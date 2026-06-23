from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.services.cards import build_capability_menu, build_confirm_card
from app.skills.compliance import COMPLIANCE_PROMPT


@dataclass
class IncomingMessage:
    message_id: str
    sender_id: str | None
    chat_id: str | None
    msg_type: str  # "text" | "file" | ...
    text: str = ""
    file_key: str | None = None
    file_name: str | None = None
    file_size: int | None = None
    raw_payload: dict = field(default_factory=dict)


@dataclass
class SkillButton:
    label: str
    primary: bool = False
    show_in_menu: bool = True
    show_in_next_step: bool = False


@dataclass
class SkillContext:
    settings: Any
    client: Any
    tools: Any
    compliance_prompt: str
    conversation_store: Any = None
    task_store: Any = None
    session_store: Any = None
    analysis_cache: Any = None
    registry: Any = None


class Skill(Protocol):
    skill_id: str
    needs_confirm: bool

    def button(self) -> SkillButton | None: ...

    async def run(
        self,
        *,
        ctx: SkillContext,
        operator_id: str | None,
        chat_id: str | None,
        args: dict,
    ) -> None: ...

    async def resume(
        self,
        *,
        ctx: SkillContext,
        msg: IncomingMessage,
        state: dict,
    ) -> None: ...


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.skill_id] = skill

    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def menu_buttons(self) -> list[dict]:
        out = []
        for s in self._skills.values():
            b = s.button()
            if b and b.show_in_menu:
                out.append({"label": b.label, "skill_id": s.skill_id, "primary": b.primary})
        return out

    def next_step_buttons(self) -> list[dict]:
        out = []
        for s in self._skills.values():
            b = s.button()
            if b and b.show_in_next_step:
                out.append({"label": b.label, "skill_id": s.skill_id})
        return out


def _confirm_detail(skill: Skill, args: dict) -> str:
    template_line = f"模板：{args['template']}\n" if args.get("template") else ""
    if args.get("company"):
        return (
            f"{template_line}目标公司：{args['company']}\n\n"
            "联网研究耗时较长且消耗 token，确认后开始执行。"
        )
    if args.get("file_id"):
        return (
            f"{template_line}将基于您选择的文件进行联网行业研究，"
            "耗时较长且消耗 token，确认后开始执行。"
        )
    return "联网研究耗时较长且消耗 token，确认后开始执行。"


def should_confirm_before_run(skill: Skill, args: dict) -> bool:
    """needs_confirm 的 Skill 仅在参数齐备时才弹确认卡。"""
    if not skill.needs_confirm:
        return False
    checker = getattr(skill, "should_confirm", None)
    if callable(checker):
        return bool(checker(args))
    return True


class SkillRouter:
    def __init__(self, registry: SkillRegistry, ctx_factory) -> None:
        self.registry = registry
        self.ctx_factory = ctx_factory

    async def route_card_async(self, ca: CardAction) -> None:
        ctx = self.ctx_factory()
        skill = self.registry.get(ca.skill_id) if ca.skill_id else None
        if skill is None:
            if ca.operator_id:
                await ctx.client.send_text(ca.operator_id, "该功能暂不可用。")
            return

        if ca.action == "run_skill" and should_confirm_before_run(skill, ca.args):
            card = build_confirm_card(
                skill.skill_id,
                "确认执行",
                _confirm_detail(skill, ca.args),
                ca.args,
            )
            await ctx.client.send_card(ca.operator_id, card)
            return

        await skill.run(
            ctx=ctx,
            operator_id=ca.operator_id,
            chat_id=ca.chat_id,
            args=ca.args,
        )

    async def route_menu_async(self, open_id: str, event_key: str) -> None:
        """自定义菜单点击入口。约定 event_key == skill_id，直接触发该 Skill 的起始流程。"""
        if not open_id or not event_key:
            return
        ctx = self.ctx_factory()
        skill = self.registry.get(event_key)
        if skill is None:
            await ctx.client.send_text(open_id, "该功能暂不可用。")
            return
        await skill.run(ctx=ctx, operator_id=open_id, chat_id=None, args={})

    async def route_message_async(self, msg: IncomingMessage) -> None:
        ctx = self.ctx_factory()

        if msg.sender_id:
            state = ctx.session_store.get(msg.sender_id)
            if state and state.get("awaiting"):
                skill = self.registry.get(state["active_skill"])
                if skill is not None:
                    keep = await skill.resume(ctx=ctx, msg=msg, state=state)
                    if keep is not True:
                        ctx.session_store.clear(msg.sender_id)
                    return

        if msg.msg_type == "file":
            skill = self.registry.get("financial_summary")
            if skill is None:
                return
            await skill.run(
                ctx=ctx,
                operator_id=msg.sender_id,
                chat_id=msg.chat_id,
                args={
                    "message_id": msg.message_id,
                    "file_key": msg.file_key,
                    "file_name": msg.file_name,
                    "file_size": msg.file_size,
                },
            )
            return

        if msg.msg_type == "text" and msg.text:
            help_texts = {"你好", "帮助", "help", "菜单", "?", "？"}
            if msg.text.strip() in help_texts:
                await ctx.client.reply_card(
                    msg.message_id,
                    build_capability_menu(self.registry.menu_buttons()),
                )
                return

            skill = self.registry.get("qa")
            if skill is None:
                return
            await skill.run(
                ctx=ctx,
                operator_id=msg.sender_id,
                chat_id=msg.chat_id,
                args={"message_id": msg.message_id, "question": msg.text},
            )
            return
