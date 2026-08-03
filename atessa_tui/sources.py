"""Optional local providers for Search and Read.

Every function degrades to a clear error string instead of raising when its
optional dependency is absent, so panes keep working on a bare install.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request

HN_API = "https://hn.algolia.com/api/v1/search"
GITHUB_API = "https://api.github.com/search/issues"
STACKEXCHANGE_API = "https://api.stackexchange.com/2.3/search/advanced"
DEVTO_API = "https://dev.to/api/articles"
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}

MAX_SOURCE_BYTES = 5 * 1024 * 1024  # 5 MB
SOURCE_TIMEOUT = 15.0


def is_safe_url(url: str) -> bool:
    """Validate that a URL targets a public HTTP/HTTPS endpoint and not private/loopback/link-local networks."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        if hostname.startswith("[") and hostname.endswith("]"):
            hostname = hostname[1:-1]
        addr_info = socket.getaddrinfo(hostname, None)
        if not addr_info:
            return False
        for info in addr_info:
            ip_str = info[4][0]
            ip = ipaddress.ip_address(ip_str)
            if (
                ip.is_loopback
                or ip.is_private
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
                or ip in ipaddress.ip_network("100.64.0.0/10")
                or ip in ipaddress.ip_network("0.0.0.0/8")
            ):
                return False
        return True
    except Exception:
        return False


