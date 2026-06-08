import json
from typing import Any

from pydantic import BaseModel


class FeishuFileMessage(BaseModel):
    message_id: str
    chat_id: str | None = None
    sender_id: str | None = None
    file_key: str | None = None
    file_name: str | None = None
    file_size: int | None = None
    mime_type: str | None = None


class FeishuMessageBrief(BaseModel):
    """任意类型消息的简要信息，用于额外推送提醒。"""

    message_id: str
    chat_id: str | None = None
    chat_type: str | None = None
    sender_id: str | None = None
    message_type: str | None = None
    summary: str = ""


def is_challenge_event(payload: dict[str, Any]) -> bool:
    return "challenge" in payload


def validate_verification_token(payload: dict[str, Any], expected_token: str) -> bool:
    if not expected_token:
        return True

    token = payload.get("token")
    if token == expected_token:
        return True

    header = payload.get("header")
    if isinstance(header, dict) and header.get("token") == expected_token:
        return True

    return False


def extract_file_message(payload: dict[str, Any]) -> FeishuFileMessage | None:
    """Extract a file message from Feishu event callback payloads.

    Feishu event shapes differ by event version and message type. This parser keeps the
    spike tolerant while still returning a normalized file message for the core flow.
    """
    event = payload.get("event")
    if not isinstance(event, dict):
        return None

    message = event.get("message")
    if not isinstance(message, dict):
        return None

    message_type = message.get("message_type")
    if message_type != "file":
        return None

    content = message.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = {}
    if not isinstance(content, dict):
        content = {}

    sender = event.get("sender")
    sender_id = None
    if isinstance(sender, dict):
        sender_id = (
            sender.get("sender_id", {}).get("open_id")
            if isinstance(sender.get("sender_id"), dict)
            else None
        )

    return FeishuFileMessage(
        message_id=message.get("message_id", ""),
        chat_id=message.get("chat_id"),
        sender_id=sender_id,
        file_key=content.get("file_key"),
        file_name=content.get("file_name") or content.get("name"),
        file_size=content.get("file_size") or content.get("size"),
        mime_type=content.get("mime_type"),
    )


def _summarize_content(message_type: str | None, content: dict[str, Any]) -> str:
    """根据消息类型生成一句话内容摘要，截断过长文本。"""
    if message_type == "text":
        text = str(content.get("text") or "").strip()
        return _truncate(text) if text else "(空文本)"
    if message_type == "file":
        name = content.get("file_name") or content.get("name") or "未命名文件"
        return f"文件：{name}"
    if message_type == "image":
        return "[图片]"
    if message_type == "post":
        title = str(content.get("title") or "").strip()
        return f"富文本：{title}" if title else "[富文本]"
    if message_type == "audio":
        return "[语音]"
    if message_type == "media":
        return "[视频]"
    # 其余类型统一兜底，避免漏推。
    snippet = _truncate(json.dumps(content, ensure_ascii=False))
    return f"[{message_type or '未知类型'}] {snippet}"


def _truncate(text: str, limit: int = 100) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def extract_message_brief(payload: dict[str, Any]) -> FeishuMessageBrief | None:
    """从任意消息事件中提取发送者与内容摘要，用于额外推送提醒。

    不限定消息类型，文本、文件、图片等都会返回摘要。无法解析时返回 None。
    """
    event = payload.get("event")
    if not isinstance(event, dict):
        return None

    message = event.get("message")
    if not isinstance(message, dict):
        return None

    message_id = message.get("message_id")
    if not message_id:
        return None

    message_type = message.get("message_type")

    content = message.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = {}
    if not isinstance(content, dict):
        content = {}

    sender = event.get("sender")
    sender_id = None
    if isinstance(sender, dict) and isinstance(sender.get("sender_id"), dict):
        sender_id = sender["sender_id"].get("open_id")

    return FeishuMessageBrief(
        message_id=message_id,
        chat_id=message.get("chat_id"),
        chat_type=message.get("chat_type"),
        sender_id=sender_id,
        message_type=message_type,
        summary=_summarize_content(message_type, content),
    )
