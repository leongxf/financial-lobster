from typing import Any


def _button(label: str, value: dict, type_: str = "default") -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": type_,
        "value": value,
    }


def build_capability_menu(buttons: list[dict]) -> dict:
    """能力入口卡：buttons 为各 Skill 的按钮元数据。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "我能帮你做这些"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "点选下面的功能，或直接发文件 / 发问题给我。\n\n发文件会先确认是否进行**文件摘要**；摘要完成后才可追问。",
                },
            },
            {
                "tag": "action",
                "actions": [
                    _button(
                        b["label"],
                        {"action": "run_skill", "skill_id": b["skill_id"]},
                        "primary" if b.get("primary") else "default",
                    )
                    for b in buttons
                ],
            },
            {
                "tag": "action",
                "actions": [
                    _button("清理记忆", {"action": "clear_memory"}, "default"),
                ],
            },
        ],
    }


def build_clear_memory_confirm_card() -> dict:
    """用户主动清理会话记忆的确认卡。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "确认清理会话记忆？"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "将清除：\n"
                        "- 最近文件索引与多轮追问历史\n"
                        "- 进行中的技能会话（如行业研究输入流程）\n\n"
                        "不会删除已上传的原始文件、解析结果与分析缓存。"
                    ),
                },
            },
            {
                "tag": "action",
                "actions": [
                    _button(
                        "确认清理",
                        {"action": "confirm_clear_memory"},
                        "primary",
                    ),
                    _button("取消", {"action": "cancel"}, "default"),
                ],
            },
        ],
    }


def build_template_select_card(
    skill_id: str,
    templates: list[dict],
    carry: dict[str, Any] | None = None,
) -> dict:
    """模板选择卡：每个模板一个按钮，点选后继续走对应 Skill 流程。

    carry 用于把已有上下文（如 file_id）透传给下一步；按钮 value 仅放模板名（basename），
    由 Skill 侧在模板目录内安全解析。
    """
    carry = carry or {}
    actions = [
        _button(
            t["label"],
            {
                "action": "run_skill",
                "skill_id": skill_id,
                "template": t["name"],
                **carry,
            },
        )
        for t in templates
    ]
    actions.append(_button("取消", {"action": "cancel"}, "default"))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "请选择行业研究模板"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "点选一个模板，我会按它的提纲展开研究。"},
            },
            {"tag": "action", "actions": actions},
        ],
    }


def build_research_target_input_card(
    skill_id: str,
    template_name: str,
    template_label: str,
    carry: dict[str, Any] | None = None,
) -> dict:
    """目标输入卡：卡片内放一个输入框让用户直接填公司名并提交。

    模板名走提交按钮 value，公司名走 form_value["company"]（提交时由飞书回传）。
    carry 透传已有上下文（如 file_id）；提交后 action="run_skill" 携带 template + company。
    """
    carry = carry or {}
    submit_value = {
        "action": "run_skill",
        "skill_id": skill_id,
        "template": template_name,
        **carry,
    }
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "请输入要研究的公司"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"已选模板「{template_label}」，请在下方输入要研究的公司名。",
                },
            },
            {
                "tag": "form",
                "name": "research_target_form",
                "elements": [
                    {
                        "tag": "input",
                        "name": "company",
                        "required": True,
                        "label": {
                            "tag": "plain_text",
                            "content": "公司名",
                        },
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "请输入公司名，例如：阿里巴巴",
                        },
                    },
                    {
                        # 飞书 form 容器内按钮须直接作为 elements 子项，不能包在 action 容器里。
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "开始研究"},
                        "type": "primary",
                        "action_type": "form_submit",
                        "name": "submit",
                        "value": submit_value,
                    },
                ],
            },
            {"tag": "action", "actions": [_button("取消", {"action": "cancel"}, "default")]},
        ],
    }


