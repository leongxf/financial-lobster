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
