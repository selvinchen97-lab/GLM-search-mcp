import sys
import re
from html.parser import HTMLParser
from pathlib import Path

# 支持直接运行：添加 src 目录到 Python 路径
src_dir = Path(__file__).parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from fastmcp import FastMCP, Context
from typing import Annotated, Optional
from pydantic import Field

# 尝试使用绝对导入（支持 mcp run）
try:
    from selvin_search.providers.model_online import ModelOnlineSearchProvider
    from selvin_search.providers.zhipu import ZhipuSearchProvider
    from selvin_search.logger import log_info
    from selvin_search.config import config
    from selvin_search.sources import SourcesCache, merge_sources, new_session_id, split_answer_and_sources
    from selvin_search.planning import engine as planning_engine, result_cache, _split_csv
    from selvin_search.utils import parallel_synthesis_prompt
except ImportError:
    from .providers.model_online import ModelOnlineSearchProvider
    from .providers.zhipu import ZhipuSearchProvider
    from .logger import log_info
    from .config import config
    from .sources import SourcesCache, merge_sources, new_session_id, split_answer_and_sources
    from .planning import engine as planning_engine, result_cache, _split_csv
    from .utils import parallel_synthesis_prompt

import asyncio

mcp = FastMCP("selvin-search")

_SOURCES_CACHE = SourcesCache(max_size=256)
_AVAILABLE_MODELS_CACHE: dict[tuple[str, str], list[str]] = {}
_AVAILABLE_MODELS_LOCK = asyncio.Lock()


class _TextExtractingHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = (data or "").strip()
        if text:
            self._parts.append(text)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()


async def _fetch_available_models(api_url: str, api_key: str) -> list[str]:
    import httpx

    models_url = f"{api_url.rstrip('/')}/models"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            models_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()

    models: list[str] = []
    for item in (data or {}).get("data", []) or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            models.append(item["id"])
    return models


async def _get_available_models_cached(api_url: str, api_key: str) -> list[str]:
    key = (api_url, api_key)
    async with _AVAILABLE_MODELS_LOCK:
        if key in _AVAILABLE_MODELS_CACHE:
            return _AVAILABLE_MODELS_CACHE[key]

    try:
        models = await _fetch_available_models(api_url, api_key)
    except Exception:
        models = []

    async with _AVAILABLE_MODELS_LOCK:
        _AVAILABLE_MODELS_CACHE[key] = models
    return models


