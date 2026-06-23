"""行业研究模板填充配置：按模板文件名指定需联网填充的章节（文字匹配）。"""

from __future__ import annotations

import json
from pathlib import Path

FILL_CONFIG_FILENAME = "research_fill_config.json"


def fill_config_path(template_dir: Path) -> Path:
    return template_dir / FILL_CONFIG_FILENAME


def _load_config(config_path: Path) -> dict:
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def get_fill_chapters(template_dir: Path, template_name: str) -> list[str]:
    """读取某模板需联网填充的章节关键词列表（对 L1 章标题做子串匹配）。"""
    templates = _load_config(fill_config_path(template_dir)).get("templates") or {}
    if not isinstance(templates, dict):
        return []
    entry = templates.get(template_name) or templates.get(Path(template_name).name)
    if not isinstance(entry, dict):
        return []
    keywords = entry.get("fill_chapters") or []
    if not isinstance(keywords, list):
        return []
    return [str(k).strip() for k in keywords if str(k).strip()]


def format_config_hint(template_dir: Path, template_name: str) -> str:
    return (
        f"模板「{template_name}」未配置需填充章节。"
        f"请在 {fill_config_path(template_dir)} 中为该模板添加 fill_chapters（章标题子串，如「第二章」）。"
    )
