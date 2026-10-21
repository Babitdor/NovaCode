"""Legacy X/Twitter scraper functions, kept outside the registered tool set."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain.tools import tool

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]


_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _USER_AGENT}



# ===================================================================
# X / Twitter  (REST API for trending, GraphQL for search)
# ===================================================================

_GUEST_TOKEN_CACHE: str | None = None
_BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)


def _get_x_guest_token(session: requests.Session | None = None) -> str:
    """Obtain a guest token for unauthenticated X.com API access."""
    global _GUEST_TOKEN_CACHE
    if _GUEST_TOKEN_CACHE:
        return _GUEST_TOKEN_CACHE

    s = session or requests.Session()
    resp = s.post(
        "https://api.twitter.com/1.1/guest/activate.json",
        headers={"Authorization": f"Bearer {_BEARER_TOKEN}"},
        timeout=15,
    )
    resp.raise_for_status()
    _GUEST_TOKEN_CACHE = resp.json()["guest_token"]
    return _GUEST_TOKEN_CACHE


def _x_auth_headers(session: requests.Session) -> dict[str, str]:
    """Build auth headers for X.com API calls."""
    headers: dict[str, str] = {
        "authorization": f"Bearer {_BEARER_TOKEN}",
        "x-guest-token": _get_x_guest_token(session),
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "content-type": "application/json",
        "origin": "https://x.com",
        "referer": "https://x.com/",
    }
    ct0 = session.cookies.get("ct0")
    if ct0:
        headers["x-csrf-token"] = ct0
    return headers


def _scrape_twitter_trending(max_count: int = 20) -> list[dict[str, Any]]:
    """Fetch trending topics from X.com REST API (no login needed)."""
    if requests is None:
        return []

    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT})
    session.get("https://x.com/", timeout=15)
    headers = _x_auth_headers(session)

    try:
        resp = session.get(
            "https://api.x.com/1.1/trends/place.json?id=1",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    trends: list[dict[str, Any]] = []
    for rank, trend in enumerate(data[0].get("trends", []), 1):
        if len(trends) >= max_count:
            break
        name = trend.get("name", "")
        if not name:
            continue

        volume = trend.get("tweet_volume")
        try:
            volume = int(volume) if volume else None
        except (ValueError, TypeError):
            volume = None

        query = trend.get("query", "")
        url = f"https://x.com/search?q={query}&src=trend_click" if query else ""

        trends.append({
            "name": name,
            "post_count": volume,
            "category": "Trending",
            "url": url,
            "rank": rank,
        })

    return trends


@tool
def twitter_trending(
    max_count: int = 20,
) -> dict[str, Any]:
    """Scrape X/Twitter trending topics (no login required).

    Uses X.com's REST API (trends/place.json) to fetch current trending
    topics with post count and rank.

    Args:
        max_count: Maximum number of trending topics to return (default 20).

    Returns:
        Dict with "trends" (list of trend dicts) and "count".
    """
    try:
        trends = _scrape_twitter_trending(max_count=max_count)
    except Exception as e:
        return {"error": f"Twitter trending failed: {e!s}", "trends": [], "count": 0}

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {"trends": trends, "count": len(trends), "scraped_at": ts}


_SEARCH_QUERY_ID = "dsWn-Op2S0SmJjgY6Yvckg"  # SearchTimeline


def _parse_tweets_from_timeline(data: dict) -> list[dict[str, Any]]:
    """Parse tweets from GraphQL SearchTimeline response."""
    tweets: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    instructions = (
        data.get("data", {})
        .get("search_by_raw_query", {})
        .get("search_timeline", {})
        .get("timeline", {})
        .get("instructions", [])
    )

    for instruction in instructions:
        entries = instruction.get("entries", [])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("entryId", "")
            if not entry_id or "tweet" not in entry_id:
                continue

            tweet_id = entry_id.removeprefix("tweet-").split("|")[0]
            if tweet_id in seen_ids:
                continue
            seen_ids.add(tweet_id)

            result = (
                entry.get("content", {})
                .get("itemContent", {})
                .get("tweetResult", {})
                .get("result", {})
            )
            if not result:
                continue

            legacy = result.get("legacy", {})
            if not legacy:
                continue
            user = (
                result.get("core", {})
                .get("user_results", {})
                .get("result", {})
                .get("legacy", {})
            )

            text = legacy.get("full_text", "") or legacy.get("text", "")
            if not text:
                continue

            text_cleaned = re.sub(r"[\U0001F300-\U0010FFFF]", "", text)
            text_cleaned = re.sub(r"\s+", " ", text_cleaned).strip()

            permalink = f"https://x.com/{user.get('screen_name', '')}/status/{tweet_id}"

            view_count = 0
            ext_views = legacy.get("ext", {}).get("views", {}).get("count", "")
            if ext_views:
                try:
                    view_count = int(ext_views)
                except (ValueError, TypeError):
                    view_count = 0

            media = (
                legacy.get("entities", {}).get("media", [])
                or legacy.get("extended_entities", {}).get("media", [])
            )

            timestamp = ""
            created_at = legacy.get("created_at", "")
            if created_at:
                try:
                    dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
                    timestamp = dt.isoformat()
                except ValueError:
                    timestamp = created_at

            tweets.append({
                "id": tweet_id,
                "author_name": user.get("name", ""),
                "author_handle": f"@{user.get('screen_name', '')}",
                "author_avatar": user.get("profile_image_url_https", ""),
                "text": text,
                "text_cleaned": text_cleaned[:500],
                "timestamp": timestamp,
                "permalink": permalink,
                "reply_count": legacy.get("reply_count", 0) or 0,
                "retweet_count": legacy.get("retweet_count", 0) or 0,
                "like_count": legacy.get("favorite_count", 0) or 0,
                "view_count": view_count,
                "is_verified": user.get("verified", False),
                "has_media": len(media) > 0,
            })

    return tweets


@tool
def twitter_search(
    query: str,
    cookies_path: str = "",
    from_user: str = "",
    max_count: int = 20,
) -> dict[str, Any]:
    """Search X/Twitter for tweets matching a query (requires auth cookies).

    X.com's GraphQL search endpoint requires authentication. Provide a path
    to a cookies JSON file exported from a logged-in browser session.
    The cookies file should contain a list of {name, value} objects including
    'auth_token' and 'ct0'.

    Args:
        query: Search query string.
        cookies_path: Path to a JSON file with X.com auth cookies.
        from_user: Optional username to filter by (without @).
        max_count: Maximum number of tweets to return (default 20, max 50).

    Returns:
        Dict with "tweets" (list of tweet dicts) and "count".
    """
    if not cookies_path:
        return {
            "error": "X.com search requires authentication cookies. "
                     "Pass 'cookies_path' pointing to a JSON file exported "
                     "from a logged-in browser session (must include auth_token).",
            "tweets": [],
            "count": 0,
        }

    # Load cookies
    try:
        raw = Path(cookies_path).read_text(encoding="utf-8")
        cookies_data = json.loads(raw)
        if isinstance(cookies_data, list):
            cookies = {c["name"]: c["value"] for c in cookies_data}
        else:
            cookies = cookies_data
    except Exception as e:
        return {"error": f"Failed to load cookies: {e!s}", "tweets": [], "count": 0}

    if "auth_token" not in cookies:
        return {
            "error": "Cookies must contain 'auth_token' from a logged-in X.com session.",
            "tweets": [],
            "count": 0,
        }

    if requests is None:
        return {"error": "Missing dependency: requests", "tweets": [], "count": 0}

    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT})
    session.cookies.update(cookies)
    _get_x_guest_token(session)  # prime cache

    try:
        session.get("https://x.com/", timeout=15)
    except Exception:
        pass

    headers = _x_auth_headers(session)
    max_count = min(max_count, 50)
    search_query = f"{query} from:{from_user}" if from_user else query

    variables: dict[str, Any] = {
        "rawQuery": search_query,
        "count": min(max_count, 20),
        "cursor": None,
        "querySource": "typed_query",
        "product": "Top",
    }
    features: dict[str, Any] = {
        "rweb_video_screen_enabled": True,
        "rweb_cashtags_enabled": True,
        "profile_label_improvements_pcf_label_in_post_enabled": True,
        "responsive_web_profile_redirect_enabled": True,
        "rweb_tipjar_consumption_enabled": True,
        "verified_phone_label_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "premium_content_api_read_enabled": True,
        "communities_web_enable_tweet_community_results_fetch": True,
        "c9s_tweet_anatomy_moderator_badge_enabled": True,
        "articles_preview_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "tweet_awards_web_tipping_enabled": False,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "responsive_web_enhance_cards_enabled": False,
        "rweb_video_timestamps_enabled": True,
        "responsive_web_grok_analyze_button_fetch_trends_enabled": True,
    }

    try:
        resp = session.post(
            f"https://api.x.com/graphql/{_SEARCH_QUERY_ID}/SearchTimeline",
            json={"variables": variables, "features": features},
            headers=headers,
            timeout=20,
        )
    except Exception as e:
        return {"error": f"Twitter search request failed: {e!s}", "tweets": [], "count": 0}

    if resp.status_code in (401, 403):
        return {"error": "Access forbidden. Cookies may be expired or blocked.", "tweets": [], "count": 0}
    if resp.status_code == 404:
        return {"error": "Search endpoint returned 404. Cookies may be expired.", "tweets": [], "count": 0}
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}", "tweets": [], "count": 0}

    try:
        result = resp.json()
        tweets = _parse_tweets_from_timeline(result)
    except Exception as e:
        return {"error": f"Failed to parse search results: {e!s}", "tweets": [], "count": 0}

    return {
        "tweets": tweets[:max_count],
        "count": min(len(tweets), max_count),
        "query": query,
        "from_user": from_user,
    }