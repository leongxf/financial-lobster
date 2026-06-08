from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.llm_provider import LLMProvider, TokenUsage

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
    pages: list[dict[str, Any]],
    history: list[dict[str, str]],
    provider: LLMProvider,
    top_k: int,
    max_chars: int,
) -> QaResult:
    context = retrieve_pages(question, pages, top_k=top_k, max_chars=max_chars)
    messages = build_qa_messages(question, context, history)

    # 追问请求日志：用于排查追问效果不理想（检索命中、历史轮数、最终 prompt 全文）。
    logger.info(
        "[QA] request | question=%r | hit_pages=%s | context_chars=%d | history_turns=%d | messages=%d",
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
