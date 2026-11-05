"""批量导入 + 路径选择验收。"""
from __future__ import annotations

import json

import pytest
from PySide6 import QtWidgets

from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig
from ui.desktop.backend import DesktopBackend
from ui.desktop.pages import MemoryPage
from ui.desktop.widgets import ConfigEditor


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture()
def desktop_config(tmp_path):
    return BrainConfig(
        storage=StorageConfig(
            sqlite_path=tmp_path / "r.db", lancedb_path=tmp_path / "v.lance",
            kuzu_path=tmp_path / "g.kuzu", lmdb_path=tmp_path / "kv.lmdb", duckdb_path=tmp_path / "t.db",
        ),
        model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=tmp_path / "models"),
        sync=SyncConfig(device_id="dev-import", local_repo_dir=tmp_path / "sync"),
    )


@pytest.fixture()
def backend(desktop_config):
    instance = DesktopBackend(desktop_config)
    try:
        yield instance
    finally:
        instance.close()


def _write_ndjson(tmp_path):
    path = tmp_path / "memories.ndjson"
    rows = [
        {"content": "从旧笔记迁移的记忆一：项目排期定在下周三", "memory_type": "fact"},
        {"content": "从旧笔记迁移的记忆二：用户讨厌冗长解释"},
        {"content": "从旧笔记迁移的记忆三：周五例行复盘改成双周"},
    ]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    return path


def test_import_ndjson_roundtrip(backend, tmp_path):
    path = _write_ndjson(tmp_path)
    result = backend.import_memories(path)
    assert result["ok"] is True
    assert result["parsed"] == 3
    assert result["applied"] == 3
    contents = [row["content"] for row in backend.memories(limit=50)]
    assert any("项目排期定在下周三" in item for item in contents)

    # 再导一次：全部被去重跳过
    again = backend.import_memories(path)
    assert again["applied"] == 0 and again["skipped"] == 3


def test_import_txt_and_md(backend, tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("聊天记录一：缓存服务迁移到新机房\n聊天记录二：把周报改成异步提交\n", encoding="utf-8")
    result = backend.import_memories(txt)
    assert result["applied"] == 2

    md = tmp_path / "notes.md"
    md.write_text("## 会议决定\n\n下季度砍掉两个边缘项目，聚焦记忆检索。\n\n附注：预算不变。\n", encoding="utf-8")
    result_md = backend.import_memories(md)
    assert result_md["parsed"] >= 2
    assert result_md["applied"] >= 2


def test_import_missing_file_and_empty(backend, tmp_path):
    assert backend.import_memories(tmp_path / "nope.txt")["ok"] is False
    empty = tmp_path / "empty.txt"
    empty.write_text("\n\n", encoding="utf-8")
    assert backend.import_memories(empty)["ok"] is False


def test_import_stop_and_progress(backend, tmp_path):
    txt = tmp_path / "many.txt"
    txt.write_text("\n".join(f"批量导入压力测试第 {i} 条：内容各不相同 {i}" for i in range(20)), encoding="utf-8")
    seen: list[tuple[int, int]] = []
    result = backend.import_memories(
        txt, should_stop=lambda: len(seen) >= 5, progress_cb=lambda done, total: seen.append((done, total))
    )
    assert len(seen) == 5
    assert result["applied"] == 5
    assert "手动停止" in result["message"]


def test_memory_page_has_import_button(backend, qapp):
    page = MemoryPage(backend)
    buttons = page.findChildren(QtWidgets.QPushButton)
    assert any(button.text() == "批量导入" for button in buttons)


def test_config_editor_path_fields_have_browse_button(desktop_config, qapp):
    editor = ConfigEditor((("存储引擎", desktop_config.storage),))
    buttons = [w for w in editor.findChildren(QtWidgets.QPushButton) if w.text() == "浏览…"]
    assert len(buttons) >= 5, "五个存储路径字段都应有「浏览…」选择器"


def test_nebula_stays_alive_without_retrieval(backend, desktop_config, qapp):
    """静息态活性：无检索时节点持续漂移 + 周期性自发脉冲。"""
    import random

    from ui.desktop.neurons import NebulaView

    random.seed(7)
    view = NebulaView(desktop_config, auto_animate=False)
    graph = backend.graph()
    view.set_graph(graph)
    before = {node.node_id: (node.x(), node.y()) for node in view.nodes}
    for _ in range(40):
        view.animate()
    after = {node.node_id: (node.x(), node.y()) for node in view.nodes}
    assert before != after, "漂移微扰下节点必须持续移动"
    assert view.pulse_count() + len(view.last_arrivals) > 0, "自发脉冲必须周期性放电"