def _get_json(url: str, timeout: float = SOURCE_TIMEOUT) -> dict:
    if not is_safe_url(url):
        raise ValueError(f"URL destination is not permitted: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "atessa-workbench"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError(f"Response from {url} exceeded maximum allowed size (5MB)")
        return json.loads(raw.decode("utf-8", errors="replace"))

def web_results(query: str, limit: int) -> list[dict[str, str]]:
    """Zero-config web results via DuckDuckGo."""
    try:
        from ddgs import DDGS
    except ImportError:
        raise RuntimeError("ddgs package is not installed (pip install ddgs)")

    try:
        rows = DDGS().text(query, max_results=limit)
        return [
            {
                "title": row.get("title", ""),
                "url": row.get("href", ""),
                "snippet": row.get("body", ""),
            }
            for row in (rows or [])
        ]
    except Exception as exc:
        raise RuntimeError(f"DuckDuckGo search failed: {exc}") from exc
_SEARCH_STOPWORDS = {
    "a", "all", "an", "and", "are", "best", "can", "for", "from", "how", "i",
    "in", "is", "like", "my", "of", "on", "or", "the", "their", "to", "what",
    "where", "with", "you", "your",
}


def _query_terms(query: str) -> tuple[str, ...]:
    return tuple(
        token
        for token in re.findall(r"[a-z0-9]+", query.casefold())
        if len(token) >= 3 and token not in _SEARCH_STOPWORDS
    )


def _term_matches(term: str, token: str) -> bool:
    return term == token or (
        len(term) >= 5 and token.startswith(term)
    ) or (len(token) >= 5 and term.startswith(token))


def _relevant_result(row: dict[str, str], query: str) -> bool:
    """Reject search-engine noise that does not mention the requested subject."""
    terms = _query_terms(query)
    if not terms:
        return True
    tokens = re.findall(
        r"[a-z0-9]+",
        f"{row.get('title', '')} {row.get('snippet', '')}".casefold(),
    )
    matched = {
        term for term in terms if any(_term_matches(term, token) for token in tokens)
    }
    required = 2 if len(terms) >= 4 else 1
    return len(matched) >= required


def _result_url(url: str, site: str) -> bool:
    """Keep real content pages, excluding host homepages and index noise."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in ("http", "https") or host not in {
        site.casefold(), f"www.{site.casefold()}"
    }:
        return False
    path = parsed.path.rstrip("/")
    if site == "reddit.com":
        return bool(re.search(r"/r/[^/]+/comments/[a-z0-9]+/[^/]+$", path, re.I))
    if site.startswith("discuss.") or site == "community.openai.com":
        return bool(re.search(r"/t/[^/]+(?:/\d+)?$", path, re.I))
    return bool(path) and path not in {"/search", "/latest"}

def site_results(query: str, site: str, limit: int) -> list[dict[str, str]]:
    """Web results constrained to one site, used for Reddit-style sources."""
    candidates = web_results(f"{query} site:{site}", max(limit * 4, 10))
    rows = [
        row
        for row in candidates
        if _result_url(str(row.get("url") or ""), site)
        and _relevant_result(row, query)
    ][:limit]
    if site != "reddit.com":
        return rows
    for row in rows:
        title, subreddit = _reddit_title(row["url"])
        current_title = str(row.get("title") or "").strip()
        if title and (not current_title or current_title.casefold() in {"reddit.com", "link to reddit.com"}):
            row["title"] = title
        if subreddit:
            snippet = row.get("snippet", "")
            if not snippet or "hides the web page description" in snippet:
                snippet = "Reddit thread"
            row["snippet"] = f"r/{subreddit} · {snippet}"
    return rows


def _reddit_title(url: str) -> tuple[str, str]:
    """Recover a readable title and subreddit from a Reddit permalink."""
    match = re.search(r"/r/([^/]+)/comments/[^/]+/([^/?#]+)", url)
    if not match:
        return "", ""
    slug = match.group(2).replace("_", " ").strip()
    return slug[:1].upper() + slug[1:], match.group(1)


def hackernews_results(query: str, limit: int) -> list[dict[str, str]]:
    """Hacker News discussions via the public Algolia index."""
    url = f"{HN_API}?{urllib.parse.urlencode({'query': query, 'hitsPerPage': limit})}"
    hits = _get_json(url).get("hits", [])
    out: list[dict[str, str]] = []
    for hit in hits[:limit]:
        title = hit.get("title") or hit.get("story_title") or ""
        if not title:
            continue
        out.append(
            {
                "title": title,
                "url": f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}",
                "snippet": f"{hit.get('points', 0)} points · {hit.get('num_comments', 0)} comments",
            }
        )
    return out


def github_results(query: str, limit: int) -> list[dict[str, str]]:
    """Issue and pull-request matches via the public GitHub search API."""
    params = {"q": query, "per_page": limit, "sort": "updated", "order": "desc"}
    data = _get_json(f"{GITHUB_API}?{urllib.parse.urlencode(params)}")
    return [
        {
            "title": f"{item.get('repository_url', '').rsplit('/', 2)[-2]}/{item.get('repository_url', '').rsplit('/', 1)[-1]} · {item.get('title', '')}",
            "url": item.get("html_url", ""),
            "snippet": (
                f"{item.get('state', '')} · {item.get('comments', 0)} comments · "
                f"{re.sub(r'<[^>]+>', '', item.get('body') or '')[:300]}"
            ),
        }
        for item in data.get("items", [])[:limit]
    ]


def stackoverflow_results(query: str, limit: int) -> list[dict[str, str]]:
    """Recent Stack Overflow questions via the public Stack Exchange API."""
    params = {
        "site": "stackoverflow",
        "q": query,
        "pagesize": limit,
        "order": "desc",
        "sort": "activity",
        "filter": "default",
    }
    data = _get_json(f"{STACKEXCHANGE_API}?{urllib.parse.urlencode(params)}")
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": (
                f"score {item.get('score', 0)} · "
                f"{'answered' if item.get('is_answered') else 'unanswered'} · "
                + ", ".join(item.get("tags", [])[:4])
            ),
        }
        for item in data.get("items", [])[:limit]
    ]


def devto_results(query: str, limit: int) -> list[dict[str, str]]:
    """Recent practical engineering posts via the public DEV Community API."""
    params = {"per_page": limit, "top": 30, "tag": query.split()[0].lower()}
    try:
        items = _get_json(f"{DEVTO_API}?{urllib.parse.urlencode(params)}")
    except Exception:
        return site_results(query, "dev.to", limit)
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("description", ""),
        }
        for item in items[:limit]
    ]


def youtube_search_results(query: str, limit: int) -> list[dict[str, str]]:
    """Discover videos from YouTube's public search page, without an API key."""
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode(
        {"search_query": query}
    )
    if not is_safe_url(url):
        raise ValueError(f"URL destination is not permitted: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=SOURCE_TIMEOUT) as response:
        raw = response.read(MAX_SOURCE_BYTES)
        html = raw.decode("utf-8", "replace")
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r'"videoId":"([\w-]{11})"', html):
        video_id = match.group(1)
        if video_id in seen:
            continue
        seen.add(video_id)
        window = html[match.start() : match.start() + 6000]
        title_match = re.search(r'"title":\{"runs":\[\{"text":"([^"]+)', window)
        title = (
            json.loads(f'"{title_match.group(1)}"')
            if title_match
            else f"YouTube video {video_id}"
        )
        results.append(
            {
                "title": title,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "snippet": "",
            }
        )
        if len(results) >= limit:
            break
    return results

def youtube_transcript_results(query: str, limit: int) -> list[dict[str, str]]:
    """Discover YouTube videos, then attach searchable transcript excerpts."""
    try:
        videos = youtube_search_results(query, max(limit * 2, limit))
    except Exception:
        videos = []
    results: list[dict[str, str]] = []
    for video in videos:
        url = video.get("content") or video.get("url") or video.get("href") or ""
        if not youtube_id(url):
            continue
        try:
            transcript = youtube_transcript(url)
        except Exception:
            transcript = ""
        excerpt = re.sub(r"\s+", " ", transcript).strip()[:800]
        results.append(
            {
                "title": video.get("title", ""),
                "url": url,
                "snippet": (
                    f"Transcript: {excerpt}" if excerpt else video.get("description", "")
                ),
            }
        )
        if len(results) >= limit:
            break
    return results


def discourse_results(query: str, limit: int) -> list[dict[str, str]]:
    """Framework support discussions without relying on one search backend."""
    sites = (
        "discuss.pytorch.org",
        "discuss.huggingface.co",
        "community.openai.com",
    )
    rows: list[dict[str, str]] = []
    errors: list[str] = []
    for site in sites:
        try:
            rows.extend(site_results(query, site, limit))
        except Exception as exc:
            errors.append(f"{site}: {exc}")
            continue

    if not rows and errors:
        raise RuntimeError("Support forums search failed: " + "; ".join(errors))
    return rows[:limit]

def youtube_comment_results(query: str, limit: int) -> list[dict[str, str]]:
    """Discover videos and load a small bounded sample of top comments."""
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        raise RuntimeError("yt-dlp package is not installed (pip install yt-dlp)")

    videos = youtube_search_results(query, min(limit, 3))
    results: list[dict[str, str]] = []
    options = {
        "skip_download": True,
        "getcomments": True,
        "extractor_args": {"youtube": {"max_comments": ["20"]}},
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
    }
    with YoutubeDL(options) as ydl:
        for video in videos:
            url = video.get("url", "")
            if not youtube_id(url):
                continue
            try:
                info = ydl.extract_info(url, download=False)
            except Exception:
                continue
            comments = [
                re.sub(r"\s+", " ", comment.get("text", "")).strip()
                for comment in (info.get("comments") or [])
                if comment.get("text")
            ]
            results.append(
                {
                    "title": video.get("title", ""),
                    "url": url,
                    "snippet": "Comments: " + " | ".join(comments[:10])[:1000],
                }
            )
            if len(results) >= limit:
                break
    return results

SEARCH_SOURCES: dict[str, str] = {
    "all": "All sources",
    "reddit": "Reddit",
    "stackoverflow": "Stack Overflow",
    "github": "GitHub issues",
    "youtube_transcripts": "YouTube transcripts",
    "youtube_comments": "YouTube comments",
    "discourse": "Support forums",
    "devto": "DEV Community",
    "hackernews": "Hacker News",
    "web": "General web",
}

ALL_SOURCE_ORDER = (
    "reddit",
    "stackoverflow",
    "github",
    "youtube_transcripts",
    "discourse",
    "hackernews",
)


def all_source_results(query: str, per_source: int) -> list[dict[str, str]]:
    """Query every reliable source and label each result with its origin."""
    results: list[dict[str, str]] = []
    errors: list[str] = []
    for name in ALL_SOURCE_ORDER:
        label = SEARCH_SOURCES.get(name, name).removesuffix(" only")
        try:
            rows = search_source(name, query, per_source)
            for row in rows:
                results.append({**row, "source": label})
        except Exception as exc:
            errors.append(f"{label}: {exc}")

    if not results and errors:
        raise RuntimeError("All search sources failed:\n" + "\n".join(errors))

    if errors:
        for err in errors:
            src_name = err.split(":")[0]
            results.append({
                "title": f"Provider error ({src_name})",
                "url": "",
                "snippet": err,
                "source": src_name,
            })
    return results

def search_source(source: str, query: str, limit: int) -> list[dict[str, str]]:
    """Fetch results for one named source."""
    if source == "web":
        return web_results(query, limit)
    if source == "reddit":
        return site_results(query, "reddit.com", limit)
    if source == "youtube_transcripts":
        return youtube_transcript_results(query, limit)
    if source == "youtube_comments":
        return youtube_comment_results(query, limit)
    if source == "hackernews":
        return hackernews_results(query, limit)
    if source == "all":
        return all_source_results(query, limit)
    if source == "github":
        return github_results(query, limit)
    if source == "stackoverflow":
        return stackoverflow_results(query, limit)
    if source == "discourse":
        return discourse_results(query, limit)
    if source == "devto":
        return devto_results(query, limit)
    raise ValueError(f"unknown source: {source}")


def youtube_id(url: str) -> str:
    """Extract a video id from a YouTube URL, or return an empty string."""
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc not in _YOUTUBE_HOSTS:
        return ""
    if parsed.netloc == "youtu.be":
        return parsed.path.lstrip("/").split("/")[0]
    if parsed.path == "/watch":
        return urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
    match = re.match(r"^/(?:embed|shorts|live)/([\w-]+)", parsed.path)
    return match.group(1) if match else ""


def youtube_transcript(url: str) -> str:
    """Return a video transcript as plain paragraphs."""
    from youtube_transcript_api import YouTubeTranscriptApi

    video_id = youtube_id(url)
    if not video_id:
        raise ValueError("not a recognisable YouTube video URL")
    fetched = YouTubeTranscriptApi().fetch(video_id)
    lines = [snippet.text.strip() for snippet in fetched if snippet.text.strip()]
    return "\n".join(lines)


def feed_entries(source: str, limit: int = 20) -> str:
    """Render an RSS or Atom feed as Markdown from feed text or a URL."""
    try:
        import feedparser
    except ImportError:
        raise RuntimeError("feedparser is not installed (pip install feedparser)")

    if isinstance(source, str) and (source.startswith("http://") or source.startswith("https://")):
        if not is_safe_url(source):
            raise ValueError(f"URL destination is not permitted: {source}")

    parsed = feedparser.parse(source)
    if not parsed.entries:
        raise ValueError("no feed entries found")
    title = getattr(parsed.feed, "title", "Feed")
    lines = [f"# {title}", ""]
    for entry in parsed.entries[:limit]:
        name = getattr(entry, "title", "(untitled)")
        link = getattr(entry, "link", "")
        published = getattr(entry, "published", "")
        lines.append(f"- [{name}]({link})" + (f"  \n  {published}" if published else ""))
    return "\n".join(lines)


def looks_like_feed(url: str, body: str) -> bool:
    """Detect a feed by URL shape or document root element."""
    if re.search(r"(?i)(?:/(?:rss|feed|atom)(?:\.xml)?/?$|\.(?:rss|atom)$)", url):
        return True
    head = body[:600].lstrip()
    if head.startswith("<?xml"):
        head = head.split("?>", 1)[-1].lstrip()
    return bool(re.match(r"(?i)<(?:rss\b|feed\b|rdf:RDF\b)", head))


def extract_article(html: str, url: str = "") -> str:
    """Extract the main article from raw HTML as Markdown."""
    import trafilatura

    text = trafilatura.extract(
        html,
        url=url or None,
        output_format="markdown",
        include_links=True,
        include_tables=True,
    )
    if not text or not text.strip():
        raise ValueError("no main article content found")
    return text.strip()
