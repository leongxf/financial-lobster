from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class EventDeduplicator:
    """基于 message_id 的飞书事件去重。

    飞书未在限定时间内收到回调 ACK 时，会重推同一条事件（message_id 保持不变）。
    若不去重，会重复下载、重复分析、重复回复并重复消耗 token。

    这里用「文件系统 O_EXCL 原子创建标记文件」实现认领：同一 message_id 只有第一次
    create 成功，后续 create 触发 FileExistsError 即判定为重复。O_EXCL 在 OS 层是原子的，
    天然适配多线程/多进程并发，无需额外加锁。
    """

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def mark_if_new(self, message_id: str) -> bool:
        """首次见到该 message_id 时认领并返回 True；已见过返回 False。

        没有 message_id 时无法去重，按新事件放行（返回 True），不阻断主流程。
        标记文件读写失败同样放行，宁可重复也不要漏处理。
        """
        if not message_id:
            return True
        path = self._marker_path(message_id)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        except OSError:
            logger.exception("failed to create dedup marker for %s", message_id)
            return True
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(time.time()))
        except OSError:
            logger.exception("failed to write dedup marker for %s", message_id)
        return True

    def _marker_path(self, message_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in message_id)
        return self.base_dir / f"{safe}.seen"
