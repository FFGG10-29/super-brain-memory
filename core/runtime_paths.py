"""core.runtime_paths —— 开发态 / PyInstaller 冻结态统一的路径解析。

分工：
- 只读资源（GGUF、ONNX、CUDA 运行库）：随安装包分发，冻结态从
  sys._MEIPASS（onedir 的 _internal）或 exe 同目录找。
- 可写数据（六引擎落盘、快照、journal）：冻结态一律放用户目录，
  绝不写到 Program Files 或 _MEI 临时目录（无写权限 / 退出即清理）。
- 开发态保持项目根 data/，避免搬走既有数据。

所有路径都能用环境变量覆盖，方便在别的机器安装后自定义位置或做离线验证。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

__all__ = [
    "APP_NAME", "clear_local_bridge", "data_dir", "env_file", "is_frozen",
    "local_bridge_path", "models_dir", "project_root", "read_local_bridge",
    "resource_root", "user_dir", "write_local_bridge",
]

APP_NAME = "super-brain"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 冻结产物中。"""
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_root() -> Path:
    """只读资源根：冻结态优先 sys._MEIPASS，onedir 下即 exe 旁的 _internal。"""
    override = os.environ.get("SUPER_BRAIN_RESOURCE_DIR")
    if override:
        return Path(override)
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else project_root()
    return project_root()


def user_dir() -> Path:
    override = os.environ.get("SUPER_BRAIN_HOME")
    if override:
        path = Path(override)
    else:
        base = os.environ.get("APPDATA") or str(Path.home() / ".config")
        path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _data_pointer() -> str:
    """Flutter「数据目录一键迁移」写下的目标；重启后端后生效。

    优先级低于 SUPER_BRAIN_DATA_DIR 环境变量（离线验证/排障要能随时覆盖）。
    文件放在 user_dir()，与 core.desktop_features._pointer_path() 严格同源。
    """
    try:
        payload = json.loads((user_dir() / "data_pointer.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(payload.get("path") or "") if isinstance(payload, dict) else ""


def data_dir() -> Path:
    override = os.environ.get("SUPER_BRAIN_DATA_DIR") or _data_pointer()
    if override:
        path = Path(override)
    elif is_frozen():
        path = user_dir() / "data"
    else:
        path = project_root() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_dir() -> Path:
    """模型权重目录：环境变量 > 随包 models/ > 用户目录 models/ > 开发态 data/models。"""
    override = os.environ.get("MODEL_LLM_GGUF_DIR") or os.environ.get("SUPER_BRAIN_MODELS_DIR")
    if override:
        return Path(override)
    if is_frozen():
        candidates = [resource_root() / "models", user_dir() / "models"]
        for path in candidates:
            if path.is_dir():
                return path
        return candidates[0]
    return data_dir() / "models"


def local_bridge_path() -> Path:
    """本机桥接描述文件（HTTP 地址 + 一次性令牌）。

    MCP stdio 进程要靠它找到正在运行的桌面内核：Kùzu 单写锁决定了「一个进程独占数据库」，
    多个 Agent 各自的 stdio 服务器只能当瘦客户端转发，不能各自开库。
    该文件落在用户目录（非仓库、非安装目录），只对本机账号可读。
    """
    return user_dir() / "mcp_bridge.json"


def write_local_bridge(*, host: str, port: int, token: str, agent_id: str = "") -> Path:
    """写桥接描述文件（桌面内核启动 HTTP 后调用）。"""
    path = local_bridge_path()
    payload = {
        "url": f"http://{host}:{port}",
        "token": token,
        "agent_id": agent_id,
        "ts_ms": int(time.time() * 1000),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def local_token_path() -> Path:
    """持久化的本机 MCP 令牌（与运行状态无关：服务停了也还在，重启复用同一把）。"""
    return user_dir() / "mcp_token.txt"


def read_local_token() -> str:
    try:
        return local_token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_local_token(token: str) -> None:
    try:
        local_token_path().parent.mkdir(parents=True, exist_ok=True)
        local_token_path().write_text(token, encoding="utf-8")
    except OSError:
        pass


def read_local_bridge() -> dict[str, object]:
    """读桥接描述文件；不存在或损坏返回空 dict。"""
    try:
        payload = json.loads(local_bridge_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def clear_local_bridge() -> None:
    try:
        local_bridge_path().unlink()
    except OSError:
        pass


def env_file() -> Path:
    override = os.environ.get("SUPER_BRAIN_ENV_FILE")
    if override:
        return Path(override)
    if is_frozen():
        return user_dir() / "config.env"   # 用户可写，不随升级被覆盖
    return project_root() / ".env"
