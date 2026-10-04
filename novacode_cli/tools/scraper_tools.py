"""Web scraping tools — GitHub trending, Hacker News, LinkedIn jobs, Reddit.

Each tool wraps a public-data scraper and returns structured results as a
dict, suitable for the agent to read directly or save to a file via the
filesystem backend.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from langchain.tools import tool

# ---------------------------------------------------------------------------
# Optional dependency guards
# ---------------------------------------------------------------------------

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]
    BeautifulSoup = None  # type: ignore[assignment]

try:
    import tenacity
except ImportError:  # pragma: no cover
    tenacity = None  # type: ignore[assignment]


_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _USER_AGENT}


# ===================================================================
# GitHub Trending
# ===================================================================


def _parse_stars(text: str) -> int:
    cleaned = re.sub(r"[^\d,]", "", text)
    return int(cleaned.replace(",", "")) if cleaned else 0


def _fetch_github_trending(
    url: str = "https://github.com/trending",
    language: str = "",
    since: str = "daily",
) -> list[dict[str, Any]]:
    if requests is None or BeautifulSoup is None:
        return {"error": "Missing dependencies: requests, beautifulsoup4"}

    full_url = f"{url}/{language}?since={since}" if language else f"{url}?since={since}"

    resp = requests.get(full_url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    repos: list[dict[str, Any]] = []

    for article in soup.select("article.Box-row"):
        h2 = article.find("h2")
        if not h2:
            continue
        link = h2.find("a")
        if not link:
            continue

        full_name = h2.get_text(" ", strip=True).replace(" / ", "/")
        href = link.get("href", "").strip("/")

        desc_p = article.find("p", class_="col-9")
        description = desc_p.get_text(strip=True) if desc_p else ""

        lang_span = article.find("span", itemprop="programmingLanguage")
        language_name = lang_span.get_text(strip=True) if lang_span else "Unknown"

        star_el = article.find("a", href=re.compile(r"/stargazers$"))
        stars = _parse_stars(star_el.get_text(strip=True)) if star_el else 0

        fork_el = article.find("a", href=re.compile(r"/forks$"))
        forks = _parse_stars(fork_el.get_text(strip=True)) if fork_el else 0

        today_el = article.find("span", class_="d-inline-block")
        today_text = ""
        if today_el:
            txt = today_el.get_text(strip=True)
            if "stars today" in txt:
                today_text = txt

        repos.append(
            {
                "name": full_name,
                "url": f"https://github.com/{href}",
                "description": description,
                "language": language_name,
                "stars": stars,
                "forks": forks,
                "stars_today": today_text,
            }
        )
    return repos


@tool
def github_trending(
    language: str = "",
    since: Literal["daily", "weekly", "monthly"] = "daily",
    max_repos: int = 25,
) -> dict[str, Any]:
    """Scrape GitHub trending repositories.

    Returns a list of trending repos with name, URL, description, language,
    stars, forks, and today's star count.

    Args:
        language: Programming language filter (e.g. "python", "typescript").
                  Empty string means all languages.
        since: Time range — "daily", "weekly", or "monthly".
        max_repos: Maximum number of repos to return (default 25).

    Returns:
        Dict with "repos" (list of repo dicts) and "count".
    """
    try:
        repos = _fetch_github_trending(language=language, since=since)
    except Exception as e:
        return {"error": f"GitHub trending scrape failed: {e!s}", "repos": [], "count": 0}

    repos = repos[:max_repos]
    return {"repos": repos, "count": len(repos), "language": language, "since": since}


# ===================================================================
# Hacker News
# ===================================================================


async def _fetch_hn_headlines(url: str = "https://news.ycombinator.com/") -> list[dict[str, str]]:
    if httpx is None or BeautifulSoup is None:
        return []  # error handled upstream

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

    title_rows = soup.select("span.titleline > a")
    headlines: list[dict[str, str]] = []
    for a_tag in title_rows:
        headlines.append(
            {"title": a_tag.get_text(strip=True), "url": a_tag.get("href", "")}
        )
    return headlines


@tool
def hacker_news(
    max_headlines: int = 30,
) -> dict[str, Any]:
    """Scrape the Hacker News front page for current headlines.

    Returns a list of headlines with title, URL, and timestamp.

    Args:
        max_headlines: Maximum headlines to return (default 30).

    Returns:
        Dict with "headlines" (list of {title, url} dicts) and "count".
    """
    if httpx is None:
        return {"error": "Missing dependency: httpx", "headlines": [], "count": 0}

    try:
        headlines = asyncio.run(_fetch_hn_headlines())
    except Exception as e:
        return {"error": f"HN scrape failed: {e!s}", "headlines": [], "count": 0}

    headlines = headlines[:max_headlines]
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {"headlines": headlines, "count": len(headlines), "scraped_at": ts}


# ===================================================================
# LinkedIn Jobs  (Playwright-based)
# ===================================================================


@tool
def linkedin_jobs(
    keyword: str,
    location: str = "",
    max_pages: int = 1,
) -> dict[str, Any]:
    """Search LinkedIn for job listings (Playwright-based, no login required).

    Scrapes LinkedIn's public job search results for the given keyword and
    location. Returns structured job postings with title, company, location,
    date, and URL.

    Args:
        keyword: Job title or keyword to search (e.g. "AI Engineer").
        location: Location filter (e.g. "United States", "Remote"). Empty for all.
        max_pages: Number of result pages to scrape (default 1, max 5).

    Returns:
        Dict with "jobs" (list of job posting dicts) and "count".
    """
    # Imported here, not at module level: playwright costs ~0.5 s of every
    # start-up and only this one tool uses it.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover
        return {
            "error": "Missing dependency: playwright. Install it with 'pip install playwright && playwright install chromium'",
            "jobs": [],
            "count": 0,
        }

    jobs: list[dict[str, Any]] = []
    max_pages = min(max_pages, 5)  # safety cap

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=_USER_AGENT,
            )
            page = ctx.new_page()

            try:
                for page_num in range(max_pages):
                    start = page_num * 25
                    kw = keyword.replace(" ", "%20")
                    loc = location.replace(" ", "%20") if location else ""
                    url = f"https://www.linkedin.com/jobs/search?keywords={kw}&location={loc}&start={start}"
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1500)

                    try:
                        page.wait_for_selector(
                            "ul.jobs-search__results-list", timeout=10000
                        )
                    except Exception:
                        break

                    items = page.query_selector_all(
                        "ul.jobs-search__results-list > li"
                    )
                    if not items:
                        break

                    for item in items:
                        try:
                            card = item.query_selector("div.base-card")
                            if not card:
                                continue
                            title_el = card.query_selector(
                                "h3.base-search-card__title"
                            )
                            company_el = card.query_selector(
                                "h4.base-search-card__subtitle a"
                            )
                            location_el = card.query_selector(
                                "span.job-search-card__location"
                            )
                            date_el = card.query_selector(
                                "time.job-search-card__listdate--new, "
                                "time.job-search-card__listdate"
                            )
                            link_el = card.query_selector("a.base-card__full-link")

                            title = (
                                title_el.inner_text().strip() if title_el else ""
                            )
                            if not title:
                                continue
                            company = (
                                company_el.inner_text().strip()
                                if company_el
                                else ""
                            )
                            job_location = (
                                location_el.inner_text().strip()
                                if location_el
                                else ""
                            )
                            posted_date = (
                                date_el.get_attribute("datetime") or ""
                                if date_el
                                else ""
                            )
                            if not posted_date and date_el:
                                posted_date = date_el.inner_text().strip()

                            job_url = (
                                link_el.get_attribute("href") if link_el else ""
                            )
                            if job_url:
                                job_url = re.sub(r"\?.*", "", job_url)

                            jobs.append(
                                {
                                    "title": title,
                                    "company": company,
                                    "location": job_location,
                                    "posted_date": posted_date,
                                    "url": job_url,
                                }
                            )
                        except Exception:
                            continue

                    if len(items) < 25:
                        break
                    time.sleep(1.0)

            finally:
                ctx.close()
                browser.close()
    except Exception as e:
        return {
            "error": f"LinkedIn scrape failed: {e!s}",
            "jobs": jobs,
            "count": len(jobs),
        }

    return {
        "jobs": jobs,
        "count": len(jobs),
        "keyword": keyword,
        "location": location,
    }


# ===================================================================
# Reddit
# ===================================================================


def _parse_score(text: str | None) -> int | None:
    if not text or text.strip() in ("•", ""):
        return None
    m = re.search(r"[\d,.]+", text.replace(",", ""))
    return int(m.group()) if m else None


def _parse_comments(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else 0


def _get_reddit_url(
    subreddit: str = "",
    user: str = "",
    search: str = "",
    feed: str = "hot",
    top: bool = False,
) -> str:
    if search:
        query = "+".join(search.split())
        return f"https://old.reddit.com/search?q={query}&restrict_sr=off&sort=new"
    if user:
        feed_path = "submitted" if not top else "top"
        return f"https://old.reddit.com/user/{user}/{feed_path}/"
    return f"https://old.reddit.com/r/{subreddit}/{feed}/"


def _fetch_reddit(
    url: str,
    max_pages: int = 1,
) -> list[dict[str, Any]]:
    if requests is None or BeautifulSoup is None:
        return []

    cookies = {"over18": "1"}
    all_posts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    current_url: str | None = url

    for _page in range(max_pages):
        if not current_url:
            break
        try:
            resp = requests.get(
                current_url, headers=_HEADERS, cookies=cookies, timeout=15
            )
            resp.raise_for_status()
        except requests.RequestException:
            break

        soup = BeautifulSoup(resp.text, "html.parser")

        for thing in soup.select("div[id^='thing_']"):
            title_link = thing.select_one("a.title")
            if not title_link:
                continue
            pid = thing.get("id", "").removeprefix("thing_")
            if pid in seen_ids:
                continue
            seen_ids.add(pid)

            author_el = thing.select_one("a.author")
            score_el = thing.select_one("div.score.unvoted")
            comments_el = thing.select_one("a.comments")

            score = (
                _parse_score(score_el.get_text(strip=True))
                if score_el
                else None
            )
            comments = (
                _parse_comments(comments_el.get_text(strip=True))
                if comments_el
                else None
            )

            href = title_link.get("href", "")
            permalink = (
                f"https://reddit.com{href}" if href.startswith("/r/") else href
            )

            flair_el = thing.select_one("span.flair")
            flair = flair_el.get_text(strip=True) if flair_el else ""

            all_posts.append(
                {
                    "id": pid,
                    "title": title_link.get_text(strip=True),
                    "url": href,
                    "permalink": permalink,
                    "author": author_el.get_text(strip=True)
                    if author_el
                    else None,
                    "score": score,
                    "num_comments": comments,
                    "flair": flair,
                }
            )

        next_el = soup.select_one("span.next-button a[rel='next']")
        current_url = next_el.get("href") if next_el else None

    return all_posts


@tool
def reddit_posts(
    subreddit: str = "",
    user: str = "",
    search: str = "",
    feed: Literal["hot", "new", "top", "submitted"] = "hot",
    max_pages: int = 1,
    keyword_filter: str = "",
    author_filter: str = "",
    top_score: bool = False,
) -> dict[str, Any]:
    """Scrape Reddit posts from old.reddit.com (no API key required).

    Supports scraping by subreddit, user, or search query. Returns structured
    post data with title, score, comments, author, and permalink.

    Args:
        subreddit: Subreddit name (e.g. "python", "machinelearning").
        user: Reddit username to scrape posts from.
        search: Keyword search across Reddit.
        feed: Post sorting — "hot", "new", "top", or "submitted" (user only).
        max_pages: Number of result pages to scrape (default 1, max 5).
        keyword_filter: Only include posts whose title matches this keyword (case-insensitive).
        author_filter: Only include posts by this author (case-insensitive).
        top_score: Sort results by score descending.

    Returns:
        Dict with "posts" (list of post dicts) and "count".
    """
    if requests is None or BeautifulSoup is None:
        return {
            "error": "Missing dependencies: requests, beautifulsoup4",
            "posts": [],
            "count": 0,
        }

    if not subreddit and not user and not search:
        return {
            "error": "Specify one of: subreddit, user, or search.",
            "posts": [],
            "count": 0,
        }

    max_pages = min(max_pages, 5)

    url = _get_reddit_url(subreddit, user, search, feed, top_score)
    feed_label = search or f"r/{subreddit}" or f"u/{user}"

    try:
        posts = _fetch_reddit(url, max_pages=max_pages)
    except Exception as e:
        return {"error": f"Reddit scrape failed: {e!s}", "posts": [], "count": 0}

    # Apply filters
    if keyword_filter:
        pattern = re.compile(re.escape(keyword_filter), re.IGNORECASE)
        posts = [p for p in posts if pattern.search(p["title"])]
    if author_filter:
        pattern = re.compile(re.escape(author_filter), re.IGNORECASE)
        posts = [p for p in posts if p["author"] and pattern.search(p["author"])]

    if top_score:
        valid = [p for p in posts if p["score"] is not None]
        valid.sort(key=lambda p: p["score"], reverse=True)
        posts = valid

    return {
        "posts": posts,
        "count": len(posts),
        "source": feed_label,
        "feed": feed,
    }


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