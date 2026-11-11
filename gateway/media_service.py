"""gateway.media_service —— 媒体生成异步旁路（v4.1 W8 / P6）。

- 进程内 `generate(media_type, params, session_id) -> dict | None`，**同步函数**，
  由 ChatEngine 放进 `asyncio.to_thread`，不阻塞 8769 消息流（P6.1）。
- B3：ComfyUI/Phane 未启动或超时 → 返回 `None`，ChatEngine 推「暂时无法生成图片」，
  不报错、不阻塞对话。首发 `stub_mode=True`，真实生成验收放第二阶段。
- P6.2：文件落 `%APPDATA%/super-brain/media/{session_id}/{uuid}.{ext}`；
  `cleanup()` 按 session_ttl 删过期目录 + 总量超限 LRU 清理。
- P6.4：媒体本体不进六引擎；只由调用方写一条 `media_interaction` meta 事件。
"""
from __future__ import annotations

import time
import urllib.request
from pathlib import Path
from typing import Any

from config import BrainConfig, get_config
from core.ids import new_uuid7
from core.runtime_paths import user_dir

__all__ = ["MediaService"]

_EXTENSIONS = {"image": ".png", "audio": ".wav", "video": ".mp4"}


class MediaService:
    def __init__(self, config: BrainConfig | None = None, *, root: Path | None = None) -> None:
        self._config = config or get_config()
        self._root = Path(root) if root else user_dir() / "media"

    @property
    def enabled(self) -> bool:
        return bool(self._config.media_service.enabled)

    def _reachable(self) -> bool:
        cfg = self._config.media_service
        url = cfg.comfyui_url.rstrip("/") + "/system_stats"
        try:
            with urllib.request.urlopen(url, timeout=float(cfg.request_timeout_sec)) as response:
                return int(getattr(response, "status", 0) or 0) < 500
        except Exception:  # noqa: BLE001  不可达即降级
            return False

    def generate(self, media_type: str, params: dict[str, Any] | None = None,
                 *, session_id: str = "default") -> dict[str, Any] | None:
        """返回 `{path, media_type, session_id}`，失败返回 None（优雅降级）。"""
        if not self.enabled:
            return None
        cfg = self._config.media_service
        if not self._reachable():
            return None
        if cfg.stub_mode:
            # 桩模式：探测到 ComfyUI 也不真正生成，等第二阶段接工作流
            return None
        extension = _EXTENSIONS.get(str(media_type).lower(), ".bin")
        session_dir = self._root / str(session_id or "default")
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / f"{new_uuid7()}{extension}"
        # 真实工作流接入点：此处应由 ComfyUI 返回 bytes 并写盘
        path.write_bytes(b"")
        return {"path": str(path), "media_type": str(media_type),
                "session_id": str(session_id), "params": dict(params or {})}

    def cleanup(self) -> dict[str, int]:
        """按 session_ttl 过期删除 + 总量超限 LRU；返回清理统计。"""
        cfg = self._config.media_service
        if not self._root.is_dir():
            return {"expired": 0, "evicted": 0}
        now = time.time()
        expired = 0
        files: list[tuple[float, int, Path]] = []
        for session_dir in [item for item in self._root.iterdir() if item.is_dir()]:
            for item in session_dir.iterdir():
                if not item.is_file():
                    continue
                stat = item.stat()
                if now - stat.st_mtime > float(cfg.session_ttl_sec):
                    item.unlink(missing_ok=True)
                    expired += 1
                else:
                    files.append((stat.st_mtime, stat.st_size, item))
            if not any(session_dir.iterdir()):
                session_dir.rmdir()
        total = sum(size for _, size, _ in files)
        budget = int(cfg.max_media_size_mb) * 1024 * 1024
        evicted = 0
        for mtime, size, item in sorted(files):
            if total <= budget:
                break
            item.unlink(missing_ok=True)
            total -= size
            evicted += 1
        return {"expired": expired, "evicted": evicted}