def build_confirm_card(skill_id: str, title: str, detail: str, args: dict[str, Any]) -> dict:
    """耗时/高成本 Skill 的确认卡。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": detail}},
            {
                "tag": "action",
                "actions": [
                    _button(
                        "确认执行",
                        {"action": "confirm", "skill_id": skill_id, **args},
                        "primary",
                    ),
                    _button("取消", {"action": "cancel"}, "default"),
                ],
            },
        ],
    }


def build_done_card(message: str) -> dict:
    """用于回调原地替换原卡片：纯文本、无按钮（=禁用）。"""
    return {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": message}}],
    }


def _format_file_size(num_bytes: int | None) -> str:
    if num_bytes is None:
        return ""
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f}KB"
    return f"{num_bytes / (1024 * 1024):.1f}MB"


def build_file_upload_prompt_card() -> dict:
    """菜单/能力卡入口：引导用户在会话中发送文件（卡片内无法上传）。"""
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "文件摘要"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "请在**本对话**中直接发送文件：\n"
                        "- 支持 PDF / Word(.docx) / Excel(.xlsx) / CSV\n"
                        "- 单文件 ≤ 20MB\n"
                        "- 扫描件 PDF 暂不支持\n\n"
                        "发送后我会自动开始分析；**摘要完成后**才可追问。"
                    ),
                },
            },
            {
                "tag": "action",
                "actions": [
                    _button("取消", {"action": "cancel"}, "default"),
                ],
            },
        ],
    }


def build_file_summary_confirm_card(args: dict[str, Any]) -> dict:
    """直接上传入口：收到文件后请用户确认是否开始摘要。"""
    file_name = args.get("file_name") or "未命名文件"
    size_text = _format_file_size(args.get("file_size"))
    size_line = f"- 大小：约 {size_text}\n" if size_text else ""
    detail = (
        f"收到文件，确认开始**文件摘要**？\n\n"
        f"- 文件：{file_name}\n"
        f"{size_line}\n"
        "将调用 LLM 分片分析，耗时与 token 消耗取决于页数。\n"
        "**摘要完成后**才可追问。"
    )
    return build_confirm_card("financial_summary", "确认文件摘要？", detail, args)


# 文件摘要进度卡顶部展示的流水线阶段（顺序固定）。
_SUMMARY_PIPELINE = (
    ("download", "① 下载与解析"),
    ("map", "② 分片分析"),
    ("reduce", "③ 归并合成"),
    ("report", "④ 生成报告"),
)
_SUMMARY_QA_HINT = (
    "**追问：** 以上步骤全部完成后才可提问；届时会收到「文件摘要已完成」引导卡。"
)


def format_summary_pipeline_guide(phase: str) -> str:
    """渲染整体流程说明：已完成 ✅、进行中 ▶️、未开始 ○。"""
    order = [key for key, _ in _SUMMARY_PIPELINE]
    current = phase if phase in order else "download"
    current_idx = order.index(current)
    lines = []
    for idx, (_, label) in enumerate(_SUMMARY_PIPELINE):
        if idx < current_idx:
            marker = "✅"
        elif idx == current_idx:
            marker = "▶️"
        else:
            marker = "○"
        lines.append(f"{marker} {label}")
    return f"**整体流程**\n" + "\n".join(lines) + f"\n\n{_SUMMARY_QA_HINT}"


def build_progress_card(
    *,
    title: str,
    status: str,
    file_name: str | None = None,
    phase: str = "download",
) -> dict:
    """分析进度卡：配合 PATCH message_id 原地更新（需 config.update_multi）。"""
    parts: list[str] = [format_summary_pipeline_guide(phase), "---"]
    if file_name:
        parts.append(f"**文件：** {file_name}")
    parts.append(f"**当前：** {status}")
    content = "\n\n".join(parts)
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
        ],
    }


def build_summary_complete_card(
    *,
    hint: str,
    file_id: str | None = None,
    buttons: list[dict] | None = None,
) -> dict:
    """摘要完成后的引导卡：追问提示 + 针对此文件的后续动作按钮。"""
    content = (
        "✅ **全部流程已完成，现在可以追问。**\n\n"
        f"{hint}"
    )
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": content}},
    ]
    if file_id and buttons:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    _button(
                        b["label"],
                        {
                            "action": "run_skill",
                            "skill_id": b["skill_id"],
                            "file_id": file_id,
                        },
                    )
                    for b in buttons
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "文件摘要已完成"},
        },
        "elements": elements,
    }
