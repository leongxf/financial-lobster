from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_SESSION_TTL_SECONDS = 30 * 60  # 30 分钟无活动则视为无活跃会话


class SessionStore:
    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, open_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in open_id)
        return self.base_dir / f"{safe}.json"

    def get(self, open_id: str) -> dict[str, Any] | None:
        """返回活跃会话；超过 TTL 或不存在返回 None。"""
        p = self._path(open_id)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - float(data.get("updated_at", 0)) > _SESSION_TTL_SECONDS:
            return None
        return data

    def set_active(
        self,
        open_id: str,
        skill_id: str,
        awaiting: str | None = None,
        args: dict | None = None,
    ) -> None:
        self._path(open_id).write_text(
            json.dumps(
                {
                    "active_skill": skill_id,
                    "awaiting": awaiting,
                    "args": args or {},
                    "updated_at": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def clear(self, open_id: str) -> None:
        self._path(open_id).unlink(missing_ok=True)
