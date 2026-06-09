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
                raise RuntimeError(f"获取 tenant access token 失败：{data}")
            return token

    async def send_text(
        self,
        receive_id: str,
        text: str,
        receive_id_type: str = "open_id",
    ) -> None:
        """主动给指定用户/群发送文本消息（im/v1/messages create 接口）。

        与 reply_text 不同：reply 是回到原消息会话，send 是主动单聊/群发。
        receive_id_type 支持 open_id / user_id / union_id / email / chat_id。
        """
        token = await self.get_tenant_access_token()
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.base_url}/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={
                    "receive_id": receive_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
            )
            response.raise_for_status()
            
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

    async def reply_file(
        self,
        message_id: str,
        file_path: Path,
        file_name: str | None = None,
        file_type: str = "stream",
    ) -> None:
        file_key = await self.upload_file(
            file_path=file_path,
            file_name=file_name or file_path.name,
            file_type=file_type,
        )
        await self._reply_file_once(message_id, file_key)

    async def upload_file(
        self,
        file_path: Path,
        file_name: str,
        file_type: str = "stream",
    ) -> str:
        token = await self.get_tenant_access_token()
        with file_path.open("rb") as file:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{self.base_url}/im/v1/files",
                    headers={"Authorization": f"Bearer {token}"},
                    data={
                        "file_type": file_type,
                        "file_name": file_name,
                    },
                    files={"file": (file_name, file)},
                )
                response.raise_for_status()

        data = response.json()
        file_key = (data.get("data") or {}).get("file_key")
        if not file_key:
            raise RuntimeError(f"上传报告文件失败：{data}")
        return file_key

    async def _reply_file_once(self, message_id: str, file_key: str) -> None:
        token = await self.get_tenant_access_token()
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.base_url}/im/v1/messages/{message_id}/reply",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "msg_type": "file",
                    "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
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
