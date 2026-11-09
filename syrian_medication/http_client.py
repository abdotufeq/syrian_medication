"""HTTP client with retry support, caching, and SSL fallback."""

from __future__ import annotations

import logging
import warnings

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InsecureRequestWarning
from urllib3.util.retry import Retry

from .constants import REQUEST_TIMEOUT


logger = logging.getLogger(__name__)

# Module-level response cache to avoid re-fetching during a single run
_response_cache: dict[str, requests.Response] = {}


def create_session() -> requests.Session:
    """Create a requests session with retry support and better headers."""
    retry = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=False,  # some sites send Retry-After values that stall the crawl for hours
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    })
    return session


# Module-level response cache to avoid re-fetching during a single run
_response_cache: dict[str, requests.Response] = {}


def fetch(session: requests.Session, url: str, use_cache: bool = True, verify_ssl: bool = True) -> requests.Response:
    """Fetch a URL with optional caching and SSL fallback.
    
    Args:
        session: Requests session to use
        url: URL to fetch
        use_cache: Whether to use the in-memory response cache
        verify_ssl: If True, tries SSL verification first, then falls back to verify=False
    """
    if use_cache and url in _response_cache:
        return _response_cache[url]
    
    # Try with SSL verification first if requested
    ssl_verify = verify_ssl
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, verify=ssl_verify)
        response.raise_for_status()
    except requests.exceptions.SSLError:
        # Fallback to no SSL verification
        if verify_ssl:
            warnings.filterwarnings("ignore", category=InsecureRequestWarning)
            logger.debug("SSL error for %s, retrying without verification", url)
            response = session.get(url, timeout=REQUEST_TIMEOUT, verify=False)
            response.raise_for_status()
        else:
            raise
    except requests.exceptions.RequestException:
        raise
    
    if use_cache:
        _response_cache[url] = response
    return response


def clear_response_cache() -> None:
    """Clear the in-memory response cache."""
    _response_cache.clear()