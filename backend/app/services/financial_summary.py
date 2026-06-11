import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.services.analysis_cache import AnalysisCache, ChunkCacheKey, sha256_text
from app.services.document_parser import ParsedDocument, ParsedPage
from app.services.llm_provider import LLMProvider, TokenUsage

ProgressCallback = Callable[[str], Awaitable[None]] | None


@dataclass(frozen=True)
class TextChunk:
    index: int
    start_page: int
    end_page: int
    page_count: int
    text: str


@dataclass(frozen=True)
class ChunkPlan:
    chunks: list["TextChunk"]
    analyzed_pages: int
    total_pages: int
    truncated: bool


@dataclass(frozen=True)
class FinancialSummaryResult:
    markdown: str
    usage: TokenUsage
    cache_hits: int = 0
    cache_misses: int = 0
    analyzed_pages: int = 0
    total_pages: int = 0
    truncated: bool = False


SYSTEM_PROMPT = """你是服务于四大投资建议/交易咨询人员的材料分析助理。
你的任务是整理材料中的客观财务和经营信息，用于辅助用户后续自行判断是否投资。

必须严格遵守：
1. 只反馈材料中出现的客观内容。
2. 不得臆造、补全、推断材料中不存在的信息。
3. 所有观点、数据、摘要尽量标注来源页码、章节、表格或 Sheet。
4. 如果材料中没有明确依据，写“未在材料中发现”或“不足以判断”。
5. 不输出投资建议、审计意见、法律意见或确定性风险结论。
6. 输出使用中文 Markdown。
"""


MERGE_PROMPT = """请把以下多段「材料整理结果」合并为一段连贯的整理结果。

要求：
- 保留每条信息的来源页码标记（如「[第 X 页]」「第 X 页」）。
- 不要臆造、不要补全原文没有的信息。
- 不要丢弃客观数据；同类指标可归并罗列，但不得删除不同期间/主体/来源页的数值。
- 只做合并与组织，不要新增结论或投资建议。
- 输出中文 Markdown 片段（不需要完整报告结构）。

待合并的整理结果：
{notes}
"""


def build_chunks(
    pages: list[ParsedPage],
    chunk_chars: int,
    max_pages: int,
    max_chunks: int,
) -> ChunkPlan:
    """把逻辑页打包成分析片段，按页数封顶（不切断单页）。

    页数才是绑定约束：只取前 max_pages 个有内容的页参与分析，超出部分不丢失地标记为
    truncated，由上层在报告中显式提示。max_chunks 仅作防跑飞硬上限。
    """
    non_empty = [page for page in pages if page.text.strip()]
    total_pages = len(non_empty)
    budget = non_empty[:max_pages] if max_pages > 0 else non_empty

    chunks: list[TextChunk] = []
    current_pages: list[ParsedPage] = []
    current_len = 0

    for page in budget:
        text = page.text.strip()

        if current_pages and current_len + len(text) > chunk_chars:
            chunks.append(_make_chunk(len(chunks) + 1, current_pages))
            current_pages = []
            current_len = 0

            if len(chunks) >= max_chunks:
                break

        current_pages.append(page)
        current_len += len(text)

    if current_pages and len(chunks) < max_chunks:
        chunks.append(_make_chunk(len(chunks) + 1, current_pages))

    analyzed_pages = sum(chunk.page_count for chunk in chunks)
    return ChunkPlan(
        chunks=chunks,
        analyzed_pages=analyzed_pages,
        total_pages=total_pages,
        truncated=analyzed_pages < total_pages,
    )


def _make_chunk(index: int, pages: list[ParsedPage]) -> TextChunk:
    start_page = pages[0].page_number
    end_page = pages[-1].page_number
    text = "\n\n".join(f"[第 {page.page_number} 页]\n{page.text}" for page in pages)
    return TextChunk(
        index=index,
        start_page=start_page,
        end_page=end_page,
        page_count=len(pages),
        text=text,
    )


