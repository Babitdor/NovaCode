"""Web search tools.

This module provides tools for searching the web using various search engines.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain.tools import tool

from novacode_cli.config.config import settings

# Initialize Tavily client if API key is available
try:
    from tavily import TavilyClient

    tavily_client = TavilyClient(api_key=settings.tavily_api_key) if settings.has_tavily else None
except ImportError:
    tavily_client = None


@tool
def web_search(
    query: str,
    max_results: int = 5,
    topic: Literal["general", "news", "finance"] = "general",
    include_raw_content: bool = False,
) -> dict[str, Any]:
    """Search the web using Tavily for current information and documentation.

    This tool searches the web and returns relevant results. After receiving results,
    you MUST synthesize the information into a natural, helpful response for the user.

    Args:
        query: The search query (be specific and detailed)
        max_results: Number of results to return (default: 5)
        topic: Search topic type - "general" for most queries, "news" for current events
        include_raw_content: Include full page content (warning: uses more tokens)

    Returns:
        Dictionary containing:
        - results: List of search results, each with:
            - title: Page title
            - url: Page URL
            - content: Relevant excerpt from the page
            - score: Relevance score (0-1)
        - query: The original search query

    IMPORTANT: After using this tool:
    1. Read through the 'content' field of each result
    2. Extract relevant information that answers the user's question
    3. Synthesize this into a clear, natural language response
    4. Cite sources by mentioning the page titles or URLs
    5. NEVER show the raw JSON to the user - always provide a formatted response
    """
    if tavily_client is None:
        return {
            "error": (
                "Tavily API key not configured. Please set TAVILY_API_KEY environment variable."
            ),
            "query": query,
        }

    try:
        return tavily_client.search(
            query,
            max_results=max_results,
            include_raw_content=include_raw_content,
            topic=topic,
        )
    except Exception as e:  # noqa: BLE001
        return {"error": f"Web search error: {e!s}", "query": query}


@tool
def duckduckgo_search(
    query: str,
    max_results: int = 5,
    region: str = "wt-wt",
    safesearch: Literal["on", "moderate", "off"] = "moderate",
    time_range: Literal["d", "w", "m", "y", ""] = "",
) -> dict[str, Any]:
    """Search the web using DuckDuckGo (no API key required).

    A free alternative to Tavily for web search. Returns relevant search results
    that you should synthesize into a natural response for the user.

    Args:
        query: The search query (be specific and detailed)
        max_results: Number of results to return (default: 5, max: 20)
        region: Region for search results (default: "wt-wt" for worldwide)
                Examples: "us-en", "uk-en", "de-de", "fr-fr", "jp-jp"
        safesearch: Safe search level - "on", "moderate", or "off"
        time_range: Time filter - "d" (day), "w" (week), "m" (month), "y" (year), "" (any)

    Returns:
        Dictionary containing:
        - success: Whether search succeeded
        - results: List of search results, each with:
            - title: Page title
            - url: Page URL
            - body: Relevant excerpt/snippet from the page
        - query: The original search query
        - total_results: Number of results returned

    IMPORTANT: After using this tool:
    1. Read through the 'body' field of each result
    2. Extract relevant information that answers the user's question
    3. Synthesize this into a clear, natural language response
    4. Cite sources by mentioning the page titles or URLs
    5. NEVER show the raw JSON to the user - always provide a formatted response

    Example:
        duckduckgo_search("Python asyncio tutorial")
        duckduckgo_search("latest news AI", time_range="w")
    """
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # Fallback to old package name
        except ImportError:
            return {
                "success": False,
                "error": "ddgs not installed. Install with: uv add ddgs",
                "query": query,
            }

    # Limit max_results to reasonable bounds
    max_results = min(max(1, max_results), 20)

    try:
        with DDGS() as ddgs:
            results = list(
                ddgs.text(
                    query,
                    region=region,
                    safesearch=safesearch,
                    timelimit=time_range if time_range else None,
                    max_results=max_results,
                )
            )

        # Format results to match expected structure
        formatted_results = [
            {
                "title": r.get("title", ""),
                "url": r.get("href", r.get("link", "")),
                "body": r.get("body", r.get("snippet", "")),
            }
            for r in results
        ]

        return {
            "success": True,
            "results": formatted_results,
            "query": query,
            "total_results": len(formatted_results),
        }

    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "error": f"DuckDuckGo search error: {e!s}",
            "query": query,
        }

