import httpx

from .base import BaseSearchProvider
from ..config import config
from ..utils import search_prompt


class ZhipuSearchProvider(BaseSearchProvider):
    def __init__(self, api_url: str, api_key: str, model: str):
        super().__init__(api_url, api_key)
        self.model = model

    def get_provider_name(self) -> str:
        return "Zhipu"

    def _base_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def search(self, query: str, platform: str = "", ctx=None) -> str:
        search_query = query
        if platform:
            search_query = f"{query} {platform}"

        sources = await self._web_search(search_query)
        if not sources:
            return (
                "智谱 Web Search API 没有返回可用搜索结果；"
                "因此本次回答未独立联网验证。"
            )

        context = self._format_search_context(sources)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": search_prompt},
                {
                    "role": "user",
                    "content": (
                        "请只基于下面的智谱 Web Search API 搜索结果回答用户问题。"
                        "不要编造搜索结果中不存在的来源。回答末尾不需要另写来源列表，"
                        "因为系统会使用真实搜索结果生成 Sources。\n\n"
                        f"用户问题：{query}\n\n"
                        f"搜索结果：\n{context}"
                    ),
                },
            ],
            "stream": False,
            "max_tokens": config.max_tokens,
        }

        timeout = httpx.Timeout(connect=6.0, read=120.0, write=10.0, pool=None)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.post(
                f"{self.api_url.rstrip('/')}/chat/completions",
                headers=self._base_headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        answer = self._extract_answer(data).strip() or self._fallback_answer(query, sources)
        answer = answer.rstrip() + "\n\n## Sources\n" + "\n".join(
            f"- [{item.get('title') or item['url']}]({item['url']})"
            for item in sources
        )
        return answer

    async def _web_search(self, query: str) -> list[dict]:
        payload = {
            "search_query": query,
            "search_engine": config.zhipu_search_engine,
            "search_intent": False,
            "count": config.zhipu_search_count,
            "search_recency_filter": config.zhipu_recency_filter,
            "content_size": config.zhipu_content_size,
        }

        timeout = httpx.Timeout(connect=6.0, read=60.0, write=10.0, pool=None)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.post(
                f"{self.api_url.rstrip('/')}/web_search",
                headers=self._base_headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        return self._normalize_search_results(data.get("search_result") or [])

    def _format_search_context(self, sources: list[dict]) -> str:
        blocks = []
        for idx, item in enumerate(sources, start=1):
            parts = [
                f"[{idx}] {item.get('title') or item['url']}",
                f"URL: {item['url']}",
            ]
            if item.get("source"):
                parts.append(f"Media: {item['source']}")
            if item.get("published_date"):
                parts.append(f"Published: {item['published_date']}")
            if item.get("description"):
                parts.append(f"Content: {item['description']}")
            blocks.append("\n".join(parts))
        return "\n\n".join(blocks)

    def _fallback_answer(self, query: str, sources: list[dict]) -> str:
        lines = [f"已通过智谱 Web Search API 搜索：{query}", ""]
        for idx, item in enumerate(sources, start=1):
            lines.append(f"{idx}. {item.get('title') or item['url']}")
            if item.get("description"):
                lines.append(f"   {item['description'][:500]}")
        return "\n".join(lines)

    def _legacy_chat_search_payload(self, query: str) -> dict:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": search_prompt},
                {"role": "user", "content": query},
            ],
            "tools": [
                {
                    "type": "web_search",
                    "web_search": {
                        "enable": True,
                        "search_engine": config.zhipu_search_engine,
                        "search_result": True,
                        "count": config.zhipu_search_count,
                        "search_recency_filter": config.zhipu_recency_filter,
                        "content_size": config.zhipu_content_size,
                        "search_prompt": (
                            "请基于网络搜索结果回答用户问题。回答必须包含可核验来源，"
                            "重要事实后尽量引用来源编号或来源链接。"
                        ),
                    },
                }
            ],
            "tool_choice": "auto",
            "stream": False,
            "max_tokens": config.max_tokens,
        }

    def _extract_answer(self, data: dict) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        return ""

    def _extract_sources(self, data: dict) -> list[dict]:
        raw_sources = data.get("web_search") or []
        if not raw_sources:
            raw_sources = self._find_nested_web_search(data)
        return self._normalize_search_results(raw_sources)

    def _normalize_search_results(self, raw_sources: list[dict]) -> list[dict]:
        sources: list[dict] = []
        seen: set[str] = set()
        for item in raw_sources or []:
            if not isinstance(item, dict):
                continue
            url = item.get("link") or item.get("url") or item.get("href")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            if url in seen:
                continue
            seen.add(url)
            out = {"url": url, "provider": "zhipu"}
            title = item.get("title") or item.get("name")
            if isinstance(title, str) and title.strip():
                out["title"] = title.strip()
            content = item.get("content") or item.get("snippet") or item.get("summary")
            if isinstance(content, str) and content.strip():
                out["description"] = content.strip()
            media = item.get("media")
            if isinstance(media, str) and media.strip():
                out["source"] = media.strip()
            publish_date = item.get("publish_date")
            if isinstance(publish_date, str) and publish_date.strip():
                out["published_date"] = publish_date.strip()
            sources.append(out)
        return sources

    def _find_nested_web_search(self, data: dict) -> list[dict]:
        found: list[dict] = []

        def walk(value):
            if isinstance(value, dict):
                ws = value.get("web_search")
                if isinstance(ws, list):
                    found.extend([item for item in ws if isinstance(item, dict)])
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(data)
        return found
