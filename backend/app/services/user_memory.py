from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.services.conversation_store import ConversationStore
from app.services.session_store import SessionStore


@dataclass(frozen=True)
class ClearMemoryResult:
    had_conversation: bool
    had_session: bool

    @property
    def cleared_anything(self) -> bool:
        return self.had_conversation or self.had_session


def clear_user_memory(settings: Settings, open_id: str) -> ClearMemoryResult:
    """清除指定用户的会话记忆：追问历史/文件索引 + 进行中的技能会话。"""
    conversation_store = ConversationStore(
        settings.conversation_storage_dir,
        recent_files_max=settings.qa_recent_files_max,
    )
    session_store = SessionStore(settings.session_storage_dir)

    had_session = session_store.get(open_id) is not None
    had_conversation = conversation_store.clear_user(open_id)
    session_store.clear(open_id)

    return ClearMemoryResult(
        had_conversation=had_conversation,
        had_session=had_session,
    )


def clear_memory_result_message(result: ClearMemoryResult) -> str:
    if result.cleared_anything:
        return (
            "已清理你的会话记忆，包括最近文件索引、追问历史和进行中的技能会话。\n\n"
            "已上传的原始文件与分析缓存未删除；如需继续追问请重新上传文件。"
        )
    return "暂无需要清理的会话记忆。"
