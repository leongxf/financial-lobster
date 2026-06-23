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
                    "content": "点选下面的功能，或直接发文件 / 发问题给我。",
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
        ],
    }


def build_next_step_card(file_id: str, buttons: list[dict]) -> dict:
    """文件分析完成后追发：针对此文件的后续动作。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "这个文件接下来做什么？"},
        },
        "elements": [
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
