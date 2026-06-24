from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.llm_provider import LLMError, LLMProvider, TokenUsage

logger = logging.getLogger(__name__)

QA_SYSTEM_PROMPT = """你是服务于四大投资建议/交易咨询人员的材料追问助理。
用户已上传财务/经营材料，现在就材料内容向你追问。

必须严格遵守：
1. 只依据下方提供的「材料片段」回答，不得臆造、补全材料中不存在的信息。
2. 回答中尽量标注信息来源页码（材料片段已用「[第 X 页]」标记）。
3. 如果材料片段中没有足够依据，明确回答「未在材料中发现」或「材料不足以判断」。
4. 不输出投资建议、审计意见或法律意见。
5. 使用简体中文回答，简洁直接。
"""

_TOKEN_PATTERN = re.compile(r"[0-9A-Za-z]+|[\u4e00-\u9fff]")


@dataclass(frozen=True)
class RetrievedContext:
    text: str
    page_numbers: list[int]


def tokenize(text: str) -> list[str]:
    """轻量分词：英文/数字按词，中文按单字，零额外依赖。"""
    return _TOKEN_PATTERN.findall(text.lower())


def extract_keywords(text: str, top_n: int = 30) -> list[str]:
    """从文本中按词频提取关键字（过滤单字噪声，保留高频中文双字以上语义靠组合）。"""
    tokens = tokenize(text)
    counter = Counter(t for t in tokens if len(t) >= 2 or "\u4e00" <= t <= "\u9fff")
    return [word for word, _ in counter.most_common(top_n)]


def score_file_by_keywords(question: str, keywords: list[str]) -> int:
    """用问题与文件关键字的重合度打分，用于跨文件选择。"""
    if not keywords:
        return 0
    q_tokens = set(tokenize(question))
    kw_tokens = set()
    for kw in keywords:
        kw_tokens.update(tokenize(kw))
    return len(q_tokens & kw_tokens)


def load_pages(pages_path: str) -> list[dict[str, Any]]:
    path = Path(pages_path)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return []


# ============================ 向量（embedding）检索 ============================
# 中英混排材料下，关键词检索会因「中文问题 vs 英文正文」字面零重合而失效，
# 这里用多语言 embedding 做语义检索，并保留关键词检索作为兜底。


def split_pages_into_chunks(
    pages: list[dict[str, Any]],
    chunk_chars: int,
    overlap: int,
) -> list[dict[str, Any]]:
    """把按页文本切成更细的 chunk（带页码归属），用于提升检索命中精度。

    每个 chunk 记录其所属页码（以 chunk 起点所在页为准）。相邻 chunk 间保留 overlap
    个字符，避免答案恰好被切在块边界而漏召回。
    """
    chunks: list[dict[str, Any]] = []
    step = max(1, chunk_chars - overlap)
    for page in pages:
        page_no = int(page.get("page_number") or 0)
        text = str(page.get("text") or "").strip()
        if not text:
            continue
        if len(text) <= chunk_chars:
            chunks.append({"page_number": page_no, "text": text})
            continue
        start = 0
        while start < len(text):
            piece = text[start : start + chunk_chars].strip()
            if piece:
                chunks.append({"page_number": page_no, "text": piece})
            start += step
    return chunks


def embedding_cache_file(cache_dir: str, file_hash: str) -> Path:
    """向量缓存文件路径（按 file_hash 命名）。"""
    return Path(cache_dir) / f"{file_hash}.json"


def load_cached_embeddings(cache_dir: str, file_hash: str) -> list[dict[str, Any]] | None:
    """按 file_hash 读取向量缓存；命中则复用，避免同文件重复算 embedding。"""
    if not file_hash:
        return None
    path = embedding_cache_file(cache_dir, file_hash)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    chunks = data.get("chunks")
    if isinstance(chunks, list) and chunks:
        return chunks
    return None


def load_cached_embedding_model(cache_dir: str, file_hash: str) -> str | None:
    """读取该文件入库时实际使用的 embedding 模型名；查询须用同一模型保证向量空间一致。"""
    if not file_hash:
        return None
    path = embedding_cache_file(cache_dir, file_hash)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    model = data.get("model")
    return model if isinstance(model, str) and model else None


def save_cached_embeddings(
    cache_dir: str,
    file_hash: str,
    chunks: list[dict[str, Any]],
    model: str,
) -> None:
    if not file_hash:
        return
    path = embedding_cache_file(cache_dir, file_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"file_hash": file_hash, "model": model, "chunks": chunks}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


async def build_chunk_embeddings(
    pages: list[dict[str, Any]],
    provider: LLMProvider,
    models: list[str],
    chunk_chars: int,
    overlap: int,
    batch_size: int,
) -> tuple[list[dict[str, Any]], str]:
    """切块并批量计算 embedding，返回 ([{page_number, text, embedding}], 实际所用模型名)。

    按 models 顺序尝试：某模型额度耗尽（billing）时整文件改用下一个模型从头重算，
    确保同一文件所有向量来自同一模型（不同模型维度/空间不可混用）。非额度类错误直接上抛。
    """
    chunks = split_pages_into_chunks(pages, chunk_chars=chunk_chars, overlap=overlap)
    if not chunks:
        return [], (models[0] if models else "")
    texts = [c["text"] for c in chunks]

    last_error: Exception | None = None
    for model in models:
        try:
            embeddings: list[list[float]] = []
            for start in range(0, len(texts), max(1, batch_size)):
                batch = texts[start : start + batch_size]
                embeddings.extend(await provider.embed(batch, model=model))
            for chunk, vector in zip(chunks, embeddings):
                chunk["embedding"] = vector
            return chunks, model
        except LLMError as exc:
            last_error = exc
            if exc.category in ("billing", "model"):
                logger.warning(
                    "embedding 模型 %s 不可用（%s），整文件改用下一个模型重算",
                    model,
                    exc.category,
                )
                continue
            raise
    assert last_error is not None
    raise last_error


