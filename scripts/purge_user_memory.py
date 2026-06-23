#!/usr/bin/env python
"""清理超过 TTL 的用户会话记忆（storage/conversations）。

用法：
  python scripts/purge_user_memory.py           # 执行清理
  python scripts/purge_user_memory.py -n        # dry-run，只统计不删除
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.services.conversation_store import (  # noqa: E402
    ConversationStore,
    _parse_iso,
)


def _dry_run_stats(store: ConversationStore, *, ttl_days: int) -> dict[str, int]:
    cutoff = datetime.now(UTC) - timedelta(days=ttl_days)
    users_scanned = 0
    users_would_delete = 0
    files_would_remove = 0

    for path in sorted(store.base_dir.glob("*.json")):
        users_scanned += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        files = data.get("files", {})
        if not isinstance(files, dict):
            continue
        expired = 0
        for entry in files.values():
            if not isinstance(entry, dict):
                expired += 1
                continue
            active_at = entry.get("last_active_at") or entry.get("created_at") or ""
            parsed = _parse_iso(str(active_at))
            if parsed is not None and parsed <= cutoff:
                expired += 1
        files_would_remove += expired
        if expired == len(files):
            users_would_delete += 1

    return {
        "users_scanned": users_scanned,
        "users_would_delete": users_would_delete,
        "files_would_remove": files_would_remove,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="清理超过 TTL 的用户会话记忆")
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="只统计将清理的数量，不实际删除",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.user_memory_cleanup_enabled:
        print("[purge-user-memory] USER_MEMORY_CLEANUP_ENABLED=false，已跳过。")
        return

    store = ConversationStore(
        settings.conversation_storage_dir,
        recent_files_max=settings.qa_recent_files_max,
    )
    ttl_days = settings.user_memory_ttl_days

    if args.dry_run:
        stats = _dry_run_stats(store, ttl_days=ttl_days)
        print(
            "[purge-user-memory] dry-run "
            f"ttl={ttl_days}d | users={stats['users_scanned']} | "
            f"users_to_delete={stats['users_would_delete']} | "
            f"files_to_remove={stats['files_would_remove']}"
        )
        return

    stats = store.purge_all_expired(ttl_days=ttl_days)
    print(
        "[purge-user-memory] done "
        f"ttl={ttl_days}d | users={stats.users_scanned} | "
        f"users_deleted={stats.users_deleted} | files_removed={stats.files_removed}"
    )


if __name__ == "__main__":
    main()
