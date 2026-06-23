from __future__ import annotations

from app.services.cards import (
    build_file_upload_prompt_card,
    build_next_step_card,
)
from app.skills.base import SkillButton, SkillContext


class FinancialSummarySkill:
    skill_id = "financial_summary"
    needs_confirm = False

    def button(self) -> SkillButton:
        return SkillButton(
            label="文件摘要",
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

        if message_id and file_key:
            await process_file_message_async(
                ctx.settings,
                message_id=message_id,
                file_key=file_key,
                file_name=args.get("file_name"),
                sender_id=operator_id,
                file_size=args.get("file_size"),
            )
            await _maybe_send_next_step(ctx, operator_id, message_id)
            return

        if not operator_id:
            return

        ctx.session_store.set_active(
            operator_id,
            self.skill_id,
            awaiting="file",
            args={},
        )
        await ctx.client.send_card(operator_id, build_file_upload_prompt_card())

    async def resume(
        self,
        *,
        ctx: SkillContext,
        msg,
        state: dict,
    ) -> bool:
        if state.get("awaiting") != "file" or not msg.sender_id:
            return False

        if msg.msg_type != "file" or not msg.file_key:
            await ctx.client.reply_text(
                msg.message_id,
                "请发送文件（PDF / Word / Excel / CSV）。",
            )
            ctx.session_store.set_active(
                msg.sender_id,
                self.skill_id,
                awaiting="file",
                args=state.get("args") or {},
            )
            return True

        from app.workers.feishu_ws import check_upload_allowed, process_file_message_async

        reject = check_upload_allowed(ctx.settings, msg.file_name, msg.file_size)
        if reject:
            await ctx.client.reply_text(msg.message_id, reject)
            ctx.session_store.set_active(
                msg.sender_id,
                self.skill_id,
                awaiting="file",
                args=state.get("args") or {},
            )
            return True

        await process_file_message_async(
            ctx.settings,
            message_id=msg.message_id,
            file_key=msg.file_key,
            file_name=msg.file_name,
            sender_id=msg.sender_id,
            file_size=msg.file_size,
        )
        await _maybe_send_next_step(ctx, msg.sender_id, msg.message_id)
        return False


async def _maybe_send_next_step(
    ctx: SkillContext,
    operator_id: str | None,
    message_id: str | None,
) -> None:
    if not operator_id or not message_id or not ctx.registry:
        return
    files = ctx.conversation_store.list_files(operator_id)
    if not files:
        return
    entry_key = files[0].get("entry_key") or files[0].get("file_id")
    if not entry_key:
        return
    card = build_next_step_card(entry_key, ctx.registry.next_step_buttons())
    await ctx.client.reply_card(message_id, card)
