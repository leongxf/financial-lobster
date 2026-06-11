from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


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
