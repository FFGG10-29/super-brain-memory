"""号池网关验收测试（§16.14 / §16.15）。

全部走注入的 fake transport，零真实网络；WorkBuddy 登录态用 tmp_path 样例
JSON + 环境变量 WORKBUDDY_AUTH_FILE 提供（不依赖本机真实登录态）。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import pytest

from gateway.account_pool import AccountPool, AccountPoolError, WorkBuddyCredential
from gateway.account_pool.pool import (
    PoolConfig,
    conversation_identity_of,
    mint_request_id,
    mint_session_id,
)
from gateway.account_pool.store import account_id_of, discover_accounts, load_workbuddy_auth

CN_CHAT_BASE = "https://copilot.tencent.com"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SESSION_RE = re.compile(r"^ses_[0-9a-f]{9}f[c-f][0-9a-f][0-9A-Za-z]{14}$")
REQUEST_RE = re.compile(r"^msg_[0-9a-f]{12}[0-9A-Za-z]{14}$")


def sse_body(text: str) -> bytes:
    chunk = json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False)
    return f"data: {chunk}\n\ndata: [DONE]\n\n".encode("utf-8")


class FakeTransport:
    """transport 替身：按 Authorization token 或请求序号出脚本；记录全部调用。"""

    def __init__(self, script: Any = None) -> None:
        # script: None | (status, headers, body) | callable(method, url, headers, body)
        self.script = script
        self.calls: list[tuple[str, str, dict, bytes]] = []

    def __call__(self, method: str, url: str, headers: dict, body: bytes):
        self.calls.append((method, url, headers, body))
        if callable(self.script):
            return self.script(method, url, headers, body)
        if self.script is None:
            raise AccountPoolError("fake transport 未配置脚本")
        return self.script


def make_account(token: str, nickname: str, uin: str, uid: str) -> WorkBuddyCredential:
    return WorkBuddyCredential(
        access_token=token, refresh_token=f"r_{token}", nickname=nickname,
        uin=uin, uid=uid, region="cn")


# ── 1. WorkBuddy 登录态解析（嵌套形 / 扁平形）────────────────────────────────

def _write_auth(tmp_path, data: dict) -> str:
    path = tmp_path / "workbuddy-desktop.info"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_workbuddy_auth_nested_form(tmp_path, monkeypatch):
    path = _write_auth(tmp_path, {
        "auth": {"accessToken": "tok-n", "refreshToken": "ref-n", "expiresAtMs": 9999999999999},
        "account": {"nickname": "小脑", "uin": "10001", "uid": "uid-n",
                    "enterpriseId": "ent-1"},
    })
    monkeypatch.setenv("WORKBUDDY_AUTH_FILE", path)
    cred = load_workbuddy_auth(path)
    assert cred.access_token == "tok-n"
    assert cred.refresh_token == "ref-n"
    assert cred.nickname == "小脑"
    assert cred.uin == "10001"
    assert cred.uid == "uid-n"
    assert cred.enterprise_id == "ent-1"
    assert cred.expires_at_ms == 9999999999999
    # 环境变量路径也能被发现并去重取同一账号
    found = discover_accounts()
    assert len(found) == 1 and found[0].account_id() == cred.account_id()


def test_workbuddy_auth_flat_form(tmp_path, monkeypatch):
    path = _write_auth(tmp_path, {
        "accessToken": "tok-f", "refreshToken": "ref-f",
        "nickname": "扁豆", "uin": "10002", "uid": "uid-f",
    })
    monkeypatch.setenv("WORKBUDDY_AUTH_FILE", path)
    cred = load_workbuddy_auth(path)
    assert (cred.access_token, cred.refresh_token) == ("tok-f", "ref-f")
    assert (cred.nickname, cred.uin, cred.uid) == ("扁豆", "10002", "uid-f")
    assert discover_accounts()[0].access_token == "tok-f"


# ── 2. 加密登录态：明确拒绝 ──────────────────────────────────────────────────

def test_encrypted_auth_rejected_with_xdpool_hint(tmp_path, monkeypatch):
    path = _write_auth(tmp_path, {
        "auth": {"accessToken": {"$wbEncrypted": "cipher-blob"}},
        "account": {"nickname": "密人", "uin": "10003"},
    })
    monkeypatch.setenv("WORKBUDDY_AUTH_FILE", path)
    with pytest.raises(AccountPoolError) as excinfo:
        load_workbuddy_auth(path)
    assert "xdpool" in str(excinfo.value)


# ── 3. account_id_of：稳定 + 16 hex ─────────────────────────────────────────

def test_account_id_stable_and_16_hex():
    first = account_id_of("10001", "uid-n", "小脑")
    assert re.fullmatch(r"[0-9a-f]{16}", first)
    assert account_id_of("10001", "uid-n", "小脑") == first
    assert account_id_of("10001", "uid-n", "小脑") != account_id_of("10002", "uid-n", "小脑")


# ── 4. conversation_identity_of：稳定 + UUID 形状 + 不同 key 不同 ────────────

def test_conversation_identity_stable_uuid_shaped():
    ids_a = conversation_identity_of("uin=10001|uid=uid-n")
    ids_b = conversation_identity_of("uin=10001|uid=uid-n")
    assert ids_a == ids_b
    assert len(ids_a) == 3
    for value in ids_a:
        assert UUID_RE.fullmatch(value), value
    assert len(set(ids_a)) == 3  # 三个身份互不相同
    assert conversation_identity_of("uin=10002|uid=uid-f") != ids_a


# ── 5. mint_session_id / mint_request_id 指纹 ────────────────────────────────

def test_mint_session_and_request_id_fingerprint():
    assert SESSION_RE.fullmatch(mint_session_id())
    assert REQUEST_RE.fullmatch(mint_request_id())
    # 同毫秒连发也不撞（单调计数器硬门）
    stamp = int(time.time() * 1000)
    batch = {mint_session_id(stamp) for _ in range(8)}
    assert len(batch) == 8


# ── 6. chat：SSE 聚合 + 协议头 ───────────────────────────────────────────────

def test_chat_aggregates_sse_and_sends_protocol_headers(tmp_path):
    transport = FakeTransport((200, {"Content-Type": "text/event-stream"},
                               sse_body("你好")))
    config = PoolConfig(accounts_file=str(tmp_path / "accounts.json"))
    pool = AccountPool(config, transport=transport)
    account = make_account("tok-a", "小脑", "10001", "uid-n")
    out = pool.chat(account, "coder-x", [{"role": "user", "content": "hi"}],
                    conversation_key="uin=10001|uid=uid-n")
    assert out == "你好"
    method, url, headers, body = transport.calls[0]
    assert method == "POST" and url == CN_CHAT_BASE + "/v2/chat/completions"
    assert headers["Authorization"] == "Bearer tok-a"
    assert headers["X-Agent-Intent"] == "craft"
    assert headers["X-Private-Data"] == "true"
    assert headers["X-Conversation-ID"] == conversation_identity_of(
        "uin=10001|uid=uid-n")[0]
    assert headers["X-Conversation-Request-ID"]
    assert headers["X-Conversation-Message-ID"]
    assert json.loads(body)["messages"][0]["content"] == "hi"


# ── 7. failover：429 冷却换号成功；401 登录态死亡 ─────────────────────────────

def test_failover_429_cooldown_then_account_b(tmp_path):
    def script(method, url, headers, body):
        if headers.get("Authorization") == "Bearer tok-a":
            return 429, {"Content-Type": "text/plain", "Retry-After": "1"}, b"rate"
        return 200, {"Content-Type": "text/event-stream"}, sse_body("你好")

    config = PoolConfig(accounts_file=str(tmp_path / "accounts.json"), default_model="m1")
    pool = AccountPool(config, transport=FakeTransport(script))
    pool.accounts = [make_account("tok-a", "甲", "10001", "uid-a"),
                     make_account("tok-b", "乙", "10002", "uid-b")]
    out = pool.complete("hi", model="m1", conversation_key="k")
    assert out == "你好"
    assert pool.accounts[0].cooldowns["m1"] > time.time()
    assert pool.accounts[1].cooldowns.get("m1", 0.0) == 0.0
    # 只对 A 发过 chat 上游请求，B 成功
    assert pool._transport.calls[0][2]["Authorization"] == "Bearer tok-a"
    assert pool._transport.calls[-1][2]["Authorization"] == "Bearer tok-b"


def test_failover_401_marks_dead_until(tmp_path):
    transport = FakeTransport((401, {"Content-Type": "text/plain"}, b"expired"))
    config = PoolConfig(accounts_file=str(tmp_path / "accounts.json"),
                        default_model="m1", credential_dead_sec=3600.0)
    pool = AccountPool(config, transport=transport)
    account = make_account("tok-a", "甲", "10001", "uid-a")
    pool.accounts = [account]
    with pytest.raises(AccountPoolError):
        pool.complete("hi", model="m1")
    assert account.dead_until > time.time()
    # chat 与强制刷新都被拒后死亡标记已写（不误标成功）
    assert transport.calls[0][1].endswith("/v2/chat/completions")
    assert transport.calls[-1][1].endswith("/v2/plugin/auth/token/refresh")


# ── 8. 免密 lane：默认关闭必须抛错 ────────────────────────────────────────────

def test_free_chat_disabled_by_default(tmp_path):
    config = PoolConfig(free_lane=False)
    pool = AccountPool(config, transport=FakeTransport())
    with pytest.raises(AccountPoolError):
        pool.free_chat("hi", model="some-model")
    assert pool._transport.calls == []  # 未发出任何请求
