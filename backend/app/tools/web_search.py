import httpx


class WebSearchTool:
    name = "web_search"

    def __init__(self, base_url: str, api_key: str, engine: str = "search_std") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.engine = engine

    async def run(self, query: str) -> list[dict]:
        """返回 [{title, url, content, date}, ...]。失败抛异常，由调用方兜底。"""
        url = self.base_url + "/web_search"
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"search_engine": self.engine, "search_query": query},
            )
            resp.raise_for_status()
        results = resp.json().get("search_result") or []
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("link") or "",
                "content": (r.get("content") or "").strip(),
                "date": r.get("publish_date") or "",
            }
            for r in results
            if r.get("link")
        ]
