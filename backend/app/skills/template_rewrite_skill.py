from __future__ import annotations

import re
from pathlib import Path

from app.services.document_parser import parse_document
from app.services.llm_provider import TokenUsage
from app.services.template_docx import extract_template_outline, render_markdown_to_docx
from app.skills.base import SkillButton, SkillContext

SOURCE_CHAR_BUDGET = 36_000
CONDENSE_CHUNK_CHARS = 14_000

REWRITE_SYSTEM_PROMPT = """你是专业文档改写助手。任务：把「源文件内容」按「目标模板结构」\
重新组织、改写成一篇成稿。

关于模板结构的约定：
- 以 #、##、### 给出的是模板的章节标题与层级（每次模板都不同，必须照搬，不要套用任何固定模板）。
- 以「> 填写要求：」开头的行是该节的写作指引，告诉你这一节该写什么；它不是正文内容，
  不要原样抄进成稿，而要按其要求、用源文件里的事实去填写。

必须严格遵守：
1. 严格遵循模板给出的章节标题与层级，不增删、不重命名、不调整顺序。
2. 每一节的内容只能来自源文件，不得臆造、补全或编造源文件中不存在的信息。
3. 必须忠实保留源文件中的关键数据、数值、单位、名称、日期，不得篡改或四舍五入。
4. 若源文件缺少某节所需信息，该节正文只写「（源文件中未提供相应内容）」，不要用常识或外部知识补写。
5. 语言精炼、书面化，做合理的归纳与组织，但不改变事实。

输出格式（Markdown）：
- 模板的章节标题用对应级别的 #、##、### 等，与输入层级一致。
- 正文用普通段落；并列要点用 - 列表；数据对照用 Markdown 表格。
- 只输出改写后的成稿正文本身，不要任何解释、前言或结束语。
"""

CONDENSE_SYSTEM_PROMPT = """你是资料整理助手。请把给定的材料片段压缩成保真的事实笔记，\
用于后续按模板改写。要求：保留所有关键数据、数值、单位、名称、日期、结论；不得臆造、\
不得补全原文没有的信息；输出简洁的中文要点（可用 - 列表），不做评价、不加建议。"""


def _chunk_text(text: str, chunk_chars: int) -> list[str]:
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        if current and current_len + len(para) > chunk_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += len(para)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


async def condense_source(text: str, provider, budget: int) -> tuple[str, TokenUsage]:
    usage = TokenUsage()
    if len(text) <= budget:
        return text, usage

    chunks = _chunk_text(text, CONDENSE_CHUNK_CHARS)
    notes: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        result = await provider.complete(
            [
                {"role": "system", "content": CONDENSE_SYSTEM_PROMPT},
                {"role": "user", "content": f"材料片段 {index}/{len(chunks)}：\n{chunk}"},
            ]
        )
        notes.append(result.content.strip())
        usage += result.usage
    combined = "\n\n".join(notes)
    if len(combined) > budget:
        combined = combined[:budget]
    return combined, usage


