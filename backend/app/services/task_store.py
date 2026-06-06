from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class TaskStore:
    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def create_task(
        self,
        task_id: str,
        *,
        message_id: str,
        file_key: str,
        file_name: str | None,
        model: str,
        provider: str,
    ) -> None:
        now = _now_iso()
        self.write(
            task_id,
            {
                "task_id": task_id,
                "status": "received",
                "message_id": message_id,
                "file_key": file_key,
                "file_name": file_name,
                "model": model,
                "provider": provider,
                "created_at": now,
                "updated_at": now,
                "events": [
                    {
                        "time": now,
                        "status": "received",
                        "message": "file message received",
                    }
                ],
            },
        )

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        event: str | None = None,
        **fields: Any,
    ) -> None:
        data = self.read(task_id)
        now = _now_iso()
        if status:
            data["status"] = status
        data.update(fields)
        data["updated_at"] = now
        if event or status:
            data.setdefault("events", []).append(
                {
                    "time": now,
                    "status": status or data.get("status"),
                    "message": event or status,
                }
            )
        self.write(task_id, data)

    def read(self, task_id: str) -> dict[str, Any]:
        path = self.task_path(task_id)
        if not path.exists():
            return {"task_id": task_id, "events": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, task_id: str, data: dict[str, Any]) -> None:
        path = self.task_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def task_path(self, task_id: str) -> Path:
        return self.base_dir / f"{_safe_name(task_id)}.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