async def generate_financial_summary_markdown(
    document: ParsedDocument,
    provider: LLMProvider,
    chunk_chars: int,
    max_pages: int,
    max_chunks: int,
    prompt_version: str,
    file_hash: str,
    reduce_group_size: int,
    reduce_max_chars: int,
    map_concurrency: int,
    cache: AnalysisCache | None = None,
    on_progress: ProgressCallback = None,
) -> FinancialSummaryResult:
    plan = build_chunks(
        document.pages,
        chunk_chars=chunk_chars,
        max_pages=max_pages,
        max_chunks=max_chunks,
    )
    chunks = plan.chunks
    if not chunks:
        return FinancialSummaryResult(
            markdown=_empty_text_report(document),
            usage=TokenUsage(),
            total_pages=plan.total_pages,
        )

    total = len(chunks)
    sem = asyncio.Semaphore(max(1, map_concurrency))

    def make_key(chunk: TextChunk) -> ChunkCacheKey | None:
        if cache is None:
            return None
        return ChunkCacheKey(
            prompt_version=prompt_version,
            chunk_chars=chunk_chars,
            file_hash=file_hash,
            chunk_index=chunk.index,
            chunk_hash=sha256_text(chunk.text),
        )

    # Map：每个片段并发分析（受 map_concurrency 限流），保留缓存与进度。
    map_results = await asyncio.gather(
        *(
            _map_one(
                chunk,
                provider,
                cache,
                make_key(chunk),
                sem,
                on_progress,
                total,
                document.file_path.name,
            )
            for chunk in chunks
        )
    )

    chunk_notes = [result.markdown for result, _ in map_results]
    usage = TokenUsage()
    for result, _ in map_results:
        usage += result.usage
    cache_hits = sum(1 for _, hit in map_results if hit)
    cache_misses = total - cache_hits

    # Reduce：分层归并，避免一次性把所有片段笔记塞进单次合成。
    reduced = await reduce_notes(
        document,
        chunk_notes,
        provider,
        group_size=reduce_group_size,
        chunk_count=total,
        concurrency=map_concurrency,
        max_input_chars=reduce_max_chars,
        on_progress=on_progress,
    )

    markdown = reduced.markdown
    if plan.truncated:
        markdown = _truncation_notice(plan, max_pages) + markdown

    return FinancialSummaryResult(
        markdown=markdown,
        usage=usage + reduced.usage,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        analyzed_pages=plan.analyzed_pages,
        total_pages=plan.total_pages,
        truncated=plan.truncated,
    )


async def _map_one(
    chunk: TextChunk,
    provider: LLMProvider,
    cache: AnalysisCache | None,
    cache_key: ChunkCacheKey | None,
    sem: asyncio.Semaphore,
    on_progress: ProgressCallback,
    total: int,
    source_file: str,
) -> tuple[FinancialSummaryResult, bool]:
    if cache is not None and cache_key is not None:
        cached = cache.get_chunk(cache_key)
        if cached is not None:
            if on_progress:
                await on_progress(f"片段 {chunk.index}/{total} 命中缓存，跳过模型调用。")
            return FinancialSummaryResult(markdown=cached.markdown, usage=TokenUsage()), True

    async with sem:
        if on_progress:
            pages = f"第 {chunk.start_page}-{chunk.end_page} 页"
            await on_progress(f"正在分析片段 {chunk.index}/{total}（{pages}）...")
        result = await summarize_chunk(chunk, provider)

    if cache is not None and cache_key is not None:
        cache.set_chunk(
            cache_key,
            markdown=result.markdown,
            usage=result.usage,
            metadata={
                "source_file": source_file,
                "start_page": chunk.start_page,
                "end_page": chunk.end_page,
                "provider": provider.config.provider,
                "model": provider.config.model,
            },
        )
    return result, False


# 拼接每段笔记时附加的标题/分隔符开销（如「## 待合并整理 N\n」与「\n\n---\n\n」），
# 估算偏大以留安全余量。
_NOTE_JOIN_OVERHEAD = 30


def _notes_total_len(notes: list[str]) -> int:
    return sum(len(note) + _NOTE_JOIN_OVERHEAD for note in notes)


def _pack_groups(notes: list[str], group_size: int, max_chars: int) -> list[list[str]]:
    """把笔记按「条数上限 group_size」和「拼接后字符预算 max_chars」双重约束打包成多组。

    任一约束先到即开新组，确保每组拼起来不超出模型单次输入预算。
    单段笔记本身受 map/merge 的 max_tokens 输出上限约束，不会超过 max_chars，故每组至少含 1 段。
    """
    groups: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for note in notes:
        note_len = len(note) + _NOTE_JOIN_OVERHEAD
        if current and (len(current) >= group_size or current_len + note_len > max_chars):
            groups.append(current)
            current = []
            current_len = 0
        current.append(note)
        current_len += note_len
    if current:
        groups.append(current)
    return groups


