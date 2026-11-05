"""ui.desktop.pages.model_page —— 模型配置页：每条通道独立供应商 + 内核重启。

- Embedding 与 LLM 是两条独立通道，可以是两家不同供应商：
  各自的「本地/云端」开关、API 地址、API key（进钥匙串）、模型名、超时；
- 典型组合：本地 BGE-M3 向量 + OpenAI 向量 + DeepSeek 对话，随你配；
- 云端向量维度必须与本地向量维度一致，否则需重建向量库（页面会提示）；
- 精排 Reranker 仅本地（云端暂无通用接口），省电可在「召回与精排」页关闭。
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

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

        # ── 顶部：本地 / 远程运行情况总览 ─────────────────────────────────
        self._summary = QtWidgets.QTableWidget(0, 5)
        self._summary.setHorizontalHeaderLabels(("通道", "模式", "设备", "状态", "模型"))
        self._summary.verticalHeader().setVisible(False)
        self._summary.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._summary.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self._summary.verticalHeader().setDefaultSectionSize(26)
        self._summary.setFixedHeight(150)
        header = self._summary.horizontalHeader()
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for column in range(4):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self._summary_head = QtWidgets.QLabel("总览：读取中…")
        self._summary_head.setObjectName("accent")
        self._summary_head.setWordWrap(True)
        summary_card = Card("模型运行状态 —— 本地 / 远程（只看状态，不会主动加载大模型）")
        summary_card.body.addWidget(self._summary_head)
        summary_card.body.addWidget(self._summary)

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

        self._rerank_note = QtWidgets.QLabel("")
        self._rerank_note.setObjectName("hint")
        self._rerank_note.setWordWrap(True)
        self._runtime_label = QtWidgets.QLabel("")
        self._runtime_label.setObjectName("hint")
        self._runtime_label.setWordWrap(True)

        # ── 本地模型文件参数（通常不用改）────────────────────────────────
        self._local_editor = ConfigEditor(
            (("本地模型参数", "model", backend.config.model),),
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
        refresh = QtWidgets.QPushButton("刷新状态")
        refresh.setToolTip("重新读取运行时 GPU 状态；不会主动加载大模型")
        refresh.clicked.connect(self.refresh_status)
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(save)
        actions.addWidget(restart)
        actions.addWidget(refresh)
        actions.addStretch(1)

        self._status = QtWidgets.QLabel("—")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)

        # 内容总量超过窗口高度时必须可滚动：直接塞进 QVBoxLayout 会被压到
        # sizeHint 以下，QFormLayout 行高塌缩、文字裁切（设置页已有同样的处理）。
        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.setSpacing(10)
        for widget in (embed_card, llm_card, self._local_editor):
            widget.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Minimum,
            )
        layout.addWidget(summary_card)
        layout.addWidget(embed_card)
        layout.addWidget(llm_card)
        layout.addWidget(self._rerank_note)
        layout.addWidget(self._runtime_label)
        layout.addWidget(self._local_editor)
        layout.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        # 操作按钮与状态固定在底部：长表单滚动时仍可随时保存 / 重启 / 刷新
        actions.setContentsMargins(18, 6, 18, 0)
        self._status.setContentsMargins(18, 0, 18, 8)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(scroll, 1)
        outer.addLayout(actions)
        outer.addWidget(self._status)
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
        # ConfigEditor 的键是配置键（model），不是卡片标题；取不到会让整组本地参数悄悄丢失
        patch: dict[str, object] = dict(self._local_editor.collect().get("model", {}))
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
        runtime = status.get("runtime", {})
        self._runtime_label.setText(
            f"运行时：CUDA Driver {runtime.get('cuda_driver_api') or '不可用'} · "
            f"llama.cpp GPU 构建 {'是' if runtime.get('llama_cpp_gpu_build') else '否'} · "
            f"use_cuda={'开' if status.get('use_cuda') else '关'}"
            "　（模型懒加载，写入或检索之后才会显示实际 provider）"
        )
        for channel, backend_key, kind_key, ready_key, key_key in (
            ("embed", "embed_backend", "embedder", "embed_ready", "embed_key_present"),
            ("llm", "llm_backend", "llm", "llm_ready", "llm_key_present"),
        ):
            label = self._widget(channel, "status")
            info = status[kind_key]
            if status[backend_key] == "local":
                if not info.get("loaded"):
                    state = "未加载（懒加载）"
                elif info.get("degraded"):
                    state = "CPU / 已降级"
                else:
                    state = "GPU 已生效"
                text = f"{info['kind']} · {state}"
                providers = info.get("providers") or []
                if providers:
                    text += f" · {'/'.join(providers)}"
                if kind_key == "embedder":
                    text = f"{text}，{info['dim']} 维"
                    text += " · 稀疏头已启用" if info.get("sparse_enabled") else " · 稀疏头未启用"
                else:
                    text += f" · GPU 层数 {info.get('active_gpu_layers')}"
            else:
                text = f"云端（{'配置完整' if status[ready_key] else '未配齐：地址/模型名'}）· key {'已配置' if status[key_key] else '未配置'}"
            label.setText(text)
        rerank = status["reranker"]
        if not rerank.get("loaded"):
            rr_state = "未加载（懒加载）"
        elif rerank.get("degraded"):
            rr_state = "CPU / 已降级"
        else:
            rr_state = "GPU 已生效"
        rr_providers = rerank.get("providers") or []
        self._rerank_note.setText(
            f"精排 Reranker：{rerank['kind']} · {rr_state} · "
            f"{'/'.join(rr_providers) or '无 provider'}"
            "　（仅本地；省电可在「召回与精排」页关闭）"
        )
        self._fill_summary(status)

    # ── 顶部总览 ──────────────────────────────────────────────────────────
    def _fill_summary(self, status: dict) -> None:
        """三通道 × 本地/远程 × 设备 × 状态，一眼看清现在谁在干活。"""
        model = self._backend.config.model
        embed, llm, rerank = status["embedder"], status["llm"], status["reranker"]

        def _device(info: dict) -> str:
            if not info.get("loaded"):
                return "—"
            return "GPU" if info.get("on_gpu") else "CPU"

        def _state(info: dict, *, cloud_ready: bool | None = None) -> str:
            if cloud_ready is not None:
                return "云端可用" if cloud_ready else "云端未配齐"
            if not info.get("loaded"):
                return "未加载（懒加载）"
            return "已降级" if info.get("degraded") else "运行中"

        embed_cloud = status.get("embed_backend") == "cloud"
        llm_cloud = status.get("llm_backend") == "cloud"
        embed_name = (
            f"{model.embed_base_url} · {model.embed_model}" if embed_cloud
            else (str(embed.get("model_file") or embed.get("kind") or "—"))
        )
        llm_name = (
            f"{model.llm_base_url} · {model.llm_model}" if llm_cloud
            else f"{llm.get('kind')} · {model.llm_gguf_file}"
        )
        rows = [
            (
                "Embedding", "远程" if embed_cloud else "本地",
                "云端" if embed_cloud else _device(embed),
                _state(embed, cloud_ready=bool(status.get("embed_ready") and status.get("embed_key_present")))
                if embed_cloud else _state(embed),
                embed_name,
            ),
            (
                "Reranker", "本地", _device(rerank), _state(rerank),
                str(rerank.get("model_file") or rerank.get("kind") or "—"),
            ),
            (
                "LLM 抽取/对话", "远程" if llm_cloud else "本地",
                "云端" if llm_cloud else _device(llm),
                _state(llm, cloud_ready=bool(status.get("llm_ready") and status.get("llm_key_present")))
                if llm_cloud else _state(llm),
                llm_name,
            ),
        ]
        self._summary.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column, value in enumerate(row):
                item = QtWidgets.QTableWidgetItem(str(value))
                if column == 2 and value == "GPU":
                    item.setForeground(QtGui.QColor("#3DDC97"))
                self._summary.setItem(row_index, column, item)

        gpu_count = sum(1 for info in (embed, rerank, llm) if info.get("on_gpu"))
        mode = f"Embedding {'远程' if embed_cloud else '本地'} · LLM {'远程' if llm_cloud else '本地'} · 精排 本地"
        self._summary_head.setText(
            f"{mode}　|　当前 {gpu_count}/3 通道在 GPU；"
            f"CUDA Driver {status.get('runtime', {}).get('cuda_driver_api') or '不可用'}"
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
            (("本地模型参数", "model", self._backend.config.model),),
            excludes={"本地模型参数": _MODEL_EXCLUDE},
        )
        old.parentWidget().layout().replaceWidget(old, self._local_editor)
        old.deleteLater()
        self.refresh_status()
