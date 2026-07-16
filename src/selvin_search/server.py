import sys
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
    from selvin_search.providers.zhipu import ZhipuSearchProvider
    from selvin_search.logger import log_info
    from selvin_search.config import config
    from selvin_search.sources import SourcesCache, merge_sources, new_session_id, split_answer_and_sources
    from selvin_search.planning import engine as planning_engine, result_cache, _split_csv
except ImportError:
    from .providers.zhipu import ZhipuSearchProvider
    from .logger import log_info
    from .config import config
    from .sources import SourcesCache, merge_sources, new_session_id, split_answer_and_sources
    from .planning import engine as planning_engine, result_cache, _split_csv

import asyncio

mcp = FastMCP("selvin-search")

_SOURCES_CACHE = SourcesCache(max_size=256)
_AVAILABLE_MODELS_CACHE: dict[tuple[str, str], list[str]] = {}
_AVAILABLE_MODELS_LOCK = asyncio.Lock()


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


@mcp.tool(
    name="web_search",
    output_schema=None,
    description="""
    Performs a web search through Zhipu Web Search API, summarizes with the configured model, and caches the source list.

    PLANNING GATE: when `plan_session_id` is provided, this tool refuses to run until the
    plan is complete (all required phases done, unverified_terms covered). If you intend to
    skip planning (one-shot factual lookup), call this tool with an empty `plan_session_id`.

    Returns:
      - session_id      string  pass to get_sources to retrieve full source list
      - content         string  answer summarized from Zhipu search_result
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
    cache_payload = f"{platform}|{model}|{query}"
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
    Returns current Selvin Search MCP server configuration and tests Zhipu API connectivity.

    **Key Features:**
        - **Configuration Check:** Verifies environment variables and current settings.
        - **Connection Test:** Sends request to Zhipu /web_search endpoint to validate API access.

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
                test_result["message"] = f"智谱 Web Search API 连通成功 (HTTP {response.status_code})"
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
