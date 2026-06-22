#!/usr/bin/env python
"""仅凭模板 → 联网检索 → 总结/摘要/节选 → 生成 docx（无源文件版）。

每个带填写要求的小节：从标题+要求生成检索词 → 智谱检索真实中文出处 →
基于检索内容做总结/摘要/节选并撰写，数据结论标【S#】，无可靠出处则占位。
全文统一 S 编号，文末汇总「来源」，渲染进 docx。

用法：
  python scripts/build_from_template.py --template ~/Desktop/模板.docx \
      [--output ~/Desktop/模板_联网生成.docx]
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

from app.core.config import get_settings  # noqa: E402
from app.services.llm_provider import LLMConfig, LLMProvider, build_chat_provider  # noqa: E402
from build_full_report import parse_template_nodes  # noqa: E402
from grounded_section import zhipu_search  # noqa: E402
from rewrite_by_template import render_markdown_to_docx  # noqa: E402

QUERIES_PER_SECTION = 3
SOURCES_PER_SECTION = 5


async def gen_queries(
    provider: LLMProvider, title: str, reqs: list[str], profile: str
) -> list[str]:
    req_text = "\n".join(f"- {r}" for r in reqs) or "（无额外要求）"
    messages = [
        {
            "role": "system",
            "content": "你是检索词规划助手。根据报告小节的标题与写作要求，生成用于中文网络搜索的"
            f"查询词。每条含具体主体(公司/产品/行业)+角度(规模/增长/政策/竞争/年份)。"
            "重要：若小节是关于『目标公司/目标集团/本公司』的（如公司概况、竞争地位、产品策略），"
            "查询词必须带上项目设定里的目标公司全称；若是行业宏观（政策、市场规模、趋势），则用行业关键词、不必带公司名。"
            f"只输出 {QUERIES_PER_SECTION} 行查询词，每行一条，不要编号、不要解释。",
        },
        {
            "role": "user",
            "content": f"项目设定：\n{profile}\n\n小节标题：{title}\n\n写作要求：\n{req_text}\n\n请输出查询词：",
        },
    ]
    result = await provider.complete(messages)
    queries = [
        re.sub(r"^[\d.\-、)]+\s*", "", line).strip()
        for line in result.content.splitlines()
        if line.strip()
    ]
    return queries[:QUERIES_PER_SECTION]


async def fill_from_web(
    provider: LLMProvider, title: str, reqs: list[str], sources: list[dict], profile: str
) -> str:
    req_text = "\n".join(f"- {r}" for r in reqs) or "（无额外要求）"
    source_block = "\n\n".join(
        f"【{s['id']}】{s['title']}（{s.get('date') or '无日期'}）\nURL: {s['url']}\n摘要: {s['content']}"
        for s in sources
    )
    messages = [
        {
            "role": "system",
            "content": "你是行业研究报告撰写助手。请基于『项目设定』与『联网检索到的出处』撰写指定小节，"
            "做合理的总结、摘要与节选。严格规则：\n"
            "1. 『目标公司/目标集团』指项目设定中的公司；其身份与基本事实可依据项目设定，句末标（项目设定）。\n"
            "2. 联网出处的数据/结论句末用【S编号】标注（编号只能用给定的）。\n"
            "3. 不得使用未提供的信息，不得臆造、不得编造 URL/编号。\n"
            "4. 关键红线：若联网出处与项目设定中的目标公司/行业明显不符（例如检索回来的是其他公司或其他行业），"
            "不得采用，宁可写「（未检索到与目标公司相关的可靠来源）」，绝不张冠李戴。\n"
            "5. 多源数据冲突时并列标注，不擅自取舍。\n"
            "6. 只输出该小节中文 Markdown 正文，不要重复小节标题、不要单列『来源』段。",
        },
        {
            "role": "user",
            "content": f"项目设定：\n{profile}\n\n小节标题：{title}\n\n写作要求：\n{req_text}\n\n"
            f"联网出处：\n{source_block}\n\n请撰写：",
        },
    ]
    result = await provider.complete_until_done(messages)
    return result.content.strip()


def build_profile(company: str, facts: list[str]) -> str:
    lines = [f"目标公司：{company}"]
    if facts:
        lines.append("已知基本事实：")
        lines.extend(f"- {f}" for f in facts)
    return "\n".join(lines)


async def run(template: Path, output: Path, profile: str) -> None:
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
    content_nodes = [n for n in nodes if n["reqs"]]
    print(f"项目设定：\n{profile}")
    print(f"模板解析出 {len(nodes)} 个节点，其中 {len(content_nodes)} 个需检索填充。")

    md_parts: list[str] = []
    all_sources: list[dict] = []
    seen_urls: set[str] = set()

    for node in nodes:
        md_parts.append("#" * min(node["level"], 6) + " " + node["title"])
        if not node["reqs"]:
            continue

        title = node["title"]
        print(f"[检索] {title}")
        queries = await gen_queries(provider, title, node["reqs"], profile)
        section_sources: list[dict] = []
        for q in queries:
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
            content = await fill_from_web(provider, title, node["reqs"], section_sources, profile)
        else:
            content = "（未检索到可靠来源）"
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
    parser = argparse.ArgumentParser(description="仅凭模板 + 项目设定联网生成 → docx")
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--company", required=True, help="目标公司名称（项目设定，最小输入）")
    parser.add_argument(
        "--fact",
        action="append",
        default=[],
        dest="facts",
        help="目标公司已知基本事实，可重复传多条（可选）",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if not args.template.exists():
        raise SystemExit(f"模板不存在：{args.template}")
    profile = build_profile(args.company, args.facts)
    output = args.output or args.template.with_name(f"{args.template.stem}_联网生成.docx")
    asyncio.run(run(args.template, output, profile))


if __name__ == "__main__":
    main()
