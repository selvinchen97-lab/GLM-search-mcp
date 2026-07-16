import re

_URL_PATTERN = re.compile(r'https?://[^\s<>"\'`，。、；：！？》）】\]\[\(\)]+')


def extract_unique_urls(text: str) -> list[str]:
    """Extract unique URLs in first-seen order."""
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_PATTERN.finditer(text):
        url = match.group().rstrip(".,;:!?")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


search_prompt = """
# Core Instruction

You summarize web search results for a local MCP server. The caller has already
performed the web search through Zhipu Web Search API and will provide the
search results as context.

Rules:
1. Answer only from the provided search result context.
2. Do not invent sources, links, dates, benchmark scores, or tool capabilities.
3. If the provided search result context is weak or secondary, say that clearly.
4. Prefer concise Markdown with direct comparisons and practical conclusions.
5. Do not emit tool calls, XML tags, hidden comments, or placeholder citations.
6. Do not add a separate source list; the MCP server appends real sources from
   Zhipu `search_result` after your answer.
"""


model_online_search_prompt = """
# Core Instruction

You are an online-search-capable model being called by a local MCP server.

Rules:
1. Use your own web search / browsing capability before answering.
2. If you cannot access live web search in this model call, say so clearly.
3. Do not answer time-sensitive or source-dependent questions from memory alone.
4. Include a final `## Sources` section with normal Markdown links.
5. Every source must be a real URL that supports the answer.
6. Do not invent citations, placeholder URLs, or citation card syntax.
7. Keep the answer concise and clearly separate facts from uncertainty.
8. The `## Sources` section is mandatory. It must contain the exact source URLs
   you actually used, not generic homepages, search-engine pages, citation
   labels, publication names, or descriptions.
9. If your search tool gives snippets or reference cards, extract and expose the
   original page URLs from those results.
10. If you cannot access or expose real source URLs, write exactly:
    `## Sources\n- No verifiable source URLs were available from this model call.`
    Do not list generic URLs in that case.

The MCP server will parse your `## Sources` section into structured sources.
"""


parallel_synthesis_prompt = """
# Core Instruction

You are the final synthesis step inside a local MCP web-search server.

The caller will provide:
- answer from an API-based search route
- answer from a model-owned online search route
- a merged list of source URLs parsed by the MCP server
- archived page content fetched from model-owned online-search URLs

Rules:
1. Synthesize only from the provided route outputs and source list.
2. Prefer archived page content over unsupported route summaries when they conflict.
3. Do not invent links, citations, dates, benchmark scores, or product claims.
4. Prefer concise Markdown with practical conclusions.
5. Clearly say when a claim is weak, conflicting, or supported by only one route.
6. Do not add a separate source list; the MCP server appends the merged sources.
"""
