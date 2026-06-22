"""行业研究四步管线（从 scripts/ 迁入，供 IndustryResearchSkill 调用）。"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.services.llm_provider import LLMProvider
from app.services.template_docx import parse_template_nodes, render_markdown_to_docx
from app.skills.compliance import COMPLIANCE_PROMPT

NotifyFn = Callable[[str], Awaitable[None]]

GROUNDED_CHAPTER_KEYWORD = "行业分析"
SOURCES_PER_SECTION = 5


def build_profile(company: str, facts: list[str] | None = None) -> str:
    lines = [f"目标公司：{company}"]
    if facts:
        lines.append("已知基本事实：")
        lines.extend(f"- {f}" for f in facts)
    return "\n".join(lines)


async def generate_queries_with_source(
    provider: LLMProvider,
    section_title: str,
    requirements: list[str],
    source_text: str,
    queries_per_section: int,
) -> list[str]:
    req_text = "\n".join(f"- {r}" for r in requirements) or "（无额外要求）"
    messages = [
        {
            "role": "system",
            "content": "你是检索词规划助手。根据某报告小节的标题、写作要求与源文件内容，"
            "生成用于中文网络搜索的查询词。优先针对『写作要求提到、但源文件里缺失或不全』"
            "的点。每条 query 要含具体主体(公司/产品/行业)+角度(规模/增长/政策/年份)。"
            f"只输出 {queries_per_section} 行查询词，每行一条，不要编号、不要解释。",
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
    return queries[:queries_per_section]


async def generate_queries_from_profile(
    provider: LLMProvider,
    title: str,
    reqs: list[str],
    profile: str,
    queries_per_section: int,
) -> list[str]:
    req_text = "\n".join(f"- {r}" for r in reqs) or "（无额外要求）"
    messages = [
        {
            "role": "system",
            "content": "你是检索词规划助手。根据报告小节的标题与写作要求，生成用于中文网络搜索的"
            f"查询词。每条含具体主体(公司/产品/行业)+角度(规模/增长/政策/竞争/年份)。"
            "重要：若小节是关于『目标公司/目标集团/本公司』的（如公司概况、竞争地位、产品策略），"
            "查询词必须带上项目设定里的目标公司全称；若是行业宏观（政策、市场规模、趋势），则用行业关键词、不必带公司名。"
            f"只输出 {queries_per_section} 行查询词，每行一条，不要编号、不要解释。",
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
    return queries[:queries_per_section]


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
            "content": COMPLIANCE_PROMPT
            + "\n\n你是专业行业研究报告撰写助手。请基于『源文件内容』和『联网检索到的出处』，"
            "撰写指定小节。额外规则：\n"
            "1. 来自联网出处的每一条数据/结论，必须在句末用【S编号】标注，编号只能是给定出处里的。\n"
            "2. 来自源文件的内容，句末标（源文件）。\n"
            "3. 输出中文 Markdown：先正文，最后一节『## 来源』把用到的【S编号】列为 编号 — 标题 — URL。",
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


async def fill_from_web(
    provider: LLMProvider,
    title: str,
    reqs: list[str],
    sources: list[dict],
    profile: str,
) -> str:
    req_text = "\n".join(f"- {r}" for r in reqs) or "（无额外要求）"
    source_block = "\n\n".join(
        f"【{s['id']}】{s['title']}（{s.get('date') or '无日期'}）\nURL: {s['url']}\n摘要: {s['content']}"
        for s in sources
    )
    messages = [
        {
            "role": "system",
            "content": COMPLIANCE_PROMPT
            + "\n\n你是行业研究报告撰写助手。请基于『项目设定』与『联网检索到的出处』撰写指定小节，"
            "做合理的总结、摘要与节选。额外规则：\n"
            "1. 『目标公司/目标集团』指项目设定中的公司；其身份与基本事实可依据项目设定，句末标（项目设定）。\n"
            "2. 联网出处的数据/结论句末用【S编号】标注（编号只能用给定的）。\n"
            "3. 只输出该小节中文 Markdown 正文，不要重复小节标题、不要单列『来源』段。",
        },
        {
            "role": "user",
            "content": f"项目设定：\n{profile}\n\n小节标题：{title}\n\n写作要求：\n{req_text}\n\n"
            f"联网出处：\n{source_block}\n\n请撰写：",
        },
    ]
    result = await provider.complete_until_done(messages)
    return result.content.strip()


async def source_only_fill(
    provider: LLMProvider, title: str, reqs: list[str], source_text: str
) -> str:
    req_text = "\n".join(f"- {r}" for r in reqs) or "（无额外要求）"
    messages = [
        {
            "role": "system",
            "content": COMPLIANCE_PROMPT
            + "\n\n你是文档改写助手。只用『源文件内容』填写指定小节，按写作要求组织。"
            "严格规则：只用源文件中的信息，不联网；句末标（源文件）；"
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
            "content": COMPLIANCE_PROMPT
            + "\n\n你是行业研究报告撰写助手。基于『源文件』和『联网出处』撰写指定小节。"
            "额外规则：\n"
            "1. 联网出处的数据/结论句末用【S编号】标注（编号只能用给定的）。\n"
            "2. 源文件内容句末标（源文件）。\n"
            "3. 只输出该小节中文 Markdown 正文，不要重复小节标题、不要单列『来源』段（来源全文统一汇总）。",
        },
        {
            "role": "user",
            "content": f"小节标题：{title}\n\n写作要求：\n{req_text}\n\n源文件内容：\n{source_text}\n\n"
            f"联网出处：\n{source_block}\n\n请撰写：",
        },
    ]
    result = await provider.complete_until_done(messages)
    return result.content.strip()


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


async def _search_and_collect(
    web_search,
    queries: list[str],
    seen_urls: set[str],
    all_sources: list[dict],
    max_sources: int,
) -> list[dict]:
    section_sources: list[dict] = []
    for q in queries:
        try:
            results = await web_search.run(query=q)
        except Exception:
            continue
        for r in results:
            link = r.get("url") or ""
            if not link or link in seen_urls:
                continue
            seen_urls.add(link)
            entry = {
                "id": f"S{len(all_sources) + 1}",
                "title": r.get("title", ""),
                "url": link,
                "content": (r.get("content") or "").strip(),
                "date": r.get("date") or "",
            }
            all_sources.append(entry)
            section_sources.append(entry)
            if len(section_sources) >= max_sources:
                break
        if len(section_sources) >= max_sources:
            break
    return section_sources


def format_verification_summary(verdicts: list[dict]) -> str:
    if not verdicts:
        return ""
    lines = ["## 引用核验摘要"]
    counts: dict[str, int] = {}
    for v in verdicts:
        verdict = v.get("verdict", "?")
        counts[verdict] = counts.get(verdict, 0) + 1
        lines.append(f"- [{verdict}] {v.get('citation', '')} {v.get('claim', '')}")
    lines.append(f"\n核验统计：{counts}")
    return "\n".join(lines)


async def run_research_from_profile(
    *,
    provider: LLMProvider,
    web_search,
    template_path: Path,
    profile: str,
    output_path: Path,
    queries_per_section: int,
    max_sources: int,
    on_progress: NotifyFn,
) -> tuple[str, list[dict]]:
    nodes = parse_template_nodes(template_path)
    content_nodes = [n for n in nodes if n["reqs"]]
    await on_progress(f"模板解析出 {len(nodes)} 个节点，其中 {len(content_nodes)} 个需检索填充。")

    md_parts: list[str] = []
    all_sources: list[dict] = []
    seen_urls: set[str] = set()
    all_verdicts: list[dict] = []

    for idx, node in enumerate(nodes, start=1):
        md_parts.append("#" * min(node["level"], 6) + " " + node["title"])
        if not node["reqs"]:
            continue

        title = node["title"]
        await on_progress(f"[{idx}/{len(nodes)}] 生成检索词：{title}")
        queries = await generate_queries_from_profile(
            provider, title, node["reqs"], profile, queries_per_section
        )

        await on_progress(f"[{idx}/{len(nodes)}] 联网检索：{title}")
        section_sources = await _search_and_collect(
            web_search, queries, seen_urls, all_sources, max_sources
        )

        await on_progress(f"[{idx}/{len(nodes)}] 带源改写：{title}")
        if section_sources:
            content = await fill_from_web(provider, title, node["reqs"], section_sources, profile)
            verdicts = await verify_citations(provider, content, section_sources)
            all_verdicts.extend(verdicts)
        else:
            content = "（未检索到与目标公司相关的可靠来源）"
        md_parts.append(content)

    if all_sources:
        md_parts.append("# 来源")
        for s in all_sources:
            md_parts.append(f"{s['id']} — {s['title']} — {s['url']}")

    markdown = "\n\n".join(md_parts)
    render_markdown_to_docx(markdown, template_path, output_path)
    return markdown, all_verdicts


async def run_research_with_source(
    *,
    provider: LLMProvider,
    web_search,
    template_path: Path,
    source_text: str,
    output_path: Path,
    queries_per_section: int,
    max_sources: int,
    on_progress: NotifyFn,
) -> tuple[str, list[dict]]:
    nodes = parse_template_nodes(template_path)
    grounded_count = sum(n["grounded"] for n in nodes)
    await on_progress(
        f"模板解析出 {len(nodes)} 个节点，其中 {grounded_count} 个需联网填充。"
    )

    md_parts: list[str] = []
    all_sources: list[dict] = []
    seen_urls: set[str] = set()
    all_verdicts: list[dict] = []

    for idx, node in enumerate(nodes, start=1):
        title = node["title"]
        level = node["level"]
        md_parts.append("#" * min(level, 6) + " " + title)
        if not node["reqs"]:
            continue

        if node["grounded"]:
            await on_progress(f"[{idx}/{len(nodes)}] 生成检索词：{title}")
            queries = await generate_queries_with_source(
                provider, title, node["reqs"], source_text, queries_per_section
            )

            await on_progress(f"[{idx}/{len(nodes)}] 联网检索：{title}")
            section_sources = await _search_and_collect(
                web_search, queries, seen_urls, all_sources, max_sources
            )

            await on_progress(f"[{idx}/{len(nodes)}] 带源改写：{title}")
            if section_sources:
                content = await grounded_fill(
                    provider, title, node["reqs"], source_text, section_sources
                )
                verdicts = await verify_citations(provider, content, section_sources)
                all_verdicts.extend(verdicts)
            else:
                content = await source_only_fill(provider, title, node["reqs"], source_text)
            md_parts.append(content)
        else:
            await on_progress(f"[{idx}/{len(nodes)}] 源文件填充：{title}")
            content = await source_only_fill(provider, title, node["reqs"], source_text)
            md_parts.append(content)

    if all_sources:
        md_parts.append("# 来源")
        for s in all_sources:
            md_parts.append(f"{s['id']} — {s['title']} — {s['url']}")

    markdown = "\n\n".join(md_parts)
    render_markdown_to_docx(markdown, template_path, output_path)
    return markdown, all_verdicts
