import json
from pathlib import Path

import httpx


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = "https://open.feishu.cn/open-apis"

    async def get_tenant_access_token(self) -> str:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.base_url}/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self.app_id,
                    "app_secret": self.app_secret,
                },
            )
            response.raise_for_status()
            data = response.json()
            token = data.get("tenant_access_token")
            if not token:
                raise RuntimeError(f"failed to get tenant access token: {data}")
            return token

    async def reply_text(self, message_id: str, text: str, max_chars: int = 3500) -> None:
        if len(text) <= max_chars:
            await self._reply_text_once(message_id, text)
            return

        parts = _split_text(text, max_chars)
        for index, part in enumerate(parts, start=1):
            prefix = f"[{index}/{len(parts)}]\n" if len(parts) > 1 else ""
            await self._reply_text_once(message_id, prefix + part)

    async def _reply_text_once(self, message_id: str, text: str) -> None:
        token = await self.get_tenant_access_token()
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.base_url}/im/v1/messages/{message_id}/reply",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
            )
            response.raise_for_status()

    async def download_message_file(
        self,
        message_id: str,
        file_key: str,
        target_path: Path,
    ) -> Path:
        """Download a Feishu message file to local storage.

        This method is wired for the spike but will need real-account verification because
        Feishu file download permissions depend on app scopes and event source.
        """
        token = await self.get_tenant_access_token()
        target_path.parent.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(
                f"{self.base_url}/im/v1/messages/{message_id}/resources/{file_key}",
                params={"type": "file"},
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            target_path.write_bytes(response.content)

        return target_path


def _split_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > max_chars:
            if current:
                parts.append(current)
                current = ""
            for start in range(0, len(line), max_chars):
                parts.append(line[start : start + max_chars])
            continue

        if len(current) + len(line) > max_chars:
            parts.append(current)
            current = line
        else:
            current += line

    if current:
        parts.append(current)

    return parts
