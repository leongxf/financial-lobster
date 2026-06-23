import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# 飞书接口的网络超时：连接/写入留短一些以便快速触发重试，读取放宽给慢响应。
_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
# 文件上传/下载会传大体积内容，读写超时单独放宽。
_TRANSFER_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
# 仅这些瞬时网络错误才重试；业务错误（经 raise_for_status 抛出的 4xx/5xx）不重试。
_TRANSIENT_ERRORS = (
    httpx.ConnectTimeout,
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


async def _with_retry(
    action: Callable[[], Awaitable[httpx.Response]],
    *,
    max_retries: int = 2,
    base_delay: float = 1.0,
) -> httpx.Response:
    """对飞书 HTTP 调用做瞬时网络错误重试（线性退避）。"""
    for attempt in range(max_retries + 1):
        try:
            return await action()
        except _TRANSIENT_ERRORS as exc:
            if attempt >= max_retries:
                raise
            logger.warning(
                "Feishu request transient error (%s), retrying %s/%s",
                type(exc).__name__,
                attempt + 1,
                max_retries,
            )
            await asyncio.sleep(base_delay * (attempt + 1))
    raise RuntimeError("unreachable")  # pragma: no cover


def _message_id_from_response(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except json.JSONDecodeError:
        return None
    message_id = (data.get("data") or {}).get("message_id")
    return str(message_id) if message_id else None


class FeishuClient:
    # 进程级 token 缓存（按 app_id 共享于所有实例）。客户端按消息新建，若不缓存则每条
    # 回复/进度都要现拉一次 token，长文件会瞬间把大量连接砸向飞书 auth 接口（引发 ConnectTimeout）。
    _token_cache: dict[str, tuple[str, float]] = {}

    def __init__(self, app_id: str, app_secret: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = "https://open.feishu.cn/open-apis"

    async def get_tenant_access_token(self) -> str:
        cached = self._token_cache.get(self.app_id)
        if cached is not None and time.time() < cached[1]:
            return cached[0]

        async def _send() -> httpx.Response:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    f"{self.base_url}/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": self.app_id,
                        "app_secret": self.app_secret,
                    },
                )
                resp.raise_for_status()
                return resp

        response = await _with_retry(_send)
        data = response.json()
        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError(f"获取 tenant access token 失败：{data}")
        # 飞书返回 expire（秒，通常 7200）；提前 60s 过期，留刷新缓冲。
        expire = int(data.get("expire") or 7200)
        self._token_cache[self.app_id] = (token, time.time() + max(60, expire - 60))
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

        async def _send() -> httpx.Response:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
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
                resp.raise_for_status()
                return resp

        await _with_retry(_send)

    async def reply_card(self, message_id: str, card: dict) -> str | None:
        """回复一张交互卡片到原消息会话。返回新卡片的 message_id（失败时 None）。"""
        token = await self.get_tenant_access_token()

        async def _send() -> httpx.Response:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    f"{self.base_url}/im/v1/messages/{message_id}/reply",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "msg_type": "interactive",
                        "content": json.dumps(card, ensure_ascii=False),
                    },
                )
                resp.raise_for_status()
                return resp

        response = await _with_retry(_send)
        return _message_id_from_response(response)

    async def send_card(
        self,
        receive_id: str,
        card: dict,
        receive_id_type: str = "open_id",
    ) -> str | None:
        """主动给用户/群发送交互卡片。返回 message_id（失败时 None）。"""
        token = await self.get_tenant_access_token()

        async def _send() -> httpx.Response:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    f"{self.base_url}/im/v1/messages",
                    params={"receive_id_type": receive_id_type},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json={
                        "receive_id": receive_id,
                        "msg_type": "interactive",
                        "content": json.dumps(card, ensure_ascii=False),
                    },
                )
                resp.raise_for_status()
                return resp

        response = await _with_retry(_send)
        return _message_id_from_response(response)

    async def patch_card(self, message_id: str, card: dict) -> None:
        """全量更新已发送的共享卡片（需 card.config.update_multi=true）。"""
        payload = dict(card)
        config = dict(payload.get("config") or {})
        config["update_multi"] = True
        payload["config"] = config

        token = await self.get_tenant_access_token()

        async def _send() -> httpx.Response:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.patch(
                    f"{self.base_url}/im/v1/messages/{message_id}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json={
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                )
                resp.raise_for_status()
                return resp

        await _with_retry(_send)

    async def send_file(
        self,
        receive_id: str,
        file_path: Path,
        file_name: str | None = None,
        receive_id_type: str = "open_id",
        file_type: str = "stream",
    ) -> None:
        """主动给指定用户/群发送文件消息。"""
        file_key = await self.upload_file(
            file_path=file_path,
            file_name=file_name or file_path.name,
            file_type=file_type,
        )
        token = await self.get_tenant_access_token()

        async def _send() -> httpx.Response:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    f"{self.base_url}/im/v1/messages",
                    params={"receive_id_type": receive_id_type},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json={
                        "receive_id": receive_id,
                        "msg_type": "file",
                        "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
                    },
                )
                resp.raise_for_status()
                return resp

        await _with_retry(_send)

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

        async def _send() -> httpx.Response:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    f"{self.base_url}/im/v1/messages/{message_id}/reply",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "msg_type": "text",
                        "content": json.dumps({"text": text}, ensure_ascii=False),
                    },
                )
                resp.raise_for_status()
                return resp

        await _with_retry(_send)

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

        async def _send() -> httpx.Response:
            # 在重试动作内部重新打开文件，确保每次重试都从文件头读取。
            with file_path.open("rb") as file:
                async with httpx.AsyncClient(timeout=_TRANSFER_TIMEOUT) as client:
                    resp = await client.post(
                        f"{self.base_url}/im/v1/files",
                        headers={"Authorization": f"Bearer {token}"},
                        data={
                            "file_type": file_type,
                            "file_name": file_name,
                        },
                        files={"file": (file_name, file)},
                    )
                    resp.raise_for_status()
                    return resp

        response = await _with_retry(_send)
        data = response.json()
        file_key = (data.get("data") or {}).get("file_key")
        if not file_key:
            raise RuntimeError(f"上传报告文件失败：{data}")
        return file_key

    async def _reply_file_once(self, message_id: str, file_key: str) -> None:
        token = await self.get_tenant_access_token()

        async def _send() -> httpx.Response:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    f"{self.base_url}/im/v1/messages/{message_id}/reply",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "msg_type": "file",
                        "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
                    },
                )
                resp.raise_for_status()
                return resp

        await _with_retry(_send)

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

        async def _send() -> httpx.Response:
            async with httpx.AsyncClient(timeout=_TRANSFER_TIMEOUT) as client:
                resp = await client.get(
                    f"{self.base_url}/im/v1/messages/{message_id}/resources/{file_key}",
                    params={"type": "file"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                return resp

        response = await _with_retry(_send)
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
