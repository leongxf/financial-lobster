from app.services.document_parser import ParsedDocument


def build_parse_preview_report(document: ParsedDocument) -> str:
    preview = document.text[:1200].strip()
    if not preview:
        preview = "未能从材料中提取到可复制文本。该文件可能是扫描件或图片型 PDF。"

    sections = [
        "# 材料接收成功",
        "",
        "## 文件概览",
        f"- 文件名：`{document.file_path.name}`",
        f"- 文件类型：`{document.file_type}`",
        f"- 页数：`{document.page_count if document.page_count is not None else '未知'}`",
        f"- 已提取文本长度：`{len(document.text)}` 字符",
        "",
        "## 当前处理状态",
        "- 文件已成功下载到本地临时目录。",
        "- 已完成基础文本提取。",
        "- 下一步将接入大模型，生成文件概览、一句话摘要、财务要点摘要、全文概要和客观数据表。",
        "",
        "## 文本预览",
        preview,
        "",
        "## 说明",
        "本阶段仅反馈材料中的客观提取结果，不臆造材料内容。",
    ]

    return "\n".join(sections)