def _format_sources_for_synthesis(sources: list[dict]) -> str:
    blocks: list[str] = []
    for idx, item in enumerate(sources, start=1):
        parts = [
            f"[{idx}] {item.get('title') or item.get('url')}",
            f"URL: {item.get('url')}",
        ]
        if item.get("provider"):
            parts.append(f"Provider: {item['provider']}")
        if item.get("source"):
            parts.append(f"Media: {item['source']}")
        if item.get("published_date"):
            parts.append(f"Published: {item['published_date']}")
        if item.get("description"):
            parts.append(f"Description: {item['description']}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def _format_archives_for_synthesis(archives: list[dict]) -> str:
    blocks: list[str] = []
    for idx, item in enumerate(archives, start=1):
        parts = [
            f"[{idx}] {item.get('title') or item.get('url')}",
            f"URL: {item.get('url')}",
            f"Fetch status: {item.get('archive_status', 'unknown')}",
        ]
        if item.get("archive_content"):
            parts.append(f"Content: {item['archive_content']}")
        elif item.get("archive_error"):
            parts.append(f"Error: {item['archive_error']}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def _tag_sources(sources: list[dict], provider: str) -> list[dict]:
    tagged: list[dict] = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        out = dict(item)
        out.setdefault("provider", provider)
        tagged.append(out)
    return tagged


def _extract_page_text(content_type: str, body: str) -> str:
    if "html" not in (content_type or "").lower():
        return re.sub(r"\s+", " ", body or "").strip()
    parser = _TextExtractingHTMLParser()
    try:
        parser.feed(body or "")
        return parser.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", body or "").strip()


async def _fetch_one_source_archive(client, source: dict) -> dict:
    url = source.get("url")
    out = dict(source)
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        out["archive_status"] = "skipped"
        out["archive_error"] = "invalid_url"
        return out

    try:
        response = await client.get(
            url,
            headers={
                "User-Agent": "selvin-search-mcp/0.1 (+https://github.com/selvinchen97-lab/GLM-search-mcp)",
                "Accept": "text/html, text/plain, application/xhtml+xml;q=0.9, */*;q=0.8",
            },
        )
        response.raise_for_status()
        text = _extract_page_text(response.headers.get("content-type", ""), response.text)
        out["archive_status"] = "fetched"
        out["archive_content"] = text[: config.online_source_fetch_chars]
    except Exception as exc:
        out["archive_status"] = "failed"
        out["archive_error"] = str(exc)[:300]
    return out


async def _fetch_online_source_archives(sources: list[dict]) -> list[dict]:
    if not config.online_source_fetch_enabled:
        return []

    targets = [
        item
        for item in sources
        if isinstance(item, dict) and item.get("provider") == "model_online"
    ][: max(config.online_source_fetch_count, 0)]
    if not targets:
        return []

    import httpx

    timeout = httpx.Timeout(connect=6.0, read=20.0, write=10.0, pool=None)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        return await asyncio.gather(
            *(_fetch_one_source_archive(client, source) for source in targets),
            return_exceptions=False,
        )


async def _collect_task_result(task: asyncio.Task) -> str:
    try:
        result = await task
    except asyncio.CancelledError:
        return ""
    except Exception:
        return ""
    return "" if isinstance(result, Exception) else result


def _has_search_signal(answer: str, sources: list[dict]) -> bool:
    if sources:
        return True
    text = (answer or "").strip()
    if not text:
        return False
    weak_markers = (
        "无法访问实时网络",
        "未能实际访问网页",
        "无实时联网工具可用",
        "没有返回可用搜索结果",
        "未独立联网验证",
    )
    return not any(marker in text for marker in weak_markers)


async def _cancel_task(task: asyncio.Task) -> None:
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _synthesize_parallel_answer(
    api_url: str,
    api_key: str,
    model: str,
    query: str,
    api_answer: str,
    online_answer: str,
    sources: list[dict],
    archives: list[dict],
) -> str:
    import httpx

    if not api_answer.strip() and not online_answer.strip():
        return ""

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": parallel_synthesis_prompt},
            {
                "role": "user",
                "content": (
                    f"用户问题：{query}\n\n"
                    "## API 搜索链路返回\n"
                    f"{api_answer or '无可用回答'}\n\n"
                    "## 模型内置联网链路返回\n"
                    f"{online_answer or '无可用回答'}\n\n"
                    "## MCP 已合并来源\n"
                    f"{_format_sources_for_synthesis(sources) or '无可用来源'}\n\n"
                    "## MCP 二次抓取并解析的模型联网来源正文\n"
                    f"{_format_archives_for_synthesis(archives) or '无可用归档正文'}"
                ),
            },
        ],
        "stream": False,
        "max_tokens": config.max_tokens,
    }

    timeout = httpx.Timeout(connect=6.0, read=120.0, write=10.0, pool=None)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.post(
            f"{api_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    choices = data.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts).strip()
    return ""


@mcp.tool(
    name="web_search",
    output_schema=None,
    description="""
    Performs a web search through the configured search mode and caches the source list.

    PLANNING GATE: when `plan_session_id` is provided, this tool refuses to run until the
    plan is complete (all required phases done, unverified_terms covered). If you intend to
    skip planning (one-shot factual lookup), call this tool with an empty `plan_session_id`.

    Returns:
      - session_id      string  pass to get_sources to retrieve full source list
      - content         string  answer from API-search summarization or online model search
      - sources_count   int
      - cached          bool    true if response was served from in-memory result cache
      - budget          object  (when plan_session_id set) actual vs estimated tool calls
    """,
    meta={"version": "3.0.0", "author": "selvin"},
)
async def web_search(
    query: Annotated[str, "Clear, self-contained natural-language search query."],
    platform: Annotated[str, "Target platform to focus on (e.g., 'Twitter', 'GitHub', 'Reddit'). Leave empty for general web search."] = "",
    model: Annotated[str, "Optional model ID for this request only. This value is used ONLY when user explicitly provided."] = "",
    plan_session_id: Annotated[str, "Planning session ID. When set, the tool enforces plan completion before running. Empty = no gating (one-shot mode)."] = "",
) -> dict:
    # ── Planning gate ───────────────────────────────────────
    if plan_session_id:
        ok_gate, reason = planning_engine.check_gate(plan_session_id)
        if not ok_gate:
            return {
                "session_id": "",
                "content": f"Planning incomplete: {reason}",
                "sources_count": 0,
                "blocked_by_gate": True,
            }

    # ── Cached response shortcut ────────────────────────────
    cache_payload = f"{config.search_mode}|{platform}|{model}|{query}"
    cached = await result_cache.get("web_search", cache_payload)
    if cached and isinstance(cached, dict):
        new_sid = new_session_id()
        await _SOURCES_CACHE.set(new_sid, cached.get("sources", []))
        budget = planning_engine.increment_tool_calls(plan_session_id) if plan_session_id else None
        out = {
            "session_id": new_sid,
            "content": cached.get("content", ""),
            "sources_count": len(cached.get("sources", [])),
            "cached": True,
        }
        if budget:
            out["budget"] = budget
        return out

    session_id = new_session_id()
    try:
        api_url = config.api_url
        api_key = config.api_key
    except ValueError as e:
        await _SOURCES_CACHE.set(session_id, [])
        return {"session_id": session_id, "content": f"配置错误: {str(e)}", "sources_count": 0}

    effective_model = config.model
    if model:
        available = [] if config.provider == "zhipu" else await _get_available_models_cached(api_url, api_key)
        if available and model not in available:
            await _SOURCES_CACHE.set(session_id, [])
            return {"session_id": session_id, "content": f"无效模型: {model}", "sources_count": 0}
        effective_model = config.effective_model(model)

    if config.search_mode == "parallel":
        api_provider = ZhipuSearchProvider(api_url, api_key, effective_model)
        online_provider = ModelOnlineSearchProvider(
            config.online_api_url,
            config.online_api_key,
            config.online_model,
        )
        api_task = asyncio.create_task(api_provider.search(query, platform))
        online_task = asyncio.create_task(online_provider.search(query, platform))
        done, _ = await asyncio.wait({api_task, online_task}, return_when=asyncio.FIRST_COMPLETED)

        api_raw = ""
        online_raw = ""
        if online_task in done:
            online_raw = await _collect_task_result(online_task)
            online_answer_probe, online_sources_probe = split_answer_and_sources(online_raw)
            if _has_search_signal(online_answer_probe, online_sources_probe):
                if api_task.done():
                    api_raw = await _collect_task_result(api_task)
                else:
                    try:
                        api_raw = await asyncio.wait_for(
                            asyncio.shield(api_task),
                            timeout=max(config.api_cancel_grace_seconds, 0),
                        )
                    except asyncio.TimeoutError:
                        await _cancel_task(api_task)
                    except Exception:
                        api_raw = ""
            else:
                api_raw = await _collect_task_result(api_task)
        else:
            api_raw = await _collect_task_result(api_task)
            online_raw = await _collect_task_result(online_task)

        api_answer, api_sources = split_answer_and_sources(api_raw)
        online_answer, online_sources = split_answer_and_sources(online_raw)
        api_sources = _tag_sources(api_sources, "api")
        online_sources = _tag_sources(online_sources, "model_online")
        all_sources = merge_sources(api_sources, online_sources)
        archives = await _fetch_online_source_archives(all_sources)
        archived_by_url = {item.get("url"): item for item in archives if item.get("url")}
        all_sources = [
            {**item, **archived_by_url.get(item.get("url"), {})}
            for item in all_sources
        ]

        try:
            answer = await _synthesize_parallel_answer(
                api_url=api_url,
                api_key=api_key,
                model=effective_model,
                query=query,
                api_answer=api_answer,
                online_answer=online_answer,
                sources=all_sources,
                archives=archives,
            )
        except Exception:
            answer = ""

        if not answer:
            parts = []
            if api_answer:
                parts.append("## API 搜索链路\n\n" + api_answer)
            if online_answer:
                parts.append("## 模型内置联网链路\n\n" + online_answer)
            answer = "\n\n".join(parts)
        if not answer:
            answer = "两条搜索链路都没有返回可用内容；因此本次回答未独立联网验证。"
    else:
        if config.search_mode == "model_online":
            search_provider = ModelOnlineSearchProvider(api_url, api_key, effective_model)
        else:
            search_provider = ZhipuSearchProvider(api_url, api_key, effective_model)

        try:
            primary_result = await search_provider.search(query, platform)
        except Exception:
            primary_result = ""
        answer, primary_sources = split_answer_and_sources(primary_result)
        all_sources = merge_sources(primary_sources)

    await _SOURCES_CACHE.set(session_id, all_sources)
    await result_cache.set("web_search", cache_payload, {"content": answer, "sources": all_sources})
    budget = planning_engine.increment_tool_calls(plan_session_id) if plan_session_id else None
    out: dict = {"session_id": session_id, "content": answer, "sources_count": len(all_sources), "cached": False}
    if budget:
        out["budget"] = budget
    return out


@mcp.tool(
    name="get_sources",
    description="""
    When you feel confused or curious about the search response content, use the session_id returned by web_search to invoke the this tool to obtain the corresponding list of information sources.
    Retrieve all cached sources for a previous web_search call.
    Provide the session_id returned by web_search to get the full source list.
    """,
    meta={"version": "1.0.0", "author": "selvin"},
)
async def get_sources(
    session_id: Annotated[str, "Session ID from previous web_search call."]
) -> dict:
    sources = await _SOURCES_CACHE.get(session_id)
    if sources is None:
        return {
            "session_id": session_id,
            "sources": [],
            "sources_count": 0,
            "error": "session_id_not_found_or_expired",
        }
    return {"session_id": session_id, "sources": sources, "sources_count": len(sources)}


@mcp.tool(
    name="get_config_info",
    output_schema=None,
    description="""
    Returns current Selvin Search MCP server configuration and tests the configured upstream connectivity.

    **Key Features:**
        - **Configuration Check:** Verifies environment variables and current settings.
        - **Connection Test:** Sends request to /web_search in api mode, /models in model_online mode, or both in parallel mode.

    **Edge Cases & Best Practices:**
        - Use this tool first when debugging connection or configuration issues.
        - API keys are automatically masked for security in the response.
        - Connection test timeout is 10 seconds; network issues may cause delays.
    """,
    meta={"version": "1.3.0", "author": "selvin"},
)
async def get_config_info() -> str:
    import json
    import httpx

    config_info = config.get_config_info()

    # 添加连接测试
    test_result = {
        "status": "未测试",
        "message": "",
        "response_time_ms": 0
    }

    try:
        api_url = config.api_url
        api_key = config.api_key

        # 发送测试请求
        import time
        start_time = time.time()

        async with httpx.AsyncClient(timeout=10.0) as client:
            if config.search_mode == "parallel":
                api_response, online_response = await asyncio.gather(
                    client.post(
                        f"{api_url.rstrip('/')}/web_search",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "search_query": "智谱联网搜索连通性测试",
                            "search_engine": config.zhipu_search_engine,
                            "search_intent": False,
                            "count": 1,
                            "search_recency_filter": config.zhipu_recency_filter,
                            "content_size": "medium",
                        },
                    ),
                    client.get(
                        f"{config.online_api_url.rstrip('/')}/models",
                        headers={
                            "Authorization": f"Bearer {config.online_api_key}",
                            "Content-Type": "application/json"
                        },
                    ),
                    return_exceptions=True,
                )
                response = api_response if not isinstance(api_response, Exception) else online_response
                api_ok = not isinstance(api_response, Exception) and api_response.status_code == 200
                online_ok = not isinstance(online_response, Exception) and online_response.status_code == 200
                if api_ok and online_ok:
                    test_result["status"] = "✅ 连接成功"
                    test_result["message"] = "并行模式连通成功：/web_search 与 /models 均可访问"
                    test_result["response_time_ms"] = round((time.time() - start_time) * 1000, 2)
                    config_info["connection_test"] = test_result
                    return json.dumps(config_info, ensure_ascii=False, indent=2)
                test_result["status"] = "⚠️ 连接异常"
                test_result["message"] = (
                    f"并行模式连通性异常：api={'OK' if api_ok else 'FAIL'}, "
                    f"model_online={'OK' if online_ok else 'FAIL'}"
                )
                test_result["response_time_ms"] = round((time.time() - start_time) * 1000, 2)
                config_info["connection_test"] = test_result
                return json.dumps(config_info, ensure_ascii=False, indent=2)
            elif config.search_mode == "model_online":
                response = await client.get(
                    f"{api_url.rstrip('/')}/models",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                )
            else:
                response = await client.post(
                    f"{api_url.rstrip('/')}/web_search",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "search_query": "智谱联网搜索连通性测试",
                        "search_engine": config.zhipu_search_engine,
                        "search_intent": False,
                        "count": 1,
                        "search_recency_filter": config.zhipu_recency_filter,
                        "content_size": "medium",
                    },
                )

            response_time = (time.time() - start_time) * 1000  # 转换为毫秒

            if response.status_code == 200:
                test_result["status"] = "✅ 连接成功"
                test_result["message"] = (
                    f"模型 API /models 连通成功 (HTTP {response.status_code})"
                    if config.search_mode == "model_online"
                    else f"智谱 Web Search API 连通成功 (HTTP {response.status_code})"
                )
                test_result["response_time_ms"] = round(response_time, 2)
            else:
                test_result["status"] = "⚠️ 连接异常"
                test_result["message"] = f"HTTP {response.status_code}: {response.text[:100]}"
                test_result["response_time_ms"] = round(response_time, 2)

    except httpx.TimeoutException:
        test_result["status"] = "❌ 连接超时"
        test_result["message"] = "请求超时（10秒），请检查网络连接或 API URL"
    except httpx.RequestError as e:
        test_result["status"] = "❌ 连接失败"
        test_result["message"] = f"网络错误: {str(e)}"
    except ValueError as e:
        test_result["status"] = "❌ 配置错误"
        test_result["message"] = str(e)
    except Exception as e:
        test_result["status"] = "❌ 测试失败"
        test_result["message"] = f"未知错误: {str(e)}"

    config_info["connection_test"] = test_result

    return json.dumps(config_info, ensure_ascii=False, indent=2)