async def reduce_notes(
    document: ParsedDocument,
    notes: list[str],
    provider: LLMProvider,
    group_size: int,
    chunk_count: int,
    concurrency: int,
    max_input_chars: int,
    on_progress: ProgressCallback = None,
) -> FinancialSummaryResult:
    """分层归并：笔记数超过 group_size 或拼接后超出字符预算时，先分组合并，逐层收敛后再终合。

    除条数外再按 max_input_chars 字符预算封顶，确保任何一次归并/合成调用的输入都不超过
    模型输入长度上限（如 dashscope 30720）。
    """
    usage = TokenUsage()
    group_size = max(2, group_size)
    # 给最终合成的报告结构模板与文件元信息留余量，避免拼好的笔记 + 模板再次超限。
    fit_budget = max(2000, max_input_chars - 2000)
    level = 0

    while len(notes) > 1 and (
        len(notes) > group_size or _notes_total_len(notes) > fit_budget
    ):
        groups = _pack_groups(notes, group_size, fit_budget)
        if len(groups) >= len(notes):
            # 无法进一步收敛（单段已接近预算上限），停止分层避免死循环，交给最终合成尽力而为。
            break
        level += 1
        if on_progress:
            await on_progress(
                f"正在分层归并（第 {level} 层，{len(notes)} 段 → {len(groups)} 组）..."
            )
        sem = asyncio.Semaphore(max(1, concurrency))
        merged = await asyncio.gather(
            *(_merge_group(group, provider, sem) for group in groups)
        )
        notes = [result.markdown for result in merged]
        for result in merged:
            usage += result.usage

    if on_progress:
        await on_progress("正在合成最终 Markdown 报告...")

    final_result = await synthesize_final_report(document, notes, provider, chunk_count)
    return FinancialSummaryResult(
        markdown=final_result.markdown,
        usage=usage + final_result.usage,
    )


async def _merge_group(
    notes: list[str],
    provider: LLMProvider,
    sem: asyncio.Semaphore,
) -> FinancialSummaryResult:
    if len(notes) == 1:
        return FinancialSummaryResult(markdown=notes[0], usage=TokenUsage())
    joined = "\n\n---\n\n".join(
        f"## 待合并整理 {index + 1}\n{note}" for index, note in enumerate(notes)
    )
    user_prompt = MERGE_PROMPT.format(notes=joined)
    async with sem:
        result = await provider.complete_until_done(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
        )
    return FinancialSummaryResult(markdown=result.content, usage=result.usage)


def _truncation_notice(plan: ChunkPlan, max_pages: int) -> str:
    return (
        f"> ⚠️ 本文件共 {plan.total_pages} 页，受 `llm_max_pages={max_pages}` 限制，"
        f"本报告仅分析了前 {plan.analyzed_pages} 页。后续追问检索仍覆盖全文。\n\n"
    )


async def summarize_chunk(chunk: TextChunk, provider: LLMProvider) -> FinancialSummaryResult:
    user_prompt = f"""请整理以下材料片段中的客观财务/经营信息。

片段范围：第 {chunk.start_page} 页至第 {chunk.end_page} 页

输出要求：
- 只列材料中明确出现的信息。
- 提取主要观点及其页码。
- 提取客观数据，尽量包含指标名、数值、单位、期间、主体、来源页码。
- 不要给投资建议，不要臆造。

材料片段：
{chunk.text}
"""
    result = await provider.complete(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
    )
    return FinancialSummaryResult(markdown=result.content, usage=result.usage)


async def synthesize_final_report(
    document: ParsedDocument,
    chunk_notes: list[str],
    provider: LLMProvider,
    chunk_count: int,
) -> FinancialSummaryResult:
    notes = "\n\n---\n\n".join(
        f"## 片段 {index + 1} 整理结果\n{note}" for index, note in enumerate(chunk_notes)
    )
    user_prompt = f"""请基于以下分片整理结果，生成最终 Markdown 报告。

文件名：{document.file_path.name}
文件类型：{document.file_type}
页数：{document.page_count}
已分析片段数：{chunk_count}

最终报告必须包含且只包含以下一级模块：
1. 文件概览
2. 一句话摘要
3. 财务要点摘要
4. 全文概要
5. 客观数据表
6. 未在材料中发现或不足以判断的信息
7. 说明

关键要求：
- 全文概要要列出主要观点，以及观点所在页码/章节。
- 客观数据表必须是 Markdown 表格，列为：指标名、数值、单位、期间、主体、来源位置。
- 不得臆造。没有依据就写“未在材料中发现”或“不足以判断”。
- 说明中必须写明：本报告仅整理材料中的客观信息，不构成投资建议。

分片整理结果：
{notes}
"""
    result = await provider.complete_until_done(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
    )
    return FinancialSummaryResult(markdown=result.content, usage=result.usage)


def _empty_text_report(document: ParsedDocument) -> str:
    return "\n".join(
        [
            "# 材料分析失败",
            "",
            "## 文件概览",
            f"- 文件名：`{document.file_path.name}`",
            f"- 文件类型：`{document.file_type}`",
            f"- 页数：`{document.page_count if document.page_count is not None else '未知'}`",
            "",
            "## 说明",
            "未能从材料中提取到可复制文本。该文件可能是扫描件或图片型 PDF。",
        ]
    )
