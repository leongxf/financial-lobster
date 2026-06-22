from __future__ import annotations

from app.skills.base import SkillButton, SkillContext
from app.services.cards import build_next_step_card


class FinancialSummarySkill:
    skill_id = "financial_summary"
    needs_confirm = False

    def button(self) -> SkillButton:
        return SkillButton(
            label="深入财务分析",
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
        from app.workers.feishu_ws import process_file_message_async

        message_id = args.get("message_id")
        file_key = args.get("file_key")

        if not file_key and args.get("file_id"):
            task = ctx.task_store.read(str(args["file_id"]))
            file_key = task.get("file_key")
            message_id = message_id or task.get("message_id") or args.get("file_id")
            if not args.get("file_name"):
                args = {**args, "file_name": task.get("file_name")}

        if not message_id or not file_key:
            if operator_id:
                await ctx.client.send_text(operator_id, "缺少文件信息，请重新发送文件。")
            return

        await process_file_message_async(
            ctx.settings,
            message_id=message_id,
            file_key=file_key,
            file_name=args.get("file_name"),
            sender_id=operator_id,
            file_size=args.get("file_size"),
        )

        if operator_id and ctx.registry:
            files = ctx.conversation_store.list_files(operator_id)
            if files:
                entry_key = files[0].get("entry_key") or files[0].get("file_id")
                if entry_key:
                    card = build_next_step_card(
                        entry_key,
                        ctx.registry.next_step_buttons(),
                    )
                    await ctx.client.reply_card(message_id, card)

    async def resume(self, *, ctx: SkillContext, msg, state: dict) -> None:
        pass
