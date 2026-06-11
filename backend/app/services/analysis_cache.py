from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.services.llm_provider import TokenUsage


@dataclass(frozen=True)
class CachedChunk:
    markdown: str
    original_usage: TokenUsage


@dataclass(frozen=True)
class ChunkCacheKey:
    """缓存命中标识：只描述「输入内容」，不含模型/调用参数。

    刻意不把 provider/model/temperature/max_tokens 纳入 key，这样换模型仍能命中——
    对「客观信息抽取」任务不同模型结果近似可互换。需要主动失效时 bump prompt_version。
    """

    prompt_version: str
    chunk_chars: int
    file_hash: str
    chunk_index: int
    chunk_hash: str

    def digest(self) -> str:
        payload = {
            "prompt_version": self.prompt_version,
            "chunk_chars": self.chunk_chars,
            "file_hash": self.file_hash,
            "chunk_index": self.chunk_index,
            "chunk_hash": self.chunk_hash,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class AnalysisCache:
    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_chunk(self, key: ChunkCacheKey) -> CachedChunk | None:
        path = self._path_for_key(key)
        if not path.exists():
            return None

        data = json.loads(path.read_text(encoding="utf-8"))
        usage = data.get("original_usage") or {}
        return CachedChunk(
            markdown=data["markdown"],
            original_usage=TokenUsage(
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
            ),
        )

    def set_chunk(
        self,
        key: ChunkCacheKey,
        *,
        markdown: str,
        usage: TokenUsage,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        path = self._path_for_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cache_key": key.digest(),
            "created_at": datetime.now(UTC).isoformat(),
            "metadata": metadata or {},
            "markdown": markdown,
            "original_usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
            },
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _path_for_key(self, key: ChunkCacheKey) -> Path:
        digest = key.digest()
        return self.base_dir / "chunks" / digest[:2] / f"{digest}.json"


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
