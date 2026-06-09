import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.config import Settings, get_settings
from app.integrations.feishu.client import FeishuClient
from app.integrations.feishu.events import (
    extract_file_message,
    is_challenge_event,
    validate_verification_token,
)

router = APIRouter(prefix="/api/feishu", tags=["feishu"])
logger = logging.getLogger(__name__)


@router.post("/events")
async def handle_feishu_event(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    payload = await request.json()

    if not validate_verification_token(payload, settings.feishu_verification_token):
        logger.warning("feishu verification token mismatch")
        raise HTTPException(status_code=401, detail="verification token 校验失败")

    if is_challenge_event(payload):
        return {"challenge": payload["challenge"]}

    file_message = extract_file_message(payload)
    if file_message is None:
        logger.info("ignored non-file feishu event", extra={"payload_type": payload.get("type")})
        return {"ok": True, "ignored": True}

    logger.info(
        "received feishu file message",
        extra={
            "message_id": file_message.message_id,
            "sender_id": file_message.sender_id,
            "file_key": file_message.file_key,
            "file_name": file_message.file_name,
            "file_size": file_message.file_size,
        },
    )

    if settings.feishu_app_id and settings.feishu_app_secret and file_message.message_id:
        client = FeishuClient(settings.feishu_app_id, settings.feishu_app_secret)
        await client.reply_text(file_message.message_id, "已收到文件，正在分析。")

    # The spike intentionally stops here. The next step is to download the file into
    # local storage and create an AssistantTask once real Feishu permissions are verified.
    return {
        "ok": True,
        "message_id": file_message.message_id,
        "file_key": file_message.file_key,
    }


def build_local_file_path(storage_dir: str, message_id: str, file_name: str | None) -> Path:
    safe_name = file_name or "uploaded-file"
    return Path(storage_dir) / message_id / safe_name