async def retrieve_by_embedding(
    question: str,
    chunks: list[dict[str, Any]],
    provider: LLMProvider,
    model: str,
    top_k: int,
    max_chars: int,
) -> RetrievedContext:
    """对问题算 embedding，与各 chunk 向量算余弦相似度取 Top-K，按字符预算拼接。"""
    if not chunks:
        return RetrievedContext(text="", page_numbers=[])

    q_vectors = await provider.embed([question], model=model)
    if not q_vectors:
        return RetrievedContext(text="", page_numbers=[])
    q_vec = q_vectors[0]

    scored: list[tuple[float, dict[str, Any]]] = []
    for chunk in chunks:
        vector = chunk.get("embedding") or []
        scored.append((_cosine(q_vec, vector), chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected = [chunk for _, chunk in scored[:top_k]]
    # 按页码升序展示，便于人类阅读与溯源。
    selected.sort(key=lambda c: int(c.get("page_number") or 0))

    parts: list[str] = []
    page_numbers: list[int] = []
    used = 0
    for chunk in selected:
        page_no = int(chunk.get("page_number") or 0)
        text = str(chunk.get("text") or "").strip()
        block = f"[第 {page_no} 页]\n{text}"
        if used + len(block) > max_chars and parts:
            break
        if used + len(block) > max_chars:
            block = block[:max_chars]
        parts.append(block)
        if page_no not in page_numbers:
            page_numbers.append(page_no)
        used += len(block)

    page_numbers.sort()
    return RetrievedContext(text="\n\n".join(parts), page_numbers=page_numbers)


def retrieve_pages(
    question: str,
    pages: list[dict[str, Any]],
    top_k: int,
    max_chars: int,
) -> RetrievedContext:
    """文件内按页 TF 检索：对每页用问题词的命中频次打分，取 Top-K，并按字符预算截断。"""
    q_tokens = [t for t in tokenize(question) if len(t) >= 2 or "\u4e00" <= t <= "\u9fff"]
    q_set = set(q_tokens)

    scored: list[tuple[int, int, dict[str, Any]]] = []
    for page in pages:
        text = str(page.get("text") or "")
        if not text.strip():
            continue
        page_tokens = Counter(tokenize(text))
        score = sum(page_tokens[t] for t in q_set)
        scored.append((score, int(page.get("page_number") or 0), page))

    # 没有任何命中时，回退到前若干页（保证仍能给模型一些上下文）。
    if not scored or all(s == 0 for s, _, _ in scored):
        selected = sorted(
            (p for p in pages if str(p.get("text") or "").strip()),
            key=lambda p: int(p.get("page_number") or 0),
        )[:top_k]
    else:
        scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        selected = [page for _, _, page in scored[:top_k]]
        selected.sort(key=lambda p: int(p.get("page_number") or 0))

    parts: list[str] = []
    page_numbers: list[int] = []
    used = 0
    for page in selected:
        page_no = int(page.get("page_number") or 0)
        text = str(page.get("text") or "").strip()
        block = f"[第 {page_no} 页]\n{text}"
        if used + len(block) > max_chars and parts:
            break
        if used + len(block) > max_chars:
            block = block[:max_chars]
        parts.append(block)
        page_numbers.append(page_no)
        used += len(block)

    return RetrievedContext(text="\n\n".join(parts), page_numbers=page_numbers)


def build_qa_messages(
    question: str,
    context: RetrievedContext,
    history: list[dict[str, str]],
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": QA_SYSTEM_PROMPT}]
    messages.extend(history)
    user_content = (
        f"材料片段：\n{context.text}\n\n"
        f"用户问题：{question}\n\n"
        "请只根据上面的材料片段回答，并标注页码。"
    )
    messages.append({"role": "user", "content": user_content})
    return messages


@dataclass(frozen=True)
class QaResult:
    answer: str
    usage: TokenUsage
    page_numbers: list[int]


async def answer_question(
    question: str,
    context: RetrievedContext,
    history: list[dict[str, str]],
    provider: LLMProvider,
    retrieval_mode: str = "unknown",
) -> QaResult:
    """根据已检索好的上下文回答问题。

    检索策略（向量 / 关键词）由调用方决定并传入 context，本函数只负责拼 prompt、
    调模型、记日志，便于排查。
    """
    messages = build_qa_messages(question, context, history)

    # 追问请求日志：用于排查追问效果不理想（检索方式、命中页、历史轮数、最终 prompt 全文）。
    logger.info(
        "[QA] request | mode=%s | question=%r | hit_pages=%s | context_chars=%d | "
        "history_turns=%d | messages=%d",
        retrieval_mode,
        question,
        context.page_numbers,
        len(context.text),
        len(history) // 2,
        len(messages),
    )
    logger.info(
        "[QA] llm payload:\n%s",
        json.dumps(messages, ensure_ascii=False, indent=2),
    )

    result = await provider.complete(messages)

    logger.info(
        "[QA] response | usage(in/out/total)=%d/%d/%d | answer=%r",
        result.usage.input_tokens,
        result.usage.output_tokens,
        result.usage.total_tokens,
        result.content,
    )

    return QaResult(
        answer=result.content,
        usage=result.usage,
        page_numbers=context.page_numbers,
    )
