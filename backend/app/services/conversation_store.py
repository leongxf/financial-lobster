from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


@dataclass(frozen=True)
class PurgeStats:
    users_scanned: int = 0
    users_deleted: int = 0
    files_removed: int = 0

    def __add__(self, other: PurgeStats) -> PurgeStats:
        return PurgeStats(
            users_scanned=self.users_scanned + other.users_scanned,
            users_deleted=self.users_deleted + other.users_deleted,
            files_removed=self.files_removed + other.files_removed,
        )


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


class ConversationStore:
    """以飞书 open_id 为键的轻量会话存储（本地 JSON）。

    每个用户维护最近若干个文件的画像（文件名、摘要、关键字、分页文本路径）以及
    每个文件下的多轮对话历史。超过上限的文件按最近活跃时间 LRU 淘汰。

    数据结构（单个用户一个 JSON 文件）：
        {
            "open_id": "ou_xxx",
            "current_file_id": "<task_id>",
            "files": {
                "<task_id>": {
                    "file_id": "<task_id>",
                    "file_name": "xxx.pdf",
                    "pages_path": "storage/uploads/<mid>/pages.json",
                    "summary": "一句话摘要",
                    "keywords": ["收入", "利润", ...],
                    "history": [{"role": "user"/"assistant", "content": "..."}],
                    "created_at": "...",
                    "last_active_at": "..."
                }
            }
        }
    """

    def __init__(self, base_dir: Path | str, recent_files_max: int = 5) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.recent_files_max = recent_files_max

    # ---- 读写底层 ----
    def _path(self, open_id: str) -> Path:
        return self.base_dir / f"{_safe_name(open_id)}.json"

    def read(self, open_id: str) -> dict[str, Any]:
        path = self._path(open_id)
        if not path.exists():
            return {"open_id": open_id, "current_file_id": None, "files": {}}
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, open_id: str, data: dict[str, Any]) -> None:
        path = self._path(open_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def clear_user(self, open_id: str) -> bool:
        """删除指定用户的全部会话数据；不存在时返回 False。"""
        path = self._path(open_id)
        if not path.exists():
            return False
        path.unlink(missing_ok=True)
        return True

    # ---- 业务方法 ----
    def upsert_file(
        self,
        open_id: str,
        *,
        file_id: str,
        file_name: str | None,
        pages_path: str,
        summary: str = "",
        keywords: list[str] | None = None,
        file_hash: str | None = None,
        embeddings_path: str = "",
    ) -> None:
        """新增或更新一个文件画像，并设为当前文件；超出上限按 LRU 淘汰。

        去重键优先用 file_hash（相同内容的文件视为同一个，重复上传复用同一条画像，
        不挤占最近文件名额）；未提供 file_hash 时回退用 file_id。

        画像额外记录：
        - entry_key：该画像在 files 字典中的键（= dedup_key），供调用方做 history 读写时
          与字典键保持一致（file_hash 去重后 file_id 不再等于字典键）。
        - embeddings_path：向量缓存文件路径（按 file_hash 命名），用于追问时向量检索。
        """
        data = self.read(open_id)
        files: dict[str, Any] = data.setdefault("files", {})
        now = _now_iso()

        dedup_key = file_hash or file_id
        existing = files.get(dedup_key) or {}
        files[dedup_key] = {
            "entry_key": dedup_key,
            "file_id": file_id,
            "file_hash": file_hash,
            "file_name": file_name,
            "pages_path": pages_path,
            "embeddings_path": embeddings_path,
            "summary": summary,
            "keywords": keywords or [],
            "history": existing.get("history", []),
            "created_at": existing.get("created_at", now),
            "last_active_at": now,
        }
        data["current_file_id"] = dedup_key
        self._evict(files)
        # 当前文件可能被淘汰的极端情况：重新指向最近活跃文件。
        if data["current_file_id"] not in files:
            data["current_file_id"] = self._most_recent_file_id(files)
        self.write(open_id, data)

    def update_embeddings_path(self, open_id: str, entry_key: str, embeddings_path: str) -> None:
        """仅补写指定文件画像的 embeddings_path（向量算完后调用）。

        文件在报告发出后即以空 embeddings_path 登记，确保用户能立即追问（先走关键词检索）；
        待后台 embedding 算完再用本方法补写路径，无缝升级为向量检索。只动单字段，不重置
        last_active_at / current_file_id，避免覆盖期间用户追问产生的状态。
        """
        data = self.read(open_id)
        entry = data.get("files", {}).get(entry_key)
        if entry is None:
            return
        entry["embeddings_path"] = embeddings_path
        self.write(open_id, data)

    def touch_file(self, open_id: str, file_id: str) -> None:
        data = self.read(open_id)
        files = data.get("files", {})
        if file_id in files:
            files[file_id]["last_active_at"] = _now_iso()
            data["current_file_id"] = file_id
            self.write(open_id, data)

    def append_history(
        self,
        open_id: str,
        file_id: str,
        *,
        question: str,
        answer: str,
        max_turns: int,
    ) -> None:
        """追加一轮问答到指定文件历史，只保留最近 max_turns 轮。"""
        data = self.read(open_id)
        files = data.get("files", {})
        file_entry = files.get(file_id)
        if file_entry is None:
            return
        history: list[dict[str, str]] = file_entry.setdefault("history", [])
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})
        # 一轮 = user + assistant 两条，保留最近 max_turns 轮。
        keep = max_turns * 2
        if keep > 0 and len(history) > keep:
            file_entry["history"] = history[-keep:]
        file_entry["last_active_at"] = _now_iso()
        data["current_file_id"] = file_id
        self.write(open_id, data)

    def get_current_file(self, open_id: str) -> dict[str, Any] | None:
        data = self.read(open_id)
        files = data.get("files", {})
        current = data.get("current_file_id")
        if current and current in files:
            return files[current]
        return None

    def list_files(self, open_id: str) -> list[dict[str, Any]]:
        data = self.read(open_id)
        files = list(data.get("files", {}).values())
        files.sort(key=lambda f: f.get("last_active_at", ""), reverse=True)
        return files

    def recent_history(self, open_id: str, file_id: str, max_turns: int) -> list[dict[str, str]]:
        data = self.read(open_id)
        file_entry = data.get("files", {}).get(file_id)
        if not file_entry:
            return []
        history = file_entry.get("history", [])
        keep = max_turns * 2
        return history[-keep:] if keep > 0 else history

    def purge_expired(self, open_id: str, *, ttl_days: int) -> PurgeStats:
        """清理指定用户超过 TTL 的文件画像；若全部过期则删除用户 JSON。"""
        if ttl_days <= 0:
            return PurgeStats()
        path = self._path(open_id)
        if not path.exists():
            return PurgeStats()
        return self._purge_path(path, ttl_days=ttl_days)

    def purge_all_expired(self, *, ttl_days: int) -> PurgeStats:
        """扫描全部用户 JSON，清理超过 TTL 的文件画像。"""
        if ttl_days <= 0:
            return PurgeStats()
        total = PurgeStats()
        for path in sorted(self.base_dir.glob("*.json")):
            total += self._purge_path(path, ttl_days=ttl_days)
        return total

    # ---- 内部工具 ----
    def _purge_path(self, path: Path, *, ttl_days: int) -> PurgeStats:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return PurgeStats(users_scanned=1)

        files_removed = self._remove_expired_files(data, ttl_days=ttl_days)
        stats = PurgeStats(users_scanned=1, files_removed=files_removed)
        files = data.get("files", {})
        if not isinstance(files, dict) or not files:
            path.unlink(missing_ok=True)
            return PurgeStats(
                users_scanned=stats.users_scanned,
                users_deleted=1,
                files_removed=stats.files_removed,
            )
        if files_removed:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        return stats

    def _remove_expired_files(self, data: dict[str, Any], *, ttl_days: int) -> int:
        files = data.get("files", {})
        if not isinstance(files, dict) or not files:
            return 0

        cutoff = datetime.now(UTC) - timedelta(days=ttl_days)
        expired_keys = [
            key
            for key, entry in files.items()
            if self._entry_expired(entry, cutoff=cutoff)
        ]
        for key in expired_keys:
            files.pop(key, None)

        if expired_keys and data.get("current_file_id") not in files:
            data["current_file_id"] = self._most_recent_file_id(files)
        return len(expired_keys)

    @staticmethod
    def _entry_expired(entry: Any, *, cutoff: datetime) -> bool:
        if not isinstance(entry, dict):
            return True
        active_at = entry.get("last_active_at") or entry.get("created_at") or ""
        parsed = _parse_iso(str(active_at))
        if parsed is None:
            return False
        return parsed <= cutoff

    # ---- 内部工具 ----
    def _evict(self, files: dict[str, Any]) -> None:
        if len(files) <= self.recent_files_max:
            return
        # 按最近活跃时间升序，淘汰最旧的，直到不超过上限。
        ordered = sorted(files.items(), key=lambda kv: kv[1].get("last_active_at", ""))
        for key, _ in ordered[: len(files) - self.recent_files_max]:
            files.pop(key, None)

    @staticmethod
    def _most_recent_file_id(files: dict[str, Any]) -> str | None:
        if not files:
            return None
        return max(files.items(), key=lambda kv: kv[1].get("last_active_at", ""))[0]
