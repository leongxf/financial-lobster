#!/usr/bin/env python
"""单个子标题的「联网带源改写」完整闭环原型（方案 A 样张）。

流程：从模板取目标子标题及其填写要求(brief) → 结合源文件实体生成检索词 →
智谱 web search 取真实中文出处 → 带源改写(只引用给定 URL) → 引用核验(逐条核对被引
snippet 是否支撑结论) → 打印样张 + 核验表。

用法：
  python scripts/grounded_section.py \
      --source ~/Desktop/源文件.docx --template ~/Desktop/模板.docx \
      --section 市场规模 [--brief "本次额外要覆盖的点..."]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from docx import Document  # noqa: E402
from docx.text.paragraph import Paragraph  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.services.document_parser import parse_document  # noqa: E402
from app.services.llm_provider import (  # noqa: E402
    LLMConfig,
    LLMProvider,
    build_chat_provider,
)

# 复用 rewrite 原型里的层级识别，避免重复实现。
sys.path.insert(0, str(ROOT / "scripts"))
from rewrite_by_template import _infer_level  # noqa: E402

MAX_SOURCES = 8  # 喂给改写的去重后出处上限。
QUERIES_PER_SECTION = 4


# ----------------------- 模板：取目标子标题的 brief -----------------------


def extract_section_brief(template_path: Path, section_keyword: str) -> tuple[str, list[str]]:
    """从模板里找到包含 section_keyword 的子标题，返回(子标题文本, 其下的填写要求列表)。"""
    document = Document(str(template_path))
    body = document.element.body
    blocks: list[tuple[str, int | None]] = []
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if text:
                blocks.append((text, _infer_level(paragraph, child)))

    for i, (text, level) in enumerate(blocks):
        if level is not None and section_keyword in text:
            requirements: list[str] = []
            for following_text, following_level in blocks[i + 1 :]:
                if following_level is not None:
                    break  # 下一个标题，本节结束
                requirements.append(following_text)
            return text, requirements
    raise SystemExit(f"模板中未找到包含「{section_keyword}」的子标题。")


# ----------------------- 智谱 web search -----------------------


async def zhipu_search(base: str, api_key: str, query: str, engine: str = "search_std") -> list[dict]:
    url = base.rstrip("/") + "/web_search"
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"search_engine": engine, "search_query": query},
        )
        resp.raise_for_status()
    return resp.json().get("search_result") or []


# ----------------------- 各阶段 LLM 调用 -----------------------


async def generate_queries(
    provider: LLMProvider, section_title: str, requirements: list[str], source_text: str
) -> list[str]:
    req_text = "\n".join(f"- {r}" for r in requirements) or "（无额外要求）"
    messages = [
        {
            "role": "system",
            "content": "你是检索词规划助手。根据某报告小节的标题、写作要求与源文件内容，"
            "生成用于中文网络搜索的查询词。优先针对『写作要求提到、但源文件里缺失或不全』"
            "的点。每条 query 要含具体主体(公司/产品/行业)+角度(规模/增长/政策/年份)。"
            f"只输出 {QUERIES_PER_SECTION} 行查询词，每行一条，不要编号、不要解释。",
        },
        {
            "role": "user",
            "content": f"小节标题：{section_title}\n\n写作要求：\n{req_text}\n\n"
            f"源文件内容(节选)：\n{source_text[:4000]}\n\n请输出查询词：",
        },
    ]
    result = await provider.complete(messages)
    queries = [
        re.sub(r"^[\d.\-、)]+\s*", "", line).strip()
        for line in result.content.splitlines()
        if line.strip()
    ]
    return queries[:QUERIES_PER_SECTION]


async def grounded_rewrite(
    provider: LLMProvider,
    section_title: str,
    requirements: list[str],
    source_text: str,
    sources: list[dict],
) -> str:
    req_text = "\n".join(f"- {r}" for r in requirements) or "（无额外要求）"
    source_block = "\n\n".join(
        f"【{s['id']}】{s['title']}（{s.get('date') or '无日期'}）\nURL: {s['url']}\n摘要: {s['content']}"
        for s in sources
    )
    messages = [
        {
            "role": "system",
            "content": "你是专业行业研究报告撰写助手。请基于『源文件内容』和『联网检索到的出处』，"
            "撰写指定小节。严格规则：\n"
            "1. 来自联网出处的每一条数据/结论，必须在句末用【S编号】标注，编号只能是给定出处里的。\n"
            "2. 来自源文件的内容，句末标（源文件）。\n"
            "3. 不得使用未提供的任何信息，不得臆造、不得编造 URL 或编号。\n"
            "4. 出处之间数据冲突时，并列说明并各自标注，不要擅自取舍。\n"
            "5. 输出中文 Markdown：先正文，最后一节『## 来源』把用到的【S编号】列为 编号 — 标题 — URL。",
        },
        {
            "role": "user",
            "content": f"小节标题：{section_title}\n\n写作要求：\n{req_text}\n\n"
            f"源文件内容：\n{source_text}\n\n联网检索到的出处：\n{source_block}\n\n"
            "请撰写该小节：",
        },
    ]
    result = await provider.complete_until_done(messages)
    return result.content


async def verify_citations(
    provider: LLMProvider, section_markdown: str, sources: list[dict]
) -> list[dict]:
    source_block = "\n\n".join(f"【{s['id']}】摘要: {s['content']}" for s in sources)
    messages = [
        {
            "role": "system",
            "content": "你是严格的事实核查员。给定一篇带【S编号】引用的小节，以及各编号对应的"
            "出处摘要。请逐条核对：被引出处摘要是否真的支撑该句的数据/结论。"
            "输出 JSON 数组，每个元素：{\"claim\":\"被核查的句子(截断50字内)\",\"citation\":\"S编号\","
            "\"verdict\":\"supported|partial|unsupported\",\"reason\":\"简述\"}。"
            "只输出 JSON，不要其他文字。",
        },
        {
            "role": "user",
            "content": f"小节正文：\n{section_markdown}\n\n各出处摘要：\n{source_block}\n\n请输出核查 JSON：",
        },
    ]
    result = await provider.complete_until_done(messages)
    text = result.content.strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return [{"claim": "(核验输出无法解析)", "citation": "", "verdict": "?", "reason": text[:200]}]
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return [{"claim": "(JSON 解析失败)", "citation": "", "verdict": "?", "reason": text[:200]}]


# ----------------------- 主流程 -----------------------


async def run(source: Path, template: Path, section_keyword: str, extra_brief: str | None) -> None:
    settings = get_settings()
    if not settings.llm_api_key:
        raise SystemExit("未配置 LLM_API_KEY。")
    zhipu_key = settings.qa_embedding_api_key
    zhipu_base = settings.qa_embedding_base_url or "https://open.bigmodel.cn/api/paas/v4"
    if not zhipu_key:
        raise SystemExit("未配置智谱 key（QA_EMBEDDING_API_KEY）。")

    provider = build_chat_provider(
        LLMConfig(
            provider=settings.llm_provider,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout_ms=settings.llm_timeout_ms,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
        ),
        settings.fallback_models,
    )

    section_title, requirements = extract_section_brief(template, section_keyword)
    if extra_brief:
        requirements.append(extra_brief)
    print(f"[目标小节] {section_title}")
    print("[填写要求/brief]")
    for r in requirements:
        print(f"  - {r}")

    parsed = parse_document(source)
    source_text = parsed.text

    print("\n[1/4] 生成检索词...")
    queries = await generate_queries(provider, section_title, requirements, source_text)
    for q in queries:
        print(f"  · {q}")

    print("\n[2/4] 智谱联网检索...")
    seen_urls: set[str] = set()
    sources: list[dict] = []
    for q in queries:
        try:
            results = await zhipu_search(zhipu_base, zhipu_key, q)
        except Exception as exc:
            print(f"  query「{q}」检索失败：{exc}")
            continue
        for r in results:
            link = r.get("link") or ""
            if not link or link in seen_urls:
                continue
            seen_urls.add(link)
            sources.append(
                {
                    "id": f"S{len(sources) + 1}",
                    "title": r.get("title", ""),
                    "url": link,
                    "content": (r.get("content") or "").strip(),
                    "date": r.get("publish_date") or "",
                }
            )
            if len(sources) >= MAX_SOURCES:
                break
        if len(sources) >= MAX_SOURCES:
            break
    print(f"  去重后取 {len(sources)} 条出处：")
    for s in sources:
        print(f"  {s['id']}: {s['title'][:40]} | {s['url']}")

    if not sources:
        raise SystemExit("未检索到任何出处，终止。")

    print("\n[3/4] 带源改写...")
    section_md = await grounded_rewrite(provider, section_title, requirements, source_text, sources)

    print("\n[4/4] 引用核验...")
    verdicts = await verify_citations(provider, section_md, sources)

    print("\n" + "=" * 70)
    print("样张（改写后小节）")
    print("=" * 70)
    print(section_md)

    print("\n" + "=" * 70)
    print("引用核验表")
    print("=" * 70)
    counts: dict[str, int] = {}
    for v in verdicts:
        verdict = v.get("verdict", "?")
        counts[verdict] = counts.get(verdict, 0) + 1
        print(f"[{verdict}] {v.get('citation', '')}  {v.get('claim', '')}")
        print(f"        理由：{v.get('reason', '')}")
    print(f"\n核验统计：{counts}")


def main() -> None:
    parser = argparse.ArgumentParser(description="单子标题联网带源改写闭环样张")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--section", default="市场规模", help="目标子标题关键词")
    parser.add_argument("--brief", default=None, help="本次额外的填写要求(可选)")
    args = parser.parse_args()
    if not args.source.exists():
        raise SystemExit(f"源文件不存在：{args.source}")
    if not args.template.exists():
        raise SystemExit(f"模板不存在：{args.template}")
    asyncio.run(run(args.source, args.template, args.section, args.brief))


if __name__ == "__main__":
    main()
