import json
import re

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
            "\n\nReturn only JSON with keys `answer`, `sources`, and `error`. "
            "`sources` must contain exact original page URLs. If you cannot expose "
            "exact URLs, return {\"answer\":\"\",\"sources\":[],"
            "\"error\":\"online_model_did_not_return_urls\"}."
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

        raw_answer = self._extract_answer(data).strip()
        return self._normalize_structured_answer(raw_answer)

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

    def _normalize_structured_answer(self, text: str) -> str:
        payload = self._parse_json_payload(text)
        if not isinstance(payload, dict):
            return (
                "模型内置联网链路没有返回可解析 JSON；"
                "因此本次模型联网结果未通过 URL 验证。"
            )

        answer = payload.get("answer")
        if not isinstance(answer, str):
            answer = ""
        sources = self._normalize_sources(payload.get("sources"))
        if not sources:
            error = payload.get("error")
            if not isinstance(error, str) or not error:
                error = "online_model_did_not_return_urls"
            return f"模型内置联网链路未返回可验证 URL：{error}"

        source_lines = "\n".join(
            f"- [{item.get('title') or item['url']}]({item['url']})"
            for item in sources
        )
        return answer.strip() + "\n\n## Sources\n" + source_lines

    def _parse_json_payload(self, text: str) -> dict | None:
        raw = (text or "").strip()
        if not raw:
            return None

        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE).strip()
            raw = re.sub(r"\s*```$", "", raw).strip()

        try:
            return json.loads(raw)
        except Exception:
            pass

        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(raw[start : end + 1])
        except Exception:
            return None

    def _normalize_sources(self, data) -> list[dict]:
        if not isinstance(data, list):
            return []
        sources: list[dict] = []
        seen: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            if url in seen:
                continue
            seen.add(url)
            out = {"url": url}
            title = item.get("title") or item.get("name")
            if isinstance(title, str) and title.strip():
                out["title"] = title.strip()
            sources.append(out)
        return sources
