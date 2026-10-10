"""HTTP request tools.

This module provides tools for making HTTP requests to APIs and web services.
"""

from __future__ import annotations

import time
from typing import Any


from novacode_cli.tools._shared import (
    _BROWSER_USER_AGENTS,
    _get_http_session,
    _retry_delay,
    _secure_random,
)


# NOTE: http_request has been merged into fetch_url (fetch_tools.py).
# This function is kept for backward-compatibility imports but is no
# longer registered as an agent tool. Use fetch_url() instead.
def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: str | dict | None = None,
    params: dict[str, str] | None = None,
    timeout: int = 30,
    max_retries: int = 3,
    follow_redirects: bool = True,
    verify_ssl: bool = True,
    auth: tuple[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    max_content_size: int = 10 * 1024 * 1024,  # 10MB default
    user_agent: str = "browser",
    stream: bool = False,
) -> dict[str, Any]:
    """Make HTTP requests to APIs and web services with enhanced reliability.

    Enhanced version with retry logic, connection pooling, and better error handling.

    Args:
        url: Target URL
        method: HTTP method (GET, POST, PUT, DELETE, PATCH, etc.)
        headers: HTTP headers to include
        data: Request body data (string or dict). Dict is sent as JSON.
        params: URL query parameters
        timeout: Request timeout in seconds (default: 30)
        max_retries: Maximum number of retry attempts (default: 3)
        follow_redirects: Whether to follow HTTP redirects (default: True)
        verify_ssl: Whether to verify SSL certificates (default: True)
        auth: Tuple of (username, password) for basic authentication
        cookies: Dictionary of cookies to send
        max_content_size: Maximum response content size in bytes (default: 10MB)
        user_agent: User agent type - "browser" for real browser UA, "bot" for bot UA,
                   or provide custom string (default: "browser")
        stream: Whether to stream the response (useful for large files)

    Returns:
        Dictionary with response data including:
        - success: Whether the request succeeded (status < 400)
        - status_code: HTTP status code
        - headers: Response headers
        - content: Response body (parsed JSON if possible, otherwise text)
        - url: Final URL after redirects
        - attempts: Number of attempts made
        - elapsed_time: Request duration in seconds

    Example:
        # Simple GET request
        http_request("https://api.example.com/data")

        # POST with JSON body
        http_request(
            "https://api.example.com/users",
            method="POST",
            data={"name": "John", "email": "john@example.com"}
        )

        # With authentication
        http_request(
            "https://api.example.com/protected",
            auth=("username", "password")
        )
    """
    timeout = max(1, min(int(timeout), 120))
    max_retries = max(1, min(int(max_retries), 5))
    start_time = time.time()

    # Build headers
    request_headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }

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

    # Get session with connection pooling
    session = _get_http_session()

    last_error: Exception | None = None
    attempts = 0

    for attempt in range(max_retries):
        attempts = attempt + 1
        response = None

        try:
            kwargs: dict[str, Any] = {
                "url": url,
                "method": method.upper(),
                "headers": request_headers,
                "timeout": (timeout // 2, timeout),  # (connect timeout, read timeout)
                "allow_redirects": follow_redirects,
                "verify": verify_ssl,
                "stream": stream or (max_content_size > 0),  # Stream if size limit set
            }

            if params:
                kwargs["params"] = params

            if data:
                if isinstance(data, dict):
                    kwargs["json"] = data
                    # Set Content-Type if not already set
                    if "Content-Type" not in request_headers:
                        request_headers["Content-Type"] = "application/json"
                else:
                    kwargs["data"] = data

            if auth:
                kwargs["auth"] = auth

            if cookies:
                kwargs["cookies"] = cookies

            response = session.request(**kwargs)

            # Check for HTTP errors (4xx, 5xx)
            if response.status_code >= 400:
                status_code = response.status_code
                # Retry on server errors (5xx) and rate limiting (429)
                if (
                    status_code >= 500 or status_code == 429
                ) and attempt < max_retries - 1:
                    delay = _retry_delay(attempt, response.headers.get("Retry-After"))
                    if delay is not None:
                        last_error = Exception(f"HTTP {status_code}")
                        response.close()
                        time.sleep(delay)
                        continue

                # Try to get error details from response
                try:
                    error_content = response.json()
                    error_msg = error_content.get(
                        "message", error_content.get("error", str(error_content))
                    )
                except (ValueError, KeyError):
                    error_msg = response.text[:500] if response.text else response.reason

                elapsed = time.time() - start_time
                return {
                    "success": False,
                    "status_code": status_code,
                    "headers": dict(response.headers),
                    "content": error_msg,
                    "url": str(response.url),
                    "attempts": attempts,
                    "elapsed_time": round(elapsed, 3),
                }

            # Handle streaming or size-limited responses
            if stream or max_content_size > 0:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_content_size:
                    elapsed = time.time() - start_time
                    return {
                        "success": False,
                        "status_code": response.status_code,
                        "headers": dict(response.headers),
                        "content": (
                            f"Response too large: "
                            f"{int(content_length) / 1024 / 1024:.2f}MB "
                            f"exceeds {max_content_size / 1024 / 1024:.2f}MB limit"
                        ),
                        "url": str(response.url),
                        "attempts": attempts,
                        "elapsed_time": round(elapsed, 3),
                    }

                # Download with size check
                chunks = []
                total_size = 0
                for chunk in response.iter_content(chunk_size=8192):
                    total_size += len(chunk)
                    if total_size > max_content_size:
                        elapsed = time.time() - start_time
                        return {
                            "success": False,
                            "status_code": response.status_code,
                            "headers": dict(response.headers),
                            "content": (
                                f"Response exceeded "
                                f"{max_content_size / 1024 / 1024:.2f}MB limit during download"
                            ),
                            "url": str(response.url),
                            "attempts": attempts,
                            "elapsed_time": round(elapsed, 3),
                        }
                    chunks.append(chunk)

                raw_content = b"".join(chunks)
                # Try to decode as text
                try:
                    text_content = raw_content.decode("utf-8")
                except UnicodeDecodeError:
                    # Return base64 for binary content
                    import base64

                    text_content = base64.b64encode(raw_content).decode("ascii")

                # Try to parse as JSON
                try:
                    content = response.json()
                except ValueError:
                    content = text_content
            else:
                # Non-streaming response
                try:
                    content = response.json()
                except ValueError:
                    content = response.text

            elapsed = time.time() - start_time
            return {
                "success": True,
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "content": content,
                "url": str(response.url),
                "attempts": attempts,
                "elapsed_time": round(elapsed, 3),
                "content_type": response.headers.get("Content-Type", ""),
            }

        except Exception as e:  # noqa: BLE001
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(_retry_delay(attempt) or 0)
                continue
        finally:
            if response is not None:
                response.close()

    # All retries exhausted
    elapsed = time.time() - start_time
    return {
        "success": False,
        "status_code": 0,
        "headers": {},
        "content": f"Request failed after {attempts} attempts: {last_error!s}",
        "url": url,
        "attempts": attempts,
        "elapsed_time": round(elapsed, 3),
    }
