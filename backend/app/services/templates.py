from __future__ import annotations

from pathlib import Path
from typing import Any


def _label_from_filename(name: str) -> str:
    return Path(name).stem


def list_research_templates(settings: Any) -> list[dict]:
    """列出可选的行业研究模板。

    扫描 research_template_dir 下的 *.docx。
    返回 [{"name": 文件名, "label": 去后缀展示名}]，按文件名排序。
    """
    out: list[dict] = []
    template_dir = Path(settings.research_template_dir)
    if not template_dir.is_dir():
        return out
    for p in sorted(template_dir.glob("*.docx")):
        if p.is_file() and not p.name.startswith("~$"):  # 跳过 Word 临时锁文件
            out.append({"name": p.name, "label": _label_from_filename(p.name)})
    return out


def resolve_template_path(settings: Any, name: str | None) -> Path | None:
    """把用户选择的模板名解析为磁盘路径。

    只取 basename 在模板目录内查找，拒绝路径穿越。name 为空或找不到时返回 None。
    """
    if not name:
        return None

    safe_name = Path(name).name  # 防穿越：丢弃任何目录成分
    candidate = Path(settings.research_template_dir) / safe_name
    if candidate.is_file():
        return candidate
    return None
