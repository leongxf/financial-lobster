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
