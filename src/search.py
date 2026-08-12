"""搜索封装（Tavily API）"""
import os
import requests


class SearchClient:
    def __init__(self, api_key=None, max_results=6, days=5):
        self.api_key = api_key or os.environ["TAVILY_API_KEY"]
        self.max_results = max_results
        self.days = days

    def search(self, query):
        """执行一次搜索，返回 [{title, url, snippet}, ...]；失败返回空列表并打印原因"""
        try:
            r = requests.post(
                "https://api.tavily.com/search",
                json={
                    "query": query,
                    "max_results": self.max_results,
                    "days": self.days,
                    "search_depth": "basic",
                },
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            return [
                {
                    "title": it.get("title", ""),
                    "url": it.get("url", ""),
                    "snippet": (it.get("content") or "")[:300],
                }
                for it in results
            ]
        except Exception as e:
            print(f"  [搜索失败] {query[:40]}... 原因: {e}")
            return []

    def search_many(self, queries):
        """多查询合并去重（按 url）"""
        seen, out = set(), []
        for q in queries:
            for item in self.search(q):
                if item["url"] and item["url"] not in seen:
                    seen.add(item["url"])
                    out.append(item)
        return out
