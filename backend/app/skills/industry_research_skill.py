from __future__ import annotations

import re
from pathlib import Path

from app.services.cards import (
    build_confirm_card,
    build_research_target_input_card,
    build_template_select_card,
)
from app.services.industry_research import (
    build_profile,
    format_verification_summary,
    run_research_from_profile,
    run_research_with_source,
)
from app.services.llm_provider import LLMConfig, build_chat_provider
from app.services.qa_service import load_pages
from app.services.templates import list_research_templates, resolve_template_path
from app.skills.base import SkillButton, SkillContext

REPORT_TEXT_MAX_CHARS = 3500


def _find_file_entry(conversation_store, open_id: str, file_id: str) -> dict | None:
    for entry in conversation_store.list_files(open_id):
        if entry.get("entry_key") == file_id or entry.get("file_id") == file_id:
            return entry
    return None


def _load_source_text_from_file(entry: dict) -> str:
    pages = load_pages(entry.get("pages_path", ""))
    if not pages:
        return ""
    return "\n\n".join(
        f"--- 第 {p['page_number']} 页 ---\n{p['text']}" for p in pages
    )


class IndustryResearchSkill:
    skill_id = "industry_research"
    needs_confirm = True

    def button(self) -> SkillButton:
        return SkillButton(
            label="行业研究",
            primary=True,
            show_in_menu=True,
            show_in_next_step=True,
        )

    def should_confirm(self, args: dict) -> bool:
        # 仅当「模板 + 目标」都齐备时才弹确认卡；否则先让 run() 引导选模板/填目标。
        return bool(args.get("template")) and bool(
            args.get("file_id") or args.get("company")
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

        if not ctx.settings.search_key:
            await ctx.client.send_text(
                operator_id,
                "未配置联网检索 API Key（SEARCH_API_KEY 或 QA_EMBEDDING_API_KEY），无法执行行业研究。",
            )
            return

        templates = list_research_templates(ctx.settings)
        if not templates:
            await ctx.client.send_text(
                operator_id,
                "未配置行业研究模板：请把模板 docx 放到 RESEARCH_TEMPLATE_DIR 目录后重试。",
            )
            return

        # 第一步：尚未选模板 → 下发模板选择卡（透传已有 file_id 等上下文）。
        # 即使只有一个模板也展示选择卡，让用户显式确认要用哪个模板。
        if not args.get("template"):
            await ctx.client.send_card(
                operator_id,
                build_template_select_card(self.skill_id, templates, carry=args),
            )
            return

        template_path = resolve_template_path(ctx.settings, args.get("template"))
        if template_path is None:
            await ctx.client.send_text(
                operator_id,
                "所选模板已不存在，请重新选择。",
            )
            await ctx.client.send_card(
                operator_id,
                build_template_select_card(self.skill_id, templates, carry={
                    k: v for k, v in args.items() if k != "template"
                }),
            )
            return

        # 第二步：已选模板但还没研究目标 → 下发带输入框的卡，让用户在卡片内填公司名。
        # 仍 set_active 作为兜底：用户若直接在聊天框打字，resume() 也能接住。
        if not args.get("file_id") and not args.get("company"):
            ctx.session_store.set_active(
                operator_id,
                self.skill_id,
                awaiting="research_target",
                args=args,
            )
            await ctx.client.send_card(
                operator_id,
                build_research_target_input_card(
                    self.skill_id,
                    args.get("template"),
                    template_path.stem,
                    carry={k: v for k, v in args.items() if k != "template"},
                ),
            )
            return

        # 第三步：模板 + 目标齐备 → 执行。
        await self._execute_research(ctx, operator_id, args, template_path)

    async def resume(
        self,
        *,
        ctx: SkillContext,
        msg,
        state: dict,
    ) -> None:
        if state.get("awaiting") != "research_target" or not msg.sender_id:
            return

        company = msg.text.strip()
        if not company:
            await ctx.client.reply_text(msg.message_id, "请输入有效的公司名。")
            ctx.session_store.set_active(
                msg.sender_id,
                self.skill_id,
                awaiting="research_target",
                args=state.get("args", {}),
            )
            return

        merged_args = {**state.get("args", {}), "company": company}
        template_path = resolve_template_path(ctx.settings, merged_args.get("template"))
        template_line = f"模板：{template_path.stem}\n" if template_path else ""
        card = build_confirm_card(
            self.skill_id,
            "确认执行行业研究",
            f"{template_line}目标公司：{company}\n\n联网研究耗时较长且消耗 token，确认后开始执行。",
            merged_args,
        )
        await ctx.client.send_card(msg.sender_id, card)
        return False

    async def _execute_research(
        self,
        ctx: SkillContext,
        operator_id: str,
        args: dict,
        template_path: Path,
    ) -> None:
        if not ctx.settings.llm_api_key:
            await ctx.client.send_text(operator_id, "未配置 LLM_API_KEY，无法执行行业研究。")
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
        web_search = ctx.tools.get("web_search")

        storage_dir = Path(ctx.settings.local_storage_dir) / f"research_{operator_id}"
        storage_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", args.get("company") or "report")
        output_path = storage_dir / f"{safe_name}_industry_research.docx"

        async def notify(text: str) -> None:
            await ctx.client.send_text(operator_id, text)

        file_id = args.get("file_id")
        company = args.get("company")

        try:
            if file_id:
                entry = _find_file_entry(ctx.conversation_store, operator_id, file_id)
                if entry is None:
                    await notify("找不到对应文件，请重新上传后再试。")
                    return
                source_text = _load_source_text_from_file(entry)
                if not source_text.strip():
                    await notify("该文件的解析内容不可用，请重新上传。")
                    return

                await notify("[1/4] 开始基于源文件的行业研究…")
                markdown, verdicts = await run_research_with_source(
                    provider=provider,
                    web_search=web_search,
                    template_path=template_path,
                    source_text=source_text,
                    output_path=output_path,
                    queries_per_section=ctx.settings.search_queries_per_section,
                    max_sources=ctx.settings.search_max_sources,
                    on_progress=notify,
                )
            else:
                profile = build_profile(company or "", args.get("facts"))
                await notify(f"[1/4] 开始行业研究：{company}")
                markdown, verdicts = await run_research_from_profile(
                    provider=provider,
                    web_search=web_search,
                    template_path=template_path,
                    profile=profile,
                    output_path=output_path,
                    queries_per_section=ctx.settings.search_queries_per_section,
                    max_sources=ctx.settings.search_max_sources,
                    on_progress=notify,
                )

            verification = format_verification_summary(verdicts)
            full_text = markdown
            if verification:
                full_text = markdown + "\n\n---\n\n" + verification

            if len(full_text) <= REPORT_TEXT_MAX_CHARS:
                await notify("行业研究完成，报告如下：\n\n" + full_text)
            else:
                await notify(
                    "行业研究完成，报告较长，已生成 docx 附件。\n\n"
                    + (verification or "")
                )
                await ctx.client.send_file(operator_id, output_path, output_path.name)
        except Exception as exc:
            await notify(f"行业研究失败：{exc}")
