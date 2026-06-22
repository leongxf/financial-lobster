#!/usr/bin/env python
"""整份模板 → 联网带源改写 → docx（交付样张）。

策略：
- 「行业分析」章节下的小节走联网带源改写（智谱检索 + 真实 URL 标注【S#】）。
- 其余章节（公司背景/产品等）只用源文件，缺失写占位符，不联网。
- 全文统一 S 编号，文末汇总「来源」清单，渲染进 docx。

用法：
  python scripts/build_full_report.py --source ~/Desktop/源文件.docx \
      --template ~/Desktop/模板.docx --output ~/Desktop/源文件_按模板改写_联网版.docx
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from docx import Document  # noqa: E402
from docx.text.paragraph import Paragraph  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.services.document_parser import parse_document  # noqa: E402
from app.services.llm_provider import LLMConfig, LLMProvider, build_chat_provider  # noqa: E402
from grounded_section import generate_queries, zhipu_search  # noqa: E402
from rewrite_by_template import _infer_level, render_markdown_to_docx  # noqa: E402

GROUNDED_CHAPTER_KEYWORD = "行业分析"
QUERIES_PER_SECTION = 3
SOURCES_PER_SECTION = 5


def parse_template_nodes(template_path: Path) -> list[dict]:
    """把模板拆成有序节点：每个标题 + 其下填写要求 + 是否需联网。"""
    document = Document(str(template_path))
    body = document.element.body
    blocks: list[tuple[str, int | None]] = []
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if text:
                blocks.append((text, _infer_level(paragraph, child)))

    nodes: list[dict] = []
    chapter = ""
    i = 0
    n = len(blocks)
    while i < n:
        text, level = blocks[i]
        if level is None:
            i += 1
            continue
        reqs: list[str] = []
        j = i + 1
        while j < n and blocks[j][1] is None:
            reqs.append(blocks[j][0])
            j += 1
        if level == 1:
            chapter = text
        grounded = GROUNDED_CHAPTER_KEYWORD in chapter and level >= 2 and bool(reqs)
        nodes.append({"level": level, "title": text, "reqs": reqs, "grounded": grounded})
        i = j
    return nodes


async def source_only_fill(
    provider: LLMProvider, title: str, reqs: list[str], source_text: str
) -> str:
    req_text = "\n".join(f"- {r}" for r in reqs) or "（无额外要求）"
    messages = [
        {
            "role": "system",
            "content": "你是文档改写助手。只用『源文件内容』填写指定小节，按写作要求组织。"
            "严格规则：只用源文件中的信息，不臆造、不联网；句末标（源文件）；"
            "源文件缺该信息则该处写「（源文件中未提供相应内容）」。输出中文 Markdown 正文，"
            "不要重复小节标题、不要加解释。",
        },
        {
            "role": "user",
            "content": f"小节标题：{title}\n\n写作要求：\n{req_text}\n\n源文件内容：\n{source_text}\n\n请填写：",
        },
    ]
    result = await provider.complete_until_done(messages)
    return result.content.strip()


async def grounded_fill(
    provider: LLMProvider,
    title: str,
    reqs: list[str],
    source_text: str,
    sources: list[dict],
) -> str:
    req_text = "\n".join(f"- {r}" for r in reqs) or "（无额外要求）"
    source_block = "\n\n".join(
        f"【{s['id']}】{s['title']}（{s.get('date') or '无日期'}）\nURL: {s['url']}\n摘要: {s['content']}"
        for s in sources
    )
    messages = [
        {
            "role": "system",
            "content": "你是行业研究报告撰写助手。基于『源文件』和『联网出处』撰写指定小节。"
            "严格规则：\n"
            "1. 联网出处的数据/结论句末用【S编号】标注（编号只能用给定的）。\n"
            "2. 源文件内容句末标（源文件）。\n"
            "3. 不得使用未提供的信息，不得臆造、不得编造 URL/编号。\n"
            "4. 多源数据冲突时并列标注，不擅自取舍。\n"
            "5. 只输出该小节中文 Markdown 正文，不要重复小节标题、不要单列『来源』段（来源全文统一汇总）。",
        },
        {
            "role": "user",
            "content": f"小节标题：{title}\n\n写作要求：\n{req_text}\n\n源文件内容：\n{source_text}\n\n"
            f"联网出处：\n{source_block}\n\n请撰写：",
        },
    ]
    result = await provider.complete_until_done(messages)
    return result.content.strip()


async def run(source: Path, template: Path, output: Path) -> None:
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

    nodes = parse_template_nodes(template)
    source_text = parse_document(source).text
    print(f"模板解析出 {len(nodes)} 个节点，其中需联网 {sum(n['grounded'] for n in nodes)} 个。")

    md_parts: list[str] = []
    all_sources: list[dict] = []
    seen_urls: set[str] = set()

    for node in nodes:
        title = node["title"]
        level = node["level"]
        md_parts.append("#" * min(level, 6) + " " + title)
        if not node["reqs"]:
            continue

        if node["grounded"]:
            print(f"[联网] {title}")
            queries = await generate_queries(provider, title, node["reqs"], source_text)
            section_sources: list[dict] = []
            for q in queries[:QUERIES_PER_SECTION]:
                try:
                    results = await zhipu_search(zhipu_base, zhipu_key, q)
                except Exception as exc:
                    print(f"    检索失败「{q}」：{exc}")
                    continue
                for r in results:
                    link = r.get("link") or ""
                    if not link or link in seen_urls:
                        continue
                    seen_urls.add(link)
                    entry = {
                        "id": f"S{len(all_sources) + 1}",
                        "title": r.get("title", ""),
                        "url": link,
                        "content": (r.get("content") or "").strip(),
                        "date": r.get("publish_date") or "",
                    }
                    all_sources.append(entry)
                    section_sources.append(entry)
                    if len(section_sources) >= SOURCES_PER_SECTION:
                        break
                if len(section_sources) >= SOURCES_PER_SECTION:
                    break
            if section_sources:
                content = await grounded_fill(
                    provider, title, node["reqs"], source_text, section_sources
                )
            else:
                content = await source_only_fill(provider, title, node["reqs"], source_text)
            md_parts.append(content)
        else:
            print(f"[源文件] {title}")
            content = await source_only_fill(provider, title, node["reqs"], source_text)
            md_parts.append(content)

    if all_sources:
        md_parts.append("# 来源")
        for s in all_sources:
            md_parts.append(f"{s['id']} — {s['title']} — {s['url']}")

    markdown = "\n\n".join(md_parts)
    render_markdown_to_docx(markdown, template, output)
    print(f"\n完成。共引用 {len(all_sources)} 个联网出处。")
    print(f"文档：{output.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="整份模板联网带源改写 → docx")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if not args.source.exists():
        raise SystemExit(f"源文件不存在：{args.source}")
    if not args.template.exists():
        raise SystemExit(f"模板不存在：{args.template}")
    output = args.output or args.source.with_name(f"{args.source.stem}_按模板改写_联网版.docx")
    asyncio.run(run(args.source, args.template, output))


if __name__ == "__main__":
    main()
