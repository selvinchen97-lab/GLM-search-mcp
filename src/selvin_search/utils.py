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

The MCP server will parse your `## Sources` section into structured sources.
"""
