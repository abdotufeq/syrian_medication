"""URL utilities: sitemap, pagination, listing links, language detection."""

from __future__ import annotations

import logging
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

import requests
from bs4 import BeautifulSoup

from .http_client import create_session, fetch

from .constants import CARD_SKIP, MAX_LISTING_PAGES, REQUEST_TIMEOUT, TITLE_SKIP


logger = logging.getLogger(__name__)


def language_link(soup: BeautifulSoup, page_url: str) -> str:
    host = urllib.parse.urlparse(page_url).netloc.lower().removeprefix("www.")
    for anchor in soup.select("a[href]"):
        absolute = urllib.parse.urljoin(page_url, anchor.get("href") or "")
        other = urllib.parse.urlparse(absolute).netloc.lower().removeprefix("www.")
        if other != host or absolute == page_url:
            continue
        if "/product" in absolute and ("/en/" in absolute or "/ar/" in absolute):
            return absolute
    return ""


def same_site(url: str, website: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    origin = urllib.parse.urlparse(website).netloc.lower().removeprefix("www.")
    return bool(host) and host == origin


def canonicalize(url: str, website: str) -> str:
    """Repair relative catalog links that urljoin nests under the listing path."""
    match = re.search(r"/products/\d+/\d+/En(?:/|$)", url)
    if match and "elsaad.com" in urllib.parse.urlparse(website).netloc:
        return "https://elsaad.com" + match.group(0).rstrip("/")
    return url.split("#")[0]


def sitemap_locations(session: requests.Session, website: str, extra: list[str]) -> list[str]:
    origin = f"{urllib.parse.urlparse(website).scheme}://{urllib.parse.urlparse(website).netloc}"
    queue = [
        f"{origin}/sitemap.xml",
        f"{origin}/wp-sitemap.xml",
        f"{origin}/sitemap_index.xml",
        f"{origin}/wp-sitemap-posts-product-1.xml",
        f"{origin}/product-sitemap.xml",
        *extra,
    ]
    seen: set[str] = set()
    locations: list[str] = []
    while queue and len(seen) < 12:
        url = queue.pop(0)
        if not url.lower().endswith(".xml") or url in seen:
            continue
        seen.add(url)
        try:
            response = fetch(session, url)
        except requests.RequestException:
            continue
        if "xml" not in response.headers.get("content-type", "") and "<loc>" not in response.text[:500]:
            continue
        for loc in re.findall(r"<loc>\s*([^<]+)\s*</loc>", response.text):
            loc = loc.strip()
            if loc.lower().endswith(".xml") and "product" in loc.lower():
                queue.append(loc)
            else:
                locations.append(loc)
    return locations


def expand_pagination(url: str) -> list[str]:
    """When a catalog link points at page 12, also visit pages 1 through 12."""
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    page_values = query.get("page") or []
    if page_values and page_values[0].isdigit():
        last = int(page_values[0])
        if 1 < last <= 80:
            pages = []
            for number in range(1, last + 1):
                new_query = dict(query)
                new_query["page"] = [str(number)]
                pages.append(urllib.parse.urlunparse(
                    parsed._replace(query=urllib.parse.urlencode(new_query, doseq=True))
                ))
            return pages
    return [url]


def listing_links(html: str, page_url: str, website: str, product_re: re.Pattern[str] | None) -> tuple[list[str], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    products: list[str] = []
    listings: list[str] = []
    for anchor in soup.select("a[href]"):
        absolute = canonicalize(urllib.parse.urljoin(page_url, anchor["href"]), website)
        if not same_site(absolute, website):
            continue
        if re.search(r"\.(?:jpe?g|png|gif|webp|svg|pdf|css|js|zip)(?:$|\?)", absolute, re.I):
            continue
        if product_re and product_re.search(absolute) and "product-category" not in absolute:
            products.append(absolute.split("?")[0] if "product_data.php" not in absolute else absolute)
            continue
        path = urllib.parse.urlparse(absolute).path.lower()
        if "product" in path and "product-category" not in path:
            listings.append(absolute)
    for option in soup.select("option[value]"):
        value = option["value"]
        if not value or value.startswith("#"):
            continue
        absolute = canonicalize(urllib.parse.urljoin(page_url, value), website)
        if same_site(absolute, website) and "product" in absolute.lower():
            listings.append(absolute)
    return products, listings


def gather_product_urls(
    session: requests.Session,
    site: dict,
    max_products: int | None,
    delay: float,
) -> list[str]:
    website = site.get("website") or ""
    product_re = re.compile(site["product_re"]) if site.get("product_re") else None
    found: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        url = canonicalize(url, website)
        if "product-category" in url or "/products/therapeutic" in url:
            return
        if product_re and not product_re.search(url):
            return
        if url in seen:
            return
        seen.add(url)
        found.append(url)

    for location in sitemap_locations(session, website, site.get("seeds") or []):
        add(location)
        if max_products and len(found) >= max_products:
            return found[:max_products]
    if len(found) >= 8 and not site.get("cards"):
        return found[:max_products] if max_products else found

    queue = list(site.get("seeds") or [])
    visited: set[str] = set()
    while queue and len(visited) < MAX_LISTING_PAGES:
        page_url = queue.pop(0)
        if page_url in visited or page_url.lower().endswith(".xml") or "/products/products" in page_url:
            continue
        visited.add(page_url)
        try:
            response = fetch(session, page_url)
        except requests.RequestException as error:
            logger.warning("  listing failed %s: %s", page_url, error)
            continue
        products, listings = listing_links(response.text, response.url, website, product_re)
        for product_url in products:
            add(product_url)
        for listing in listings:
            for page_url_to_visit in expand_pagination(listing):
                if page_url_to_visit not in visited and page_url_to_visit not in queue:
                    queue.append(page_url_to_visit)
        if max_products and len(found) >= max_products:
            break

    scan = site.get("id_scan")
    if scan and (not max_products or len(found) < max_products):
        identifiers = range(int(scan["start"]), int(scan["end"]) + 1)

        def probe(identifier: int) -> str:
            url = scan["url"].format(id=identifier)
            if url in seen:
                return ""
            probe_session = create_session()
            try:
                response = fetch(probe_session, url)
            except requests.RequestException:
                return ""
            if response.status_code >= 400 or len(response.content) < int(scan.get("min_bytes", 8000)):
                return ""
            return response.url

        with ThreadPoolExecutor(max_workers=8) as pool:
            for result in pool.map(probe, identifiers):
                if result:
                    add(result)
        logger.info("  id scan found %d", len(found))
    if max_products:
        return found[:max_products]
    return found