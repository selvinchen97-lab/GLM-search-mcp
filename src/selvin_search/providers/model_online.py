import httpx

from .base import BaseSearchProvider
from ..config import config
from ..utils import model_online_search_prompt


class ModelOnlineSearchProvider(BaseSearchProvider):
    def __init__(self, api_url: str, api_key: str, model: str):
        super().__init__(api_url, api_key)
        self.model = model

    def get_provider_name(self) -> str:
        return "ModelOnline"

    def _base_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def search(self, query: str, platform: str = "", ctx=None) -> str:
        platform_prompt = ""
        if platform:
            platform_prompt = f"\n\nFocus on this source family or platform: {platform}"
        source_prompt = (
            "\n\nReturn a final `## Sources` section with the exact URLs of the "
            "original pages you used. Do not provide generic homepages or source "
            "names. If no exact URLs are available, say so using the required "
            "`No verifiable source URLs were available from this model call.` line."
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": model_online_search_prompt},
                {"role": "user", "content": query + platform_prompt + source_prompt},
            ],
            "stream": False,
            "max_tokens": config.max_tokens,
        }
        if config.online_use_search_tool:
            payload["tools"] = [
                {
                    "type": "web_search",
                    "web_search": {
                        "enable": True,
                        "search_engine": config.zhipu_search_engine,
                        "search_result": True,
                        "count": config.zhipu_search_count,
                        "search_recency_filter": config.zhipu_recency_filter,
                        "content_size": config.zhipu_content_size,
                    },
                }
            ]
            payload["tool_choice"] = "auto"

        timeout = httpx.Timeout(connect=6.0, read=120.0, write=10.0, pool=None)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.post(
                f"{self.api_url.rstrip('/')}/chat/completions",
                headers=self._base_headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        return self._extract_answer(data).strip()

    def _extract_answer(self, data: dict) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            reasoning = message.get("reasoning_content")
            if content.strip():
                return content
            if isinstance(reasoning, str):
                return reasoning
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        return ""
