"""ui.desktop.pages.model_page —— 模型配置页：每类模型独立选本地 / 云端 + 内核重启。

- 语义检索 Embedding / 对话抽取 LLM：各自独立选「本地」或「云端」（OpenAI 兼容）；
- 云端 API 地址、key（进钥匙串）、超时全局共享一份，填一次即可；
- 精排 Reranker 仅本地（云端暂无通用接口），省电可在「召回与精排」页关闭；
- 换云端向量维度必须与本地向量维度一致，否则需重建向量库（页面会提示）。
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ui.desktop.backend import DesktopBackend
from ui.desktop.widgets import Card, ConfigEditor

__all__ = ["ModelPage"]

# 这些字段在本页用专用控件呈现，不进「本地模型参数」自动表单
_MODEL_EXCLUDE = {
    "embedding_backend", "llm_backend", "cloud_base_url", "cloud_embedding_model",
    "cloud_llm_model", "cloud_embed_dim", "cloud_timeout_sec", "cloud_api_key_env",
}


class ModelPage(QtWidgets.QWidget):
    kernel_restarted = QtCore.Signal()

    def __init__(self, backend: DesktopBackend) -> None:
        super().__init__()
        self._backend = backend
        model = backend.config.model

        # ── 每类模型的选择 ────────────────────────────────────────────────
        card = Card(
            "模型接入 —— 每类模型独立选「本地」或「云端」；"
            "云端地址与 key 只填一次（OpenAI 兼容），选了云端的类需再填对应模型名"
        )
        self._form = QtWidgets.QFormLayout()
        self._form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)

        self._embed_backend = QtWidgets.QComboBox()
        self._embed_backend.addItem("本地（BGE-M3，维度稳定）", "local")
        self._embed_backend.addItem("云端 API", "cloud")
        self._embed_backend.setToolTip(
            "向量维度本地锁定最稳；换云端要保证「云端向量维度」与「本地向量维度」一致，否则需重建向量库"
        )
        self._embed_model = QtWidgets.QLineEdit()
        self._embed_model.setPlaceholderText("云端向量模型名，如 text-embedding-3-small")
        self._embed_dim = QtWidgets.QSpinBox()
        self._embed_dim.setRange(2, 100000)
        self._embed_dim.setToolTip("云端返回的向量维度，必须与本地向量维度一致")
        embed_row = QtWidgets.QHBoxLayout()
        embed_row.addWidget(self._embed_backend)
        embed_row.addWidget(QtWidgets.QLabel("云端模型名"))
        embed_row.addWidget(self._embed_model, 1)
        embed_row.addWidget(QtWidgets.QLabel("云端维度"))
        embed_row.addWidget(self._embed_dim)
        self._form.addRow("语义检索 Embedding", embed_row)

        self._llm_backend = QtWidgets.QComboBox()
        self._llm_backend.addItem("本地（Qwen 小模型，离线可用）", "local")
        self._llm_backend.addItem("云端 API（更强，需联网）", "cloud")
        self._llm_model = QtWidgets.QLineEdit()
        self._llm_model.setPlaceholderText("云端对话模型名，如 deepseek-chat / gpt-4o-mini")
        llm_row = QtWidgets.QHBoxLayout()
        llm_row.addWidget(self._llm_backend)
        llm_row.addWidget(QtWidgets.QLabel("云端模型名"))
        llm_row.addWidget(self._llm_model, 1)
        self._form.addRow("对话/抽取 LLM", llm_row)

        rerank_note = QtWidgets.QLabel("仅本地（云端暂无通用精排接口）；要省电可在「召回与精排」页关闭")
        rerank_note.setObjectName("hint")
        self._form.addRow("精排 Reranker", rerank_note)

        self._cloud_url = QtWidgets.QLineEdit()
        self._cloud_url.setPlaceholderText("https://api.openai.com/v1 或本地 vLLM 地址（不带尾斜杠）")
        self._form.addRow("云端 API 地址", self._cloud_url)

        self._cloud_timeout = QtWidgets.QSpinBox()
        self._cloud_timeout.setRange(3, 600)
        self._cloud_timeout.setSuffix(" s")
        self._cloud_timeout.setToolTip("云端请求超时；超时自动退回本地能力，不会卡死")
        self._form.addRow("云端超时", self._cloud_timeout)

        self._cloud_key = QtWidgets.QLineEdit()
        self._cloud_key.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self._cloud_key.setPlaceholderText("API key；留空=不改动（保存后进系统钥匙串，不写配置文件）")
        save_key = QtWidgets.QPushButton("保存到钥匙串")
        save_key.clicked.connect(self._save_cloud_key)
        key_row = QtWidgets.QHBoxLayout()
        key_row.addWidget(self._cloud_key, 1)
        key_row.addWidget(save_key)
        self._form.addRow("云端 API key", key_row)

        self._status_label = QtWidgets.QLabel("")
        self._status_label.setObjectName("hint")
        self._status_label.setWordWrap(True)
        self._form.addRow("当前实际使用", self._status_label)
        card.body.addLayout(self._form)

        # ── 本地模型文件参数（通常不用改）────────────────────────────────
        self._local_editor = ConfigEditor(
            (("本地模型参数", model),),
            excludes={"本地模型参数": _MODEL_EXCLUDE},
        )
        card.body.addWidget(self._local_editor)

        # ── 操作 ──────────────────────────────────────────────────────────
        save = QtWidgets.QPushButton("保存模型配置")
        save.setObjectName("primary")
        save.setToolTip("写入用户配置文件；引擎开关类参数需再点「应用并重启内核」")
        save.clicked.connect(self.save)
        restart = QtWidgets.QPushButton("应用并重启内核")
        restart.setObjectName("primary")
        restart.setToolTip("重建三模型与引擎；记忆数据不会丢")
        restart.clicked.connect(self.restart_kernel)
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(save)
        actions.addWidget(restart)
        actions.addStretch(1)

        self._status = QtWidgets.QLabel("—")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.addWidget(card)
        layout.addLayout(actions)
        layout.addWidget(self._status)
        layout.addStretch(1)
        self._sync_controls()
        self.refresh_status()

    # ── 数据绑定 ──────────────────────────────────────────────────────────
    def _sync_controls(self) -> None:
        model = self._backend.config.model
        self._embed_backend.setCurrentIndex(1 if model.embedding_backend == "cloud" else 0)
        self._embed_model.setText(model.cloud_embedding_model)
        self._embed_dim.setValue(int(model.cloud_embed_dim))
        self._llm_backend.setCurrentIndex(1 if model.llm_backend == "cloud" else 0)
        self._llm_model.setText(model.cloud_llm_model)
        self._cloud_url.setText(model.cloud_base_url)
        self._cloud_timeout.setValue(int(model.cloud_timeout_sec))

    def collect(self) -> dict[str, dict[str, object]]:
        patch: dict[str, object] = dict(self._local_editor.collect().get("本地模型参数", {}))
        patch.update(
            {
                "embedding_backend": str(self._embed_backend.currentData()),
                "llm_backend": str(self._llm_backend.currentData()),
                "cloud_embedding_model": self._embed_model.text().strip(),
                "cloud_llm_model": self._llm_model.text().strip(),
                "cloud_embed_dim": int(self._embed_dim.value()),
                "cloud_base_url": self._cloud_url.text().strip(),
                "cloud_timeout_sec": int(self._cloud_timeout.value()),
            }
        )
        return {"model": patch}

    def refresh_status(self) -> None:
        status = self._backend.model_status()
        embedder, llm = status["embedder"], status["llm"]
        self._status_label.setText(
            f"Embedding：{embedder['kind']}（{embedder['dim']} 维{'，已降级' if embedder['degraded'] else ''}）· "
            f"LLM：{llm['kind']}{'（已降级）' if llm['degraded'] else ''} · "
            f"key {'已配置' if status['cloud_key_present'] else '未配置'} · "
            f"选择：向量={status['embedding_backend']} / 对话={status['llm_backend']}"
        )

    # ── 动作 ──────────────────────────────────────────────────────────────
    def save(self) -> None:
        result = self._backend.save_settings_patch(self.collect())
        self._status.setText(str(result["message"]))
        self.refresh_status()

    def restart_kernel(self) -> None:
        result = self._backend.restart()
        self._status.setText(str(result["message"]))
        self.refresh_status()
        if result.get("ok"):
            self.kernel_restarted.emit()

    def _save_cloud_key(self) -> None:
        result = self._backend.save_cloud_key(self._cloud_key.text())
        self._cloud_key.clear()
        self.refresh_status()
        self._status.setText(str(result["message"]))

    def reload(self) -> None:
        """预设 / 恢复出厂后由主窗口调用：同步控件与状态。"""
        self._sync_controls()
        old = self._local_editor
        self._local_editor = ConfigEditor(
            (("本地模型参数", self._backend.config.model),),
            excludes={"本地模型参数": _MODEL_EXCLUDE},
        )
        old.parentWidget().layout().replaceWidget(old, self._local_editor)
        old.deleteLater()
        self.refresh_status()
