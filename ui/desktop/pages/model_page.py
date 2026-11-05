"""ui.desktop.pages.model_page —— 模型配置页：每条通道独立供应商 + 内核重启。

- Embedding 与 LLM 是两条独立通道，可以是两家不同供应商：
  各自的「本地/云端」开关、API 地址、API key（进钥匙串）、模型名、超时；
- 典型组合：本地 BGE-M3 向量 + OpenAI 向量 + DeepSeek 对话，随你配；
- 云端向量维度必须与本地向量维度一致，否则需重建向量库（页面会提示）；
- 精排 Reranker 仅本地（云端暂无通用接口），省电可在「召回与精排」页关闭。
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ui.desktop.backend import DesktopBackend
from ui.desktop.widgets import Card, ConfigEditor

__all__ = ["ModelPage"]

# 这些字段在本页用专用控件呈现，不进「本地模型参数」自动表单
_MODEL_EXCLUDE = {
    "embed_backend", "embed_base_url", "embed_model", "embed_dim",
    "embed_timeout_sec", "embed_api_key_env",
    "llm_backend", "llm_base_url", "llm_model", "llm_timeout_sec", "llm_api_key_env",
}


class ModelPage(QtWidgets.QWidget):
    kernel_restarted = QtCore.Signal()

    def __init__(self, backend: DesktopBackend) -> None:
        super().__init__()
        self._backend = backend
        self._channel_bindings: dict[str, dict[str, object]] = {}

        embed_card = self._build_channel_card(
            "embed",
            "语义检索 Embedding —— 向量决定「联想」能力；本地 BGE-M3 维度稳定，云端需保证维度一致",
            ("云端向量模型名，如 text-embedding-3-small",),
        )
        llm_card = self._build_channel_card(
            "llm",
            "对话/抽取 LLM —— 抽取结构、写摘要、总结；本地小模型离线可用，云端模型更强",
            ("云端对话模型名，如 deepseek-chat / gpt-4o-mini",),
        )

        rerank_note = QtWidgets.QLabel(
            "精排 Reranker：仅本地（云端暂无通用精排接口）；要省电可在「召回与精排」页关闭"
        )
        rerank_note.setObjectName("hint")

        # ── 本地模型文件参数（通常不用改）────────────────────────────────
        self._local_editor = ConfigEditor(
            (("本地模型参数", backend.config.model),),
            excludes={"本地模型参数": _MODEL_EXCLUDE},
        )

        # ── 操作 ──────────────────────────────────────────────────────────
        save = QtWidgets.QPushButton("保存模型配置")
        save.setObjectName("primary")
        save.setToolTip("写入用户配置文件；模型来源类参数需再点「应用并重启内核」")
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
        layout.addWidget(embed_card)
        layout.addWidget(llm_card)
        layout.addWidget(rerank_note)
        layout.addWidget(self._local_editor)
        layout.addLayout(actions)
        layout.addWidget(self._status)
        layout.addStretch(1)
        self._sync_controls()
        self.refresh_status()

    # ── 通道卡片工厂 ──────────────────────────────────────────────────────
    def _build_channel_card(self, channel: str, title: str, placeholders: tuple[str, ...]) -> Card:
        config = self._backend.config
        is_embed = channel == "embed"
        prefix = "向量模型" if is_embed else "对话模型"
        card = Card(title)
        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)

        backend_box = QtWidgets.QComboBox()
        if is_embed:
            backend_box.addItem("本地（BGE-M3，维度稳定）", "local")
            backend_box.addItem("云端 API", "cloud")
        else:
            backend_box.addItem("本地（Qwen 小模型，离线可用）", "local")
            backend_box.addItem("云端 API（更强，需联网）", "cloud")
        backend_box.currentIndexChanged.connect(self.refresh_status)
        self._form_bind(channel, "backend", backend_box, lambda: str(backend_box.currentData()))
        form.addRow("模型来源", backend_box)

        model_edit = QtWidgets.QLineEdit()
        model_edit.setPlaceholderText(placeholders[0])
        self._form_bind(channel, "model", model_edit, lambda: model_edit.text().strip())
        form.addRow(f"云端{prefix}名", model_edit)

        url_edit = QtWidgets.QLineEdit()
        url_edit.setPlaceholderText("https://api.openai.com/v1（OpenAI/DeepSeek/本地 vLLM 均可，不带尾斜杠）")
        self._form_bind(channel, "url", url_edit, lambda: url_edit.text().strip())
        form.addRow("API 地址", url_edit)

        timeout = QtWidgets.QSpinBox()
        timeout.setRange(3, 600)
        timeout.setSuffix(" s")
        timeout.setToolTip("云端请求超时；超时自动退回本地能力，不会卡死")
        self._form_bind(channel, "timeout", timeout, lambda: int(timeout.value()))
        form.addRow("请求超时", timeout)

        key_edit = QtWidgets.QLineEdit()
        key_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        key_edit.setPlaceholderText(
            f"该供应商的 API key；留空=不改动（保存后进系统钥匙串，不写配置文件）"
        )
        save_key = QtWidgets.QPushButton("保存到钥匙串")
        save_key.clicked.connect(lambda: self._save_key(channel))
        key_row = QtWidgets.QHBoxLayout()
        key_row.addWidget(key_edit, 1)
        key_row.addWidget(save_key)
        self._form_bind(channel, "key_edit", key_edit, None)
        form.addRow("API key", key_row)

        if is_embed:
            dim = QtWidgets.QSpinBox()
            dim.setRange(2, 100000)
            dim.setToolTip("云端返回的向量维度，必须与「本地模型参数」里的向量维度一致，否则需重建向量库")
            self._form_bind(channel, "dim", dim, lambda: int(dim.value()))
            form.addRow("云端向量维度", dim)

        status = QtWidgets.QLabel("")
        status.setObjectName("hint")
        status.setWordWrap(True)
        self._form_bind(channel, "status", status, None)
        form.addRow("当前实际使用", status)

        card.body.addLayout(form)
        return card

    def _form_bind(self, channel: str, name: str, widget: QtWidgets.QWidget, getter) -> None:
        self._channel_bindings.setdefault(channel, {})[name] = (widget, getter)

    def _widget(self, channel: str, name: str) -> QtWidgets.QWidget:
        widget, _getter = self._channel_bindings[channel][name]  # type: ignore[misc]
        assert isinstance(widget, QtWidgets.QWidget)
        return widget

    def _sync_controls(self) -> None:
        model = self._backend.config.model
        for channel, prefix in (("embed", "embed"), ("llm", "llm")):
            backend_value = getattr(model, f"{prefix}_backend")
            combo = self._widget(channel, "backend")
            combo.setCurrentIndex(1 if backend_value == "cloud" else 0)
            self._widget(channel, "model").setText(getattr(model, f"{prefix}_model"))
            self._widget(channel, "url").setText(getattr(model, f"{prefix}_base_url"))
            timeout = self._widget(channel, "timeout")
            assert isinstance(timeout, QtWidgets.QSpinBox)
            timeout.setValue(int(getattr(model, f"{prefix}_timeout_sec")))
        dim = self._widget("embed", "dim")
        assert isinstance(dim, QtWidgets.QSpinBox)
        dim.setValue(int(model.embed_dim))

    # ── 数据 ──────────────────────────────────────────────────────────────
    def collect(self) -> dict[str, dict[str, object]]:
        patch: dict[str, object] = dict(self._local_editor.collect().get("本地模型参数", {}))
        for channel, prefix in (("embed", "embed"), ("llm", "llm")):
            patch.update(
                {
                    f"{prefix}_backend": self._channel_bindings[channel]["backend"][1](),
                    f"{prefix}_model": self._channel_bindings[channel]["model"][1](),
                    f"{prefix}_base_url": self._channel_bindings[channel]["url"][1](),
                    f"{prefix}_timeout_sec": self._channel_bindings[channel]["timeout"][1](),
                }
            )
        dim_getter = self._channel_bindings["embed"]["dim"][1]
        patch["embed_dim"] = dim_getter()
        return {"model": patch}

    def refresh_status(self) -> None:
        status = self._backend.model_status()
        for channel, backend_key, kind_key, ready_key, key_key in (
            ("embed", "embed_backend", "embedder", "embed_ready", "embed_key_present"),
            ("llm", "llm_backend", "llm", "llm_ready", "llm_key_present"),
        ):
            label = self._widget(channel, "status")
            info = status[kind_key]
            if status[backend_key] == "local":
                text = f"{info['kind']}{'（已降级）' if info['degraded'] else ''}"
                if kind_key == "embedder":
                    text = f"{text}，{info['dim']} 维"
            else:
                text = f"云端（{'配置完整' if status[ready_key] else '未配齐：地址/模型名'}）· key {'已配置' if status[key_key] else '未配置'}"
            label.setText(text)

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

    def _save_key(self, channel: str) -> None:
        key_edit = self._widget(channel, "key_edit")
        result = self._backend.save_cloud_key(channel, key_edit.text())
        key_edit.clear()
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
