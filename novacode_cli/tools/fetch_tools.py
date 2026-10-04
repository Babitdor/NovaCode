"""URL fetching tools.

This module provides tools for fetching web content and converting to markdown.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import requests
from langchain.tools import tool

from novacode_cli.tools._shared import (
    _BROWSER_USER_AGENTS,
    _get_fetch_session,
    _secure_random,
)


def _summarize_web_content(
    content: str,
    max_length: int = 8000,
    focus_query: str | None = None,
) -> dict[str, Any]:
    """Compress large web content to fit token limits, optionally focused on a query."""
    if not content:
        return {
            "success": False,
            "error": "No content to summarize",
            "original_length": 0,
            "summarized_length": 0,
        }

    original_length = len(content)

    # If content is already small enough, return as-is
    if original_length <= max_length:
        return {
            "success": True,
            "summarized_content": content,
            "original_length": original_length,
            "summarized_length": original_length,
            "compression_ratio": 1.0,
        }

    # Extract key sections
    lines = content.split("\n")
    sections: list[dict[str, str]] = []
    current_section: dict[str, str] = {"title": "Intro", "content": ""}

    for line in lines:
        # Track code blocks separately
        if line.strip().startswith("```"):
            if current_section["content"].strip():
                sections.append(current_section.copy())
                current_section = {"title": "", "content": ""}
            # Extract code block
            continue

        # Track headers as section boundaries
        if line.startswith("#"):
            if current_section["content"].strip():
                sections.append(current_section.copy())
            current_section = {"title": line.strip("# ").strip(), "content": ""}
            continue

        current_section["content"] += line + "\n"

    # Add final section
    if current_section["content"].strip():
        sections.append(current_section)

    # Score sections by relevance
    def score_section(section: dict[str, str]) -> float:
        score = 0.0
        text = section["title"] + " " + section["content"]

        # Boost for headers/keywords
        if section["title"]:
            score += 10.0

        # Boost for focus query matches
        if focus_query:
            query_terms = focus_query.lower().split()
            for term in query_terms:
                score += text.lower().count(term) * 2.0

        # Boost for important patterns
        important_patterns = [
            "api",
            "example",
            "usage",
            "install",
            "config",
            "important",
            "note",
            "warning",
            "parameter",
            "return",
            "argument",
        ]
        for pattern in important_patterns:
            score += text.lower().count(pattern) * 0.5

        return score

    # Sort sections by score
    scored_sections = [(score_section(s), s) for s in sections]
    scored_sections.sort(key=lambda x: x[0], reverse=True)

    # Build summarized content
    summarized_parts: list[str] = []
    current_length = 0

    # Always include first section (intro)
    if sections:
        intro = sections[0]
        summarized_parts.append(f"## {intro['title']}\n{intro['content']}")
        current_length += len(intro["content"])

    # Add highest-scored sections until we hit max_length
    for _score, section in scored_sections[1:]:
        section_text = f"## {section['title']}\n{section['content']}"
        if current_length + len(section_text) < max_length:
            summarized_parts.append(section_text)
            current_length += len(section_text)
        else:
            # Truncate section if needed
            remaining = max_length - current_length - 100
            if remaining > 200:
                truncated = section["content"][:remaining] + "\n...[truncated]"
                summarized_parts.append(f"## {section['title']}\n{truncated}")
            break

    summarized_content = "\n\n".join(summarized_parts)
    summarized_length = len(summarized_content)

    return {
        "success": True,
        "summarized_content": summarized_content,
        "original_length": original_length,
        "summarized_length": summarized_length,
        "compression_ratio": round(summarized_length / original_length, 2),
        "sections_analyzed": len(sections),
        "sections_included": len(summarized_parts),
    }


def _convert_github_to_raw_url(url: str) -> tuple[str, bool]:
    """Convert GitHub page URLs to raw content URLs for better fetching.

    Handles:
    - Blob URLs: https://github.com/owner/repo/blob/branch/path -> raw.githubusercontent.com
    - Tree URLs: https://github.com/owner/repo/tree/branch/path -> raw.githubusercontent.com
    - Raw URLs: Already raw, return as-is

    Args:
        url: The GitHub URL to convert

    Returns:
        Tuple of (converted_url, was_converted)
    """
    # Pattern: https://github.com/owner/repo/blob/branch/path
    blob_match = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)", url)
    if blob_match:
        owner, repo, branch, path = blob_match.groups()
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
        return raw_url, True

    # Pattern: https://github.com/owner/repo/tree/branch/path (for directory listings)
    tree_match = re.match(r"https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)", url)
    if tree_match:
        owner, repo, branch, path = tree_match.groups()
        # For tree URLs, we can't get raw content, but we can try the API
        # Return the API URL for directory contents
        api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"
        return api_url, True

    # Pattern: https://github.com/owner/repo (repo root)
    repo_match = re.match(r"https://github\.com/([^/]+)/([^/]+)/?$", url)
    if repo_match:
        owner, repo = repo_match.groups()
        # Return API URL for repo info
        api_url = f"https://api.github.com/repos/{owner}/{repo}"
        return api_url, True

    # Already a raw URL or not a GitHub URL
    return url, False


@tool
def fetch_url(
    url: str,
    method: str = "GET",
    timeout: int = 30,
    max_retries: int = 3,
    headers: dict[str, str] | None = None,
    data: str | dict | None = None,
    params: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    follow_redirects: bool = True,
    max_content_size: int = 10 * 1024 * 1024,  # 10MB default
    verify_ssl: bool = True,
    user_agent: str = "browser",
    summarize: bool = False,
    summarize_max_length: int = 8000,
    focus_query: str | None = None,
) -> dict[str, Any]:
    """Make HTTP requests with retry logic and content conversion.

    For GET requests to HTML pages, content is converted to clean markdown.
    For other methods or JSON responses, content is returned as parsed JSON or
    raw text with appropriate formatting.

    GitHub URL Support (GET only):
    - Automatically converts GitHub blob URLs to raw content URLs
    - Handles tree URLs via GitHub API for directory listings
    - Handles repo root URLs via GitHub API for repository info

    Args:
        url: The URL to fetch (must be a valid HTTP/HTTPS URL)
        method: HTTP method - GET, POST, PUT, DELETE, PATCH, etc. (default: GET)
        timeout: Request timeout in seconds (default: 30)
        max_retries: Maximum number of retry attempts (default: 3)
        headers: Additional HTTP headers to include
        data: Request body — a dict is sent as JSON, a string is sent as-is
        params: URL query parameters as a dict
        auth: Tuple of (username, password) for basic authentication
        cookies: Dict of cookies to send with the request
        follow_redirects: Whether to follow HTTP redirects (default: True)
        max_content_size: Maximum content size in bytes (default: 10MB)
        verify_ssl: Whether to verify SSL certificates (default: True)
        user_agent: "browser" for real browser UA, "bot" for bot UA, or custom string
        summarize: Whether to summarize large content (default: False, GET only)
        summarize_max_length: Max length for summarized content (default: 8000 chars)
        focus_query: Optional query to focus summarization on relevant sections

    Returns:
        Dictionary containing:
        - success: Whether the request succeeded
        - url: The final URL after redirects
        - status_code: HTTP status code
        - content: Page content (markdown for HTML, parsed JSON for JSON, text otherwise)
        - content_type: MIME type of the response
        - content_length: Length of the content in characters
        - attempts: Number of attempts made

    IMPORTANT: After using this tool:
    1. Read through the content and extract relevant information
    2. Synthesize this into a clear, natural language response
    3. NEVER show raw JSON or markdown to the user unless specifically requested

    Example:
        fetch_url("https://example.com")  # GET, HTML -> markdown
        fetch_url("https://api.example.com/data", params={"q": "test"})  # GET, JSON -> parsed
        fetch_url("https://api.example.com/users", method="POST", data={"name": "John"})
    """
    method = method.upper()

    # Convert GitHub URLs to raw content URLs for better fetching (GET only)
    was_github_url = False
    is_github_api_url = False
    if method == "GET":
        url, was_github_url = _convert_github_to_raw_url(url)
        is_github_api_url = url.startswith("https://api.github.com")

    # Build headers
    request_headers: dict[str, str] = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }

    # For GitHub API URLs, use JSON Accept header
    if is_github_api_url:
        request_headers["Accept"] = "application/vnd.github.v3+json"
    elif method != "GET":
        request_headers["Accept"] = "application/json, text/plain, */*"

    # Set User-Agent
    if user_agent == "browser":
        request_headers["User-Agent"] = _secure_random.choice(_BROWSER_USER_AGENTS)
    elif user_agent == "bot":
        request_headers["User-Agent"] = "Mozilla/5.0 (compatible; DeepAgents/1.0)"
    else:
        request_headers["User-Agent"] = user_agent

    # Merge custom headers
    if headers:
        request_headers.update(headers)

    # Determine whether the response should be converted to markdown.
    # Only GET requests to HTML pages get markdown conversion.
    _convert_to_markdown = method == "GET"

    # Get session with connection pooling
    session = _get_fetch_session()

    last_error: Exception | None = None
    attempts = 0

    for attempt in range(max_retries):
        attempts = attempt + 1

        try:
            # Build request kwargs
            req_kwargs: dict[str, Any] = {
                "timeout": (timeout // 2, timeout),  # (connect timeout, read timeout)
                "allow_redirects": follow_redirects,
                "stream": True,
                "verify": verify_ssl,
            }

            if params:
                req_kwargs["params"] = params

            if auth:
                req_kwargs["auth"] = auth

            if cookies:
                req_kwargs["cookies"] = cookies

            if data is not None:
                if isinstance(data, dict):
                    req_kwargs["json"] = data
                    if "Content-Type" not in request_headers:
                        request_headers["Content-Type"] = "application/json"
                else:
                    req_kwargs["data"] = data

            response = session.request(
                method,
                url,
                headers=request_headers,
                **req_kwargs,
            )

            # Check for HTTP errors (4xx, 5xx)
            if response.status_code >= 400:
                status_code = response.status_code
                # Retry on server errors (5xx) and rate limiting (429)
                if (status_code >= 500 or status_code == 429) and attempt < max_retries - 1:
                    last_error = requests.exceptions.HTTPError(f"HTTP {status_code}")
                    time.sleep(2 ** (attempt + 1))
                    continue
                # Try to get error details from response
                try:
                    error_content = response.json()
                    error_msg = error_content.get(
                        "message", error_content.get("error", str(error_content))
                    )
                except (ValueError, KeyError):
                    error_msg = response.text[:500] if response.text else response.reason
                return {
                    "success": False,
                    "error": f"HTTP error {status_code}: {error_msg}",
                    "url": url,
                    "status_code": status_code,
                    "attempts": attempts,
                }

            # Check content size before downloading
            content_length_hdr = response.headers.get("Content-Length")
            if content_length_hdr and int(content_length_hdr) > max_content_size:
                return {
                    "success": False,
                    "error": (
                        f"Content too large: "
                        f"{int(content_length_hdr) / 1024 / 1024:.2f}MB "
                        f"exceeds {max_content_size / 1024 / 1024:.2f}MB limit"
                    ),
                    "url": url,
                    "status_code": response.status_code,
                    "attempts": attempts,
                }

            # Check content type for HTML-only markdown conversion
            content_type = response.headers.get("Content-Type", "")
            is_html = any(
                ct in content_type.lower()
                for ct in ["text/html", "application/xhtml", "text/xml"]
            )

            # Only convert to markdown if it's a GET to HTML content
            if _convert_to_markdown and not is_html and not is_github_api_url:
                # Non-HTML content — return raw or JSON-formatted
                raw_text = response.text
                if "application/json" in content_type.lower():
                    try:
                        parsed = json.loads(raw_text)
                        content = f"```json\n{json.dumps(parsed, indent=2)}\n```"
                    except (json.JSONDecodeError, ImportError):  # type: ignore
                        content = raw_text
                else:
                    content = raw_text
                return {
                    "success": True,
                    "url": str(response.url),
                    "content": content,
                    "status_code": response.status_code,
                    "content_length": len(content),
                    "content_type": content_type,
                    "attempts": attempts,
                }

            # Download content with size check
            chunks = []
            total_size = 0
            for chunk in response.iter_content(chunk_size=8192, decode_unicode=True):
                total_size += len(chunk)
                if total_size > max_content_size:
                    return {
                        "success": False,
                        "error": (
                            f"Content exceeded "
                            f"{max_content_size / 1024 / 1024:.2f}MB limit during download"
                        ),
                        "url": url,
                        "status_code": response.status_code,
                        "attempts": attempts,
                    }
                chunks.append(chunk)

            content = "".join(chunks)

            # Handle encoding
            if response.encoding and response.encoding.lower() not in ["utf-8", "utf8"]:
                try:
                    content = content.encode(response.encoding).decode("utf-8", errors="replace")
                except (UnicodeDecodeError, UnicodeEncodeError):
                    pass  # Keep original content

            if _convert_to_markdown and is_html:
                # Convert HTML content to markdown. Imported on first use:
                # markdownify pulls in bs4 and lxml, ~0.4 s of every start-up.
                from markdownify import markdownify

                content = markdownify(content)

            # For JSON responses, format as JSON code block
            if "application/json" in content_type.lower() or is_github_api_url:
                try:
                    parsed = json.loads(content) if isinstance(content, str) else content
                    content = f"```json\n{json.dumps(parsed, indent=2)}\n```"
                except (json.JSONDecodeError, ImportError):  # type: ignore
                    pass  # Keep original content

            # Apply summarization if requested and content is markdown and large
            if summarize and len(content) > summarize_max_length:
                summary_result = _summarize_web_content(
                    content,
                    max_length=summarize_max_length,
                    focus_query=focus_query,
                )
                if summary_result["success"]:
                    return {
                        "success": True,
                        "url": str(response.url),
                        "content": summary_result["summarized_content"],
                        "status_code": response.status_code,
                        "content_length": summary_result["summarized_length"],
                        "content_type": content_type,
                        "attempts": attempts,
                        "summarized": True,
                    }

            return {
                "success": True,
                "url": str(response.url),
                "content": content,
                "status_code": response.status_code,
                "content_length": len(content),
                "content_type": content_type,
                "attempts": attempts,
            }

        except requests.exceptions.Timeout as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))  # Exponential backoff: 2s, 4s, 8s
                continue

        except requests.exceptions.SSLError as e:
            # SSL errors are usually not transient - don't retry
            return {
                "success": False,
                "error": (
                    f"SSL certificate verification failed: {e!s}. "
                    f"Try setting verify_ssl=False if you trust this site."
                ),
                "url": url,
                "attempts": attempts,
            }

        except requests.exceptions.ConnectionError as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue

        except requests.exceptions.HTTPError as e:
            # HTTP errors (4xx, 5xx) - retry on server errors
            if hasattr(e, "response") and e.response is not None:
                status_code = e.response.status_code
                if status_code >= 500 and attempt < max_retries - 1:
                    last_error = e
                    time.sleep(2 ** (attempt + 1))
                    continue
                # Client errors (4xx) don't benefit from retry
                return {
                    "success": False,
                    "error": f"HTTP error {status_code}: {e!s}",
                    "url": url,
                    "status_code": status_code,
                    "attempts": attempts,
                }
            last_error = e

        except Exception as e:  # noqa: BLE001
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue

    # All retries exhausted
    return {
        "success": False,
        "error": f"Failed after {attempts} attempts: {last_error!s}",
        "url": url,
        "attempts": attempts,
    }
