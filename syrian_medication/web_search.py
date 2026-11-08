"""Pluggable web search with an SQLite cache.

Providers, tried in order until one returns results:

  * ``brave``   - Brave Search API          (env BRAVE_API_KEY)
  * ``google``  - Google Programmable Search (env GOOGLE_CSE_KEY + GOOGLE_CSE_ID)
  * ``serpapi`` - SerpAPI Google results     (env SERPAPI_KEY)
  * ``yahoo``   - Yahoo HTML results, no key (default fallback; supports ``site:``)
  * ``bing``    - Bing HTML results, no key  (last resort; weak for Arabic / ``site:``)

API providers are only used when their key is set. Keyless HTML providers are
rate limited: a provider that answers with an error or a block page is put on
cooldown and the next one is tried. Every successful answer is cached in the
``web_search_cache`` table so re-runs never repeat a query.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DEFAULT_PROVIDERS = ("brave", "google", "serpapi", "yahoo", "bing")


@dataclasses.dataclass
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""

    @property
    def domain(self) -> str:
        return urllib.parse.urlparse(self.url).netloc.lower().removeprefix("www.")


class SearchBlocked(Exception):
    """The provider refused or rate-limited the request."""


def _brave(session: requests.Session, query: str, limit: int) -> list[SearchResult]:
    key = os.environ.get("BRAVE_API_KEY")
    response = session.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": min(limit, 20)},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
        timeout=20,
    )
    if response.status_code in (401, 403, 429):
        raise SearchBlocked(f"brave HTTP {response.status_code}")
    response.raise_for_status()
    return [
        SearchResult(item.get("url", ""), item.get("title", ""), item.get("description", ""))
        for item in response.json().get("web", {}).get("results", [])
    ]


def _google(session: requests.Session, query: str, limit: int) -> list[SearchResult]:
    response = session.get(
        "https://www.googleapis.com/customsearch/v1",
        params={
            "key": os.environ.get("GOOGLE_CSE_KEY"),
            "cx": os.environ.get("GOOGLE_CSE_ID"),
            "q": query,
            "num": min(limit, 10),
        },
        timeout=20,
    )
    if response.status_code in (403, 429):
        raise SearchBlocked(f"google HTTP {response.status_code}")
    response.raise_for_status()
    return [
        SearchResult(item.get("link", ""), item.get("title", ""), item.get("snippet", ""))
        for item in response.json().get("items", [])
    ]


def _serpapi(session: requests.Session, query: str, limit: int) -> list[SearchResult]:
    response = session.get(
        "https://serpapi.com/search.json",
        params={"engine": "google", "q": query, "num": min(limit, 20), "api_key": os.environ.get("SERPAPI_KEY")},
        timeout=30,
    )
    if response.status_code in (401, 403, 429):
        raise SearchBlocked(f"serpapi HTTP {response.status_code}")
    response.raise_for_status()
    return [
        SearchResult(item.get("link", ""), item.get("title", ""), item.get("snippet", ""))
        for item in response.json().get("organic_results", [])
    ]


def _yahoo(session: requests.Session, query: str, limit: int) -> list[SearchResult]:
    response = session.get(
        "https://search.yahoo.com/search",
        params={"p": query, "n": min(limit, 30)},
        headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"},
        timeout=20,
    )
    if response.status_code in (403, 429, 999):
        raise SearchBlocked(f"yahoo HTTP {response.status_code}")
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    for block in soup.select("div.algo"):
        anchor = block.select_one("h3 a") or block.select_one("a[href]")
        if not anchor:
            continue
        href = anchor.get("href", "")
        match = re.search(r"/RU=([^/]+)/R[KS]=", href)
        url = urllib.parse.unquote(match.group(1)) if match else href
        title_el = block.select_one("h3")
        snippet_el = block.select_one(".compText, p")
        results.append(SearchResult(
            url,
            title_el.get_text(" ", strip=True) if title_el else anchor.get_text(" ", strip=True),
            snippet_el.get_text(" ", strip=True) if snippet_el else "",
        ))
    if not results:
        lowered = response.text.lower()
        no_match = "we did not find results" in lowered or "no results found" in lowered
        if not no_match:
            # Consent page, captcha or throttled blank page rather than a real "no results".
            raise SearchBlocked("yahoo returned no result list")
    return results


def _bing_target(href: str) -> str:
    encoded = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("u", [""])[0]
    if encoded.startswith("a1"):
        payload = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
        try:
            return base64.urlsafe_b64decode(payload).decode("utf-8", "replace")
        except ValueError:
            return href
    return href


def _bing(session: requests.Session, query: str, limit: int) -> list[SearchResult]:
    response = session.get(
        "https://www.bing.com/search",
        params={"q": query, "setlang": "en", "count": min(limit, 30)},
        headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
        timeout=20,
    )
    if response.status_code in (403, 429):
        raise SearchBlocked(f"bing HTTP {response.status_code}")
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    for block in soup.select("li.b_algo"):
        anchor = block.select_one("h2 a")
        if not anchor:
            continue
        snippet = block.select_one(".b_caption p, p")
        results.append(SearchResult(
            _bing_target(anchor.get("href", "")),
            anchor.get_text(" ", strip=True),
            snippet.get_text(" ", strip=True) if snippet else "",
        ))
    return results


PROVIDERS = {
    "brave": (_brave, lambda: bool(os.environ.get("BRAVE_API_KEY"))),
    "google": (_google, lambda: bool(os.environ.get("GOOGLE_CSE_KEY") and os.environ.get("GOOGLE_CSE_ID"))),
    "serpapi": (_serpapi, lambda: bool(os.environ.get("SERPAPI_KEY"))),
    "yahoo": (_yahoo, lambda: True),
    "bing": (_bing, lambda: True),
}


class WebSearch:
    """Cached, rate-limited search over the configured providers."""

    def __init__(
        self,
        connection: sqlite3.Connection | None = None,
        providers: tuple[str, ...] | list[str] = DEFAULT_PROVIDERS,
        delay: float = 2.0,
        cooldown: float = 300.0,
        retry_wait: float = 15.0,
    ) -> None:
        self.connection = connection
        self.providers = [name for name in providers if name in PROVIDERS and PROVIDERS[name][1]()]
        self.delay = delay
        self.cooldown = cooldown
        self.retry_wait = retry_wait
        self.session = requests.Session()
        self._blocked_until: dict[str, float] = {}
        self._last_call: dict[str, float] = {}
        self._lock = threading.Lock()
        self.queries = 0
        self.cache_hits = 0
        if connection is not None:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS web_search_cache (
                    query TEXT PRIMARY KEY,
                    provider TEXT,
                    results_json TEXT,
                    fetched_at TEXT
                )
                """
            )
            connection.commit()

    @property
    def available(self) -> bool:
        return bool(self.providers)

    def _cached(self, query: str) -> list[SearchResult] | None:
        if self.connection is None:
            return None
        row = self.connection.execute(
            "SELECT results_json FROM web_search_cache WHERE query = ?", (query,)
        ).fetchone()
        if row is None:
            return None
        return [SearchResult(**item) for item in json.loads(row[0])]

    def _store(self, query: str, provider: str, results: list[SearchResult]) -> None:
        if self.connection is None:
            return
        self.connection.execute(
            "INSERT OR REPLACE INTO web_search_cache (query, provider, results_json, fetched_at) VALUES (?, ?, ?, ?)",
            (
                query,
                provider,
                json.dumps([dataclasses.asdict(result) for result in results], ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        self.connection.commit()

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Return up to ``limit`` results; [] when every provider failed or found nothing."""
        query = re.sub(r"\s+", " ", query).strip()
        with self._lock:
            cached = self._cached(query)
            if cached is not None:
                self.cache_hits += 1
                return cached[:limit]
            answered = ""
            for name in self.providers:
                if self._blocked_until.get(name, 0) > time.time():
                    continue
                wait = self.delay - (time.time() - self._last_call.get(name, 0))
                if wait > 0:
                    time.sleep(wait)
                self._last_call[name] = time.time()
                function = PROVIDERS[name][0]
                try:
                    try:
                        raw = function(self.session, query, limit)
                    except SearchBlocked:
                        time.sleep(self.retry_wait)  # transient throttling is common; retry once
                        self._last_call[name] = time.time()
                        raw = function(self.session, query, limit)
                    results = [result for result in raw if result.url.startswith("http")]
                except SearchBlocked as error:
                    logger.warning("search provider %s blocked (%s); cooling down %.0fs", name, error, self.cooldown)
                    self._blocked_until[name] = time.time() + self.cooldown
                    continue
                except (requests.RequestException, ValueError) as error:
                    logger.debug("search provider %s failed for %r: %s", name, query, error)
                    continue
                self.queries += 1
                if results:
                    self._store(query, name, results)
                    return results[:limit]
                answered = name
            if answered:
                # Every reachable provider answered, none found anything: cache the miss.
                self._store(query, answered, [])
            return []