async def rewrite_to_markdown(outline: str, source_text: str, provider) -> tuple[str, TokenUsage]:
    user_prompt = (
        f"目标模板结构：\n{outline}\n\n"
        f"源文件内容：\n{source_text}\n\n"
        "请严格按上述模板结构，用源文件内容改写输出 Markdown 成稿。"
    )
    result = await provider.complete_until_done(
        [
            {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
    )
    return result.content, result.usage


def _find_file_entry(conversation_store, open_id: str, file_id: str) -> dict | None:
    for entry in conversation_store.list_files(open_id):
        if entry.get("entry_key") == file_id or entry.get("file_id") == file_id:
            return entry
    return None


def _load_source_text_from_file(entry: dict) -> str:
    from app.services.qa_service import load_pages

    pages = load_pages(entry.get("pages_path", ""))
    if not pages:
        return ""
    return "\n\n".join(f"--- 第 {p['page_number']} 页 ---\n{p['text']}" for p in pages)


class TemplateRewriteSkill:
    skill_id = "template_rewrite"
    needs_confirm = False

    def button(self) -> SkillButton:
        return SkillButton(
            label="按模板改写",
            show_in_menu=True,
            show_in_next_step=True,
        )

    async def run(
        self,
        *,
        ctx: SkillContext,
        operator_id: str | None,
        chat_id: str | None,
        args: dict,
    ) -> None:
        if not operator_id:
            return

        file_id = args.get("file_id")
        if file_id:
            ctx.session_store.set_active(
                operator_id,
                self.skill_id,
                awaiting="template_file",
                args={"source_file_id": file_id},
            )
            await ctx.client.send_text(operator_id, "请发送【模板文件】(.docx)。")
            return

        ctx.session_store.set_active(
            operator_id,
            self.skill_id,
            awaiting="source_file",
            args={},
        )
        await ctx.client.send_text(
            operator_id,
            "请先发送【源文件】（内容来源，支持 PDF/Word/Excel/CSV）。",
        )

    async def resume(
        self,
        *,
        ctx: SkillContext,
        msg,
        state: dict,
    ) -> bool:
        awaiting = state.get("awaiting")
        args = dict(state.get("args") or {})
        sender_id = msg.sender_id
        if not sender_id:
            return True

        if awaiting == "source_file":
            if msg.msg_type != "file" or not msg.file_key:
                await ctx.client.reply_text(msg.message_id, "请发送源文件（PDF/Word/Excel/CSV）。")
                ctx.session_store.set_active(sender_id, self.skill_id, awaiting="source_file", args=args)
                return True

            from app.workers.feishu_ws import check_upload_allowed

            reject = check_upload_allowed(ctx.settings, msg.file_name, msg.file_size)
            if reject:
                await ctx.client.reply_text(msg.message_id, reject)
                ctx.session_store.set_active(sender_id, self.skill_id, awaiting="source_file", args=args)
                return True

            storage_dir = Path(ctx.settings.local_storage_dir) / f"rewrite_{sender_id}"
            storage_dir.mkdir(parents=True, exist_ok=True)
            safe_name = msg.file_name or "source-file"
            target_path = storage_dir / safe_name
            await ctx.client.download_message_file(msg.message_id, msg.file_key, target_path)
            parsed = parse_document(target_path)
            source_text_path = storage_dir / "source_text.txt"
            source_text_path.write_text(parsed.text, encoding="utf-8")

            args["source_text_path"] = str(source_text_path)
            ctx.session_store.set_active(
                sender_id,
                self.skill_id,
                awaiting="template_file",
                args=args,
            )
            await ctx.client.reply_text(
                msg.message_id,
                "已收到源文件，请再发送【模板文件】(.docx)。",
            )
            return True

        if awaiting == "template_file":
            if msg.msg_type != "file" or not msg.file_key:
                await ctx.client.reply_text(msg.message_id, "请发送模板文件 (.docx)。")
                ctx.session_store.set_active(sender_id, self.skill_id, awaiting="template_file", args=args)
                return True

            suffix = Path(msg.file_name or "").suffix.lower()
            if suffix != ".docx":
                await ctx.client.reply_text(msg.message_id, "模板必须是 .docx 文件，请重新发送。")
                ctx.session_store.set_active(sender_id, self.skill_id, awaiting="template_file", args=args)
                return True

            storage_dir = Path(ctx.settings.local_storage_dir) / f"rewrite_{sender_id}"
            storage_dir.mkdir(parents=True, exist_ok=True)
            template_path = storage_dir / (msg.file_name or "template.docx")
            await ctx.client.download_message_file(msg.message_id, msg.file_key, template_path)

            source_text = ""
            if args.get("source_text_path"):
                source_text = Path(args["source_text_path"]).read_text(encoding="utf-8")
            elif args.get("source_file_id"):
                entry = _find_file_entry(ctx.conversation_store, sender_id, args["source_file_id"])
                if entry:
                    source_text = _load_source_text_from_file(entry)

            if not source_text.strip():
                await ctx.client.reply_text(msg.message_id, "源文件内容不可用，请重新开始。")
                return False

            await self._run_pipeline(ctx, sender_id, template_path, source_text, msg.file_name or "template.docx")
            return False

        return False

    async def _run_pipeline(
        self,
        ctx: SkillContext,
        operator_id: str,
        template_path: Path,
        source_text: str,
        template_name: str,
    ) -> None:
        from app.services.llm_provider import LLMConfig, build_chat_provider

        if not ctx.settings.llm_api_key:
            await ctx.client.send_text(operator_id, "未配置 LLM_API_KEY，无法改写。")
            return

        provider = build_chat_provider(
            LLMConfig(
                provider=ctx.settings.llm_provider,
                base_url=ctx.settings.llm_base_url,
                api_key=ctx.settings.llm_api_key,
                model=ctx.settings.llm_model,
                timeout_ms=ctx.settings.llm_timeout_ms,
                max_tokens=ctx.settings.llm_max_tokens,
                temperature=ctx.settings.llm_temperature,
            ),
            ctx.settings.fallback_models,
        )

        async def notify(text: str) -> None:
            await ctx.client.send_text(operator_id, text)

        await notify("[1/4] 解析模板结构…")
        outline = extract_template_outline(template_path)

        await notify("[2/4] 准备源文件内容…")
        condensed, _ = await condense_source(source_text, provider, SOURCE_CHAR_BUDGET)

        await notify(f"[3/4] 调用模型 {ctx.settings.llm_model} 按模板改写…")
        markdown, _ = await rewrite_to_markdown(outline, condensed, provider)

        output_path = template_path.parent / f"{Path(template_name).stem}_改写.docx"
        await notify("[4/4] 渲染 docx…")
        render_markdown_to_docx(markdown, template_path, output_path)

        await notify("按模板改写完成，正在发送附件…")
        await ctx.client.send_file(operator_id, output_path, output_path.name)
