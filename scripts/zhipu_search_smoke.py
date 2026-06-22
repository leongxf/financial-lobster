#!/usr/bin/env python
"""智谱 GLM Web Search 冒烟测试：实测中文行业出处的可得性与质量。

复用 .env 里的智谱 key（qa_embedding_api_key / 走 open.bigmodel.cn）。
两套接口都试，直接打印真实返回，用结果判断方案 A 是否值得建：
  1) 专用搜索接口  POST /api/paas/v4/web_search        （search_std / search_pro）
  2) 工具接口      POST /api/paas/v4/tools  tool=web-search-pro
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402

QUERIES = [
    "美容肽 化妆品原料 中国 市场规模 2024",
    "多肽化妆品原料 市场规模 预测 年复合增长率",
    "化妆品 多肽原料 监管政策 2024 国家药监局",
    "玻色因 市场规模 中国 2023 艾瑞 弗若斯特沙利文",
]


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def try_web_search(base: str, api_key: str, query: str, engine: str) -> None:
    url = base.rstrip("/") + "/web_search"
    payload = {"search_engine": engine, "search_query": query}
    print(f"\n--- [web_search engine={engine}] {query}")
    try:
        resp = httpx.post(url, headers=_headers(api_key), json=payload, timeout=40)
    except Exception as exc:
        print(f"  请求异常：{exc}")
        return
    if resp.status_code != 200:
        print(f"  HTTP {resp.status_code}: {resp.text[:300]}")
        return
    data = resp.json()
    results = data.get("search_result") or []
    print(f"  返回 {len(results)} 条结果")
    for i, r in enumerate(results[:5], 1):
        title = r.get("title", "")
        link = r.get("link", "")
        content = r.get("content") or ""
        date = r.get("publish_date") or ""
        print(f"   {i}. {title[:50]}  | {date}")
        print(f"      {link}")
        print(f"      正文 {len(content)} 字符：{content[:120].strip()}")


def try_tools_pro(base: str, api_key: str, query: str) -> None:
    url = base.rstrip("/") + "/tools"
    payload = {
        "tool": "web-search-pro",
        "messages": [{"role": "user", "content": query}],
        "stream": False,
    }
    print(f"\n--- [tools web-search-pro] {query}")
    try:
        resp = httpx.post(url, headers=_headers(api_key), json=payload, timeout=40)
    except Exception as exc:
        print(f"  请求异常：{exc}")
        return
    if resp.status_code != 200:
        print(f"  HTTP {resp.status_code}: {resp.text[:300]}")
        return
    data = resp.json()
    choices = data.get("choices") or []
    found = 0
    for choice in choices:
        for call in (choice.get("message") or {}).get("tool_calls") or []:
            for r in call.get("search_result") or []:
                found += 1
                if found <= 5:
                    title = r.get("title", "")
                    link = r.get("link", "")
                    content = r.get("content") or ""
                    print(f"   {found}. {title[:50]}")
                    print(f"      {link}")
                    print(f"      正文 {len(content)} 字符：{content[:120].strip()}")
    if found == 0:
        print(f"  未解析到 search_result。原始返回（截断）：{json.dumps(data, ensure_ascii=False)[:400]}")
    else:
        print(f"  共 {found} 条结果")


def main() -> None:
    settings = get_settings()
    base = settings.qa_embedding_base_url or "https://open.bigmodel.cn/api/paas/v4"
    api_key = settings.qa_embedding_api_key
    if not api_key:
        raise SystemExit("未找到智谱 key（QA_EMBEDDING_API_KEY）。")
    print(f"base={base}  key=***{api_key[-6:]}")

    # 只用一条 query 探接口可用性，确认哪套能用后再跑全部。
    probe = QUERIES[0]
    print("\n========== 接口探测（专用 /web_search, search_std）==========")
    try_web_search(base, api_key, probe, "search_std")
    print("\n========== 接口探测（专用 /web_search, search_pro）==========")
    try_web_search(base, api_key, probe, "search_pro")
    print("\n========== 接口探测（/tools web-search-pro）==========")
    try_tools_pro(base, api_key, probe)


if __name__ == "__main__":
    main()
