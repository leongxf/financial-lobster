from __future__ import annotations

from app.skills.base import SkillButton, SkillContext


class QASkill:
    skill_id = "qa"
    needs_confirm = False

    def button(self) -> SkillButton:
        return SkillButton(
            label="追问文件内容",
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
        question = args.get("question")
        message_id = args.get("message_id")

        if not question:
            if operator_id:
                ctx.session_store.set_active(
                    operator_id,
                    self.skill_id,
                    awaiting="question",
                    args={"file_id": args.get("file_id")},
                )
                await ctx.client.send_text(operator_id, "请直接发文字向我提问。")
            return

        if not operator_id or not message_id:
            return

        from app.workers.feishu_ws import process_question_async

        await process_question_async(
            ctx.settings,
            message_id=message_id,
            sender_id=operator_id,
            question=question,
        )

    async def resume(
        self,
        *,
        ctx: SkillContext,
        msg,
        state: dict,
    ) -> bool:
        if state.get("awaiting") != "question" or not msg.text.strip():
            if msg.sender_id:
                await ctx.client.reply_text(msg.message_id, "请输入有效的问题文字。")
                ctx.session_store.set_active(
                    msg.sender_id,
                    self.skill_id,
                    awaiting="question",
                    args=state.get("args", {}),
                )
            return True

        from app.workers.feishu_ws import process_question_async

        await process_question_async(
            ctx.settings,
            message_id=msg.message_id,
            sender_id=msg.sender_id,
            question=msg.text.strip(),
        )
        return False