@mcp.tool(
    name="switch_model",
    output_schema=None,
    description="""
    Switches the GLM-compatible model used for search summarization, persisting the setting.

    **Key Features:**
        - **Model Selection:** Change the AI model for web search and content fetching.
        - **Persistent Storage:** Model preference saved to ~/.config/selvin-search/config.json.
        - **Immediate Effect:** New model used for all subsequent operations.

    **Edge Cases & Best Practices:**
        - Use get_config_info to verify available models before switching.
        - Invalid model IDs may cause API errors in subsequent requests.
        - Model changes persist across sessions until explicitly changed again.
    """,
    meta={"version": "1.3.0", "author": "selvin"},
)
async def switch_model(
    model: Annotated[str, "Model ID to switch to."]
) -> str:
    import json

    try:
        previous_model = config.model
        config.set_model(model)
        current_model = config.model

        result = {
            "status": "✅ 成功",
            "previous_model": previous_model,
            "current_model": current_model,
            "message": f"模型已从 {previous_model} 切换到 {current_model}",
            "config_file": str(config.config_file)
        }

        return json.dumps(result, ensure_ascii=False, indent=2)

    except ValueError as e:
        result = {
            "status": "❌ 失败",
            "message": f"切换模型失败: {str(e)}"
        }
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        result = {
            "status": "❌ 失败",
            "message": f"未知错误: {str(e)}"
        }
        return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_intent",
    output_schema=None,
    description="""
    Phase 1 of search planning: Analyze user intent. Call this FIRST to create a session.
    Returns session_id for subsequent phases. Required flow:
    plan_intent → plan_complexity → plan_sub_query(×N) → plan_search_term(×N) → plan_tool_mapping(×N) → plan_execution

    Required phases depend on complexity: Level 1 = phases 1-3; Level 2 = phases 1-5; Level 3 = all 6.
    """,
)
async def plan_intent(
    thought: Annotated[str, "Reasoning for this phase"],
    core_question: Annotated[str, "Distilled core question in one sentence"],
    query_type: Annotated[str, "factual | comparative | exploratory | analytical"],
    time_sensitivity: Annotated[str, "realtime | recent | historical | irrelevant"],
    session_id: Annotated[str, "Empty for new session, or existing ID to revise"] = "",
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    domain: Annotated[str, "Specific domain if identifiable"] = "",
    premise_valid: Annotated[Optional[bool], "False if the question contains a flawed assumption"] = None,
    ambiguities: Annotated[str, "Comma-separated unresolved ambiguities"] = "",
    unverified_terms: Annotated[str, "Comma-separated external terms to verify"] = "",
    is_revision: Annotated[bool, "True to overwrite existing intent"] = False,
) -> str:
    import json
    data = {"core_question": core_question, "query_type": query_type, "time_sensitivity": time_sensitivity}
    if domain:
        data["domain"] = domain
    if premise_valid is not None:
        data["premise_valid"] = premise_valid
    if ambiguities:
        data["ambiguities"] = _split_csv(ambiguities)
    if unverified_terms:
        data["unverified_terms"] = _split_csv(unverified_terms)
    return json.dumps(planning_engine.process_phase(
        phase="intent_analysis", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=data,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_complexity",
    output_schema=None,
    description="Phase 2: Assess search complexity (1-3). Controls required phases: Level 1 = phases 1-3; Level 2 = phases 1-5; Level 3 = all 6.",
)
async def plan_complexity(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for complexity assessment"],
    level: Annotated[int, "Complexity 1-3"],
    estimated_sub_queries: Annotated[int, "Expected number of sub-queries"],
    estimated_tool_calls: Annotated[int, "Expected total tool calls"],
    justification: Annotated[str, "Why this complexity level"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    is_revision: Annotated[bool, "True to overwrite"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    return json.dumps(planning_engine.process_phase(
        phase="complexity_assessment", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence,
        phase_data={"level": level, "estimated_sub_queries": estimated_sub_queries,
                     "estimated_tool_calls": estimated_tool_calls, "justification": justification},
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_sub_query",
    output_schema=None,
    description="""Phase 3: Submit ALL sub-queries in ONE call (batch).

items_json: JSON array, each element shape:
    {"id":"sq1","goal":"...","expected_output":"...","boundary":"...",
     "depends_on":["sq0"],"tool_hint":"web_search"}

Validation enforced by the engine:
  - ids must be unique
  - depends_on must reference declared ids
  - no cycles in depends_on graph
  - duplicate id → error returned, batch rejected

Set is_revision=true to replace any previously submitted decomposition.""",
)
async def plan_sub_query(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for the full decomposition"],
    items_json: Annotated[str, "JSON array of sub-query objects (see description for shape)"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    is_revision: Annotated[bool, "True to replace prior decomposition"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    try:
        parsed = json.loads(items_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"items_json is not valid JSON: {e}"})
    if not isinstance(parsed, list) or not parsed:
        return json.dumps({"error": "items_json must be a non-empty JSON array"})
    normalized: list[dict] = []
    for raw in parsed:
        if not isinstance(raw, dict):
            return json.dumps({"error": f"each item must be an object, got: {type(raw).__name__}"})
        for required in ("id", "goal", "expected_output", "boundary"):
            if not raw.get(required):
                return json.dumps({"error": f"item missing required field: {required!r}"})
        item: dict = {
            "id": raw["id"],
            "goal": raw["goal"],
            "expected_output": raw["expected_output"],
            "boundary": raw["boundary"],
        }
        if raw.get("depends_on"):
            deps = raw["depends_on"]
            if isinstance(deps, str):
                deps = _split_csv(deps)
            item["depends_on"] = list(deps)
        if raw.get("tool_hint"):
            item["tool_hint"] = raw["tool_hint"]
        normalized.append(item)
    return json.dumps(planning_engine.process_phase(
        phase="query_decomposition", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=normalized,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_search_term",
    output_schema=None,
    description="""Phase 4: Submit ALL search terms in ONE call (batch).

terms_json: JSON array, each element shape:
    {"term":"react server components 2025","purpose":"sq1","round":1}

Strict rules (enforced):
  - term MUST be ≤8 words (engine rejects otherwise)
  - purpose MUST reference a declared sub-query id
  - one term per (purpose, round); add multiple rounds for follow-up refinement

approach (broad_first | narrow_first | targeted) and fallback_plan are
strategy-level; pass them as top-level params, not inside terms.""",
)
async def plan_search_term(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for the full strategy"],
    terms_json: Annotated[str, "JSON array of search-term objects (see description)"],
    approach: Annotated[str, "broad_first | narrow_first | targeted"] = "targeted",
    fallback_plan: Annotated[str, "Fallback if primary searches fail"] = "",
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    is_revision: Annotated[bool, "True to replace prior strategy"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    try:
        parsed = json.loads(terms_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"terms_json is not valid JSON: {e}"})
    if not isinstance(parsed, list) or not parsed:
        return json.dumps({"error": "terms_json must be a non-empty JSON array"})
    normalized: list[dict] = []
    for raw in parsed:
        if not isinstance(raw, dict):
            return json.dumps({"error": f"each term must be an object, got: {type(raw).__name__}"})
        for required in ("term", "purpose", "round"):
            if raw.get(required) in (None, ""):
                return json.dumps({"error": f"search term missing required field: {required!r}"})
        normalized.append({
            "term": raw["term"],
            "purpose": raw["purpose"],
            "round": int(raw["round"]),
        })
    data: dict = {"search_terms": normalized, "approach": approach}
    if fallback_plan:
        data["fallback_plan"] = fallback_plan
    return json.dumps(planning_engine.process_phase(
        phase="search_strategy", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=data,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_tool_mapping",
    output_schema=None,
    description="""Phase 5: Submit ALL sub-query → tool mappings in ONE call (batch).

mappings_json: JSON array, each element shape:
    {"sub_query_id":"sq1","tool":"web_search","reason":"...",
     "params":{"platform":"GitHub"}}

tool must be: web_search.""",
)
async def plan_tool_mapping(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for the full mapping"],
    mappings_json: Annotated[str, "JSON array of mapping objects (see description)"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    is_revision: Annotated[bool, "True to replace prior mappings"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    try:
        parsed = json.loads(mappings_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"mappings_json is not valid JSON: {e}"})
    if not isinstance(parsed, list) or not parsed:
        return json.dumps({"error": "mappings_json must be a non-empty JSON array"})
    normalized: list[dict] = []
    for raw in parsed:
        if not isinstance(raw, dict):
            return json.dumps({"error": f"each mapping must be an object, got: {type(raw).__name__}"})
        for required in ("sub_query_id", "tool", "reason"):
            if not raw.get(required):
                return json.dumps({"error": f"mapping missing required field: {required!r}"})
        if raw["tool"] != "web_search":
            return json.dumps({"error": f"tool must be web_search, got: {raw['tool']!r}"})
        item: dict = {
            "sub_query_id": raw["sub_query_id"],
            "tool": raw["tool"],
            "reason": raw["reason"],
        }
        if isinstance(raw.get("params"), dict):
            item["params"] = raw["params"]
        normalized.append(item)
    return json.dumps(planning_engine.process_phase(
        phase="tool_selection", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=normalized,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_execution",
    output_schema=None,
    description="Phase 6: Define execution order. parallel_groups: semicolon-separated groups of comma-separated IDs (e.g., 'sq1,sq2;sq3').",
)
async def plan_execution(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for execution order"],
    parallel_groups: Annotated[str, "Parallel batches: 'sq1,sq2;sq3,sq4' (semicolon=groups, comma=IDs)"],
    sequential: Annotated[str, "Comma-separated IDs that must run in order"],
    estimated_rounds: Annotated[int, "Estimated execution rounds"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    is_revision: Annotated[bool, "True to overwrite"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    parallel = [_split_csv(g) for g in parallel_groups.split(";") if g.strip()] if parallel_groups else []
    seq = _split_csv(sequential)
    return json.dumps(planning_engine.process_phase(
        phase="execution_order", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence,
        phase_data={"parallel": parallel, "sequential": seq, "estimated_rounds": estimated_rounds},
    ), ensure_ascii=False, indent=2)


def main():
    import signal
    import os
    import threading

    # 信号处理（仅主线程）
    if threading.current_thread() is threading.main_thread():
        def handle_shutdown(signum, frame):
            os._exit(0)
        signal.signal(signal.SIGINT, handle_shutdown)
        if sys.platform != 'win32':
            signal.signal(signal.SIGTERM, handle_shutdown)

    # Windows 父进程监控
    if sys.platform == 'win32':
        import time
        import ctypes
        parent_pid = os.getppid()

        def is_parent_alive(pid):
            """Windows 下检查进程是否存活"""
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return True
            exit_code = ctypes.c_ulong()
            result = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return result and exit_code.value == STILL_ACTIVE

        def monitor_parent():
            while True:
                if not is_parent_alive(parent_pid):
                    os._exit(0)
                time.sleep(2)

        threading.Thread(target=monitor_parent, daemon=True).start()

    try:
        mcp.run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        pass
    finally:
        os._exit(0)


if __name__ == "__main__":
    main()
