"""Manufacturer site scraping orchestration."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .constants import ProductRecord
from .database import upsert_manufacturer
from .http_client import create_session, fetch
from .product_parser import (
    attach_photos, known_product_urls, save_product,
)
from .text_utils import ascii_url, english_text, name_score, strength_agrees
from .url_utils import gather_product_urls


logger = logging.getLogger(__name__)


def language_sibling(url: str) -> str:
    for english_marker, arabic_marker in (
        ("/en/", "/ar/"),
        ("/ar/", "/en/"),
        ("LNG=En", "LNG=Ar"),
        ("LNG=Ar", "LNG=En"),
        ("/En/", "/Ar/"),
        ("/Ar/", "/En/"),
        ("lan=ZW4%3D", "lan=YXI%3D"),
        ("lan=ZW4=", "lan=YXI="),
        ("lan=YXI=", "lan=ZW4="),
        ("lan=YXI%3D", "lan=ZW4%3D"),
    ):
        if english_marker in url:
            return url.replace(english_marker, arabic_marker, 1)
    return ""


def merge_language(target: dict, other: dict) -> None:
    for key in (
        "trade_name", "trade_name_ar", "composition", "composition_ar",
        "strength", "strength_ar", "dosage_form", "dosage_form_ar",
        "pack", "pack_ar", "category", "category_ar", "uses", "uses_ar",
        "description", "description_ar", "price", "price_currency",
    ):
        if not target.get(key) and other.get(key):
            target[key] = other[key]


def load_product(
    session: requests.Session,
    url: str,
    manufacturer: str,
    download: bool,
    photo_cache: dict[str, str],
) -> dict | None:
    """Fetch and parse a single product page, reusing the caller's session."""
    try:
        response = fetch(session, url)
        product = parse_product_page(response.url, response.text)
    except (requests.RequestException, ValueError) as exc:
        logger.debug("Failed to load %s: %s", url, exc)
        return None
    if not product.get("trade_name_ar") and not product.get("composition_ar"):
        sibling = product.get("language_url") or language_sibling(url)
        if sibling:
            try:
                other_response = fetch(session, sibling)
                merge_language(product, parse_product_page(other_response.url, other_response.text))
            except (requests.RequestException, ValueError):
                pass
    product["saved_photos"] = []
    if download:
        for photo_url in (product.get("photos") or [])[:4]:
            local_path = download_photo(session, manufacturer, photo_url, photo_cache)
            if local_path:
                product["saved_photos"].append((photo_url, local_path))
    return product


def store_parsed_product(
    connection: sqlite3.Connection,
    manufacturer_id: int,
    product: dict,
    download: bool,
) -> bool:
    if not any(product.get(key) for key in ("trade_name", "trade_name_ar", "composition", "composition_ar")):
        return False
    product_id = save_product(connection, manufacturer_id, product)
    if product_id is None:
        return False
    for photo_url, local_path in product.get("saved_photos") or []:
        connection.execute(
            """
            INSERT OR IGNORE INTO product_photos
                (manufacturer_product_id, source_url, local_path)
            VALUES (?, ?, ?)
            """,
            (product_id, photo_url, local_path),
        )
    if download and not product.get("saved_photos"):
        return True
    return True


def extract_catalog_cards(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    products = []
    seen: set[str] = set()
    for tag in soup.find_all(["h3", "h4"]):
        if tag.find_parent(["nav", "header", "footer"]):
            continue
        raw = re.sub(r"\s+", " ", tag.get_text(" ", strip=True))
        english, arabic = split_languages(raw)
        title = english or arabic
        if not title or title.lower() in CARD_SKIP or len(title) > 90 or len(title.split()) > 8:
            continue
        composition = ""
        match = re.search(r"\(([^)]{2,80})\)", raw)
        if not match:
            following = tag.find_next(["h3", "h4", "p"])
            if following is not None:
                match = re.search(r"\(([^)]{2,80})\)", following.get_text(" ", strip=True))
        if match:
            composition = match.group(1).strip()
            title = re.sub(r"\([^)]*\)", "", title).strip(" -")
        if title.lower() in seen:
            continue
        seen.add(title.lower())
        title_en, title_ar = split_languages(title)
        composition_en, composition_ar = split_languages(composition)
        slug = slugify(title)
        if slug == "unknown":
            slug = hashlib.sha1(raw.encode()).hexdigest()[:12]
        products.append({
            "trade_name": title_en,
            "trade_name_ar": title_ar,
            "composition": composition_en,
            "composition_ar": composition_ar,
            "strength": "",
            "strength_ar": "",
            "dosage_form": "",
            "dosage_form_ar": "",
            "pack": "",
            "pack_ar": "",
            "category": "",
            "category_ar": "",
            "uses": "",
            "uses_ar": "",
            "price": "",
            "price_currency": "",
            "description": "",
            "description_ar": "",
            "extra": {},
            "source_url": f"{page_url}#{slug[:70]}",
            "photos": [],
            "saved_photos": [],
        })
    return products


def collect_cards(session: requests.Session, site: dict, max_products: int | None) -> list[dict]:
    card_re = re.compile(site["card_re"]) if site.get("card_re") else None
    pages = list(site.get("seeds") or [])
    seen_pages = set(pages)
    products: list[dict] = []
    index = 0
    while index < len(pages) and index < MAX_LISTING_PAGES:
        page_url = pages[index]
        index += 1
        try:
            response = fetch(session, page_url)
        except requests.RequestException as error:
            logger.warning("  listing failed %s: %s", page_url, error)
            continue
        if card_re is None or card_re.search(response.url):
            products.extend(extract_catalog_cards(response.text, response.url))
        if card_re:
            for anchor in BeautifulSoup(response.text, "html.parser").select("a[href]"):
                absolute = urllib.parse.urljoin(response.url, anchor["href"])
                if card_re.search(absolute) and absolute not in seen_pages and same_site(absolute, site["website"]):
                    seen_pages.add(absolute)
                    pages.append(absolute)
        if max_products and len(products) >= max_products:
            break
    merged: list[dict] = []
    by_key: dict[str, dict] = {}
    for product in products:
        key = slugify(product.get("trade_name") or product.get("trade_name_ar") or "")
        if not key or key == "unknown":
            key = product["source_url"]
        existing = by_key.get(key)
        if existing:
            merge_language(existing, product)
            continue
        by_key[key] = product
        merged.append(product)
    if max_products:
        return merged[:max_products]
    return merged


def scrape_site(
    connection: sqlite3.Connection,
    session: requests.Session,
    site: dict,
    max_products: int | None,
    delay: float,
    download: bool,
    workers: int,
) -> int:
    manufacturer_id = upsert_manufacturer(connection, site)
    parser = site.get("parser") or ""
    if not parser:
        connection.commit()
        return 0

    logger.info("\n%s", site["english_name"])
    if max_products is None:
        connection.execute(
            """
            DELETE FROM product_photos
            WHERE manufacturer_product_id IN (
                SELECT id FROM manufacturer_products
                WHERE manufacturer_id = ? AND trade_name_ar IS NULL
            )
            """,
            (manufacturer_id,),
        )
        connection.execute(
            "DELETE FROM manufacturer_products WHERE manufacturer_id = ? AND trade_name_ar IS NULL",
            (manufacturer_id,),
        )
    photo_cache: dict[str, str] = {}
    saved = 0
    status = "ok"
    note = ""
    try:
        if parser == "alpha":
            response = fetch(session, site["seeds"][0])
            products = parse_alpha(response.text)
            try:
                arabic_page = fetch(session, "https://alpha-syria.com/ar/products.html")
                arabic_products = parse_alpha(arabic_page.text)
                by_key = {
                    (item["extra"].get("alpha_id"), item["extra"].get("variant")): item
                    for item in arabic_products
                }
                for product in products:
                    extra = product.get("extra") or {}
                    other = by_key.get((extra.get("alpha_id"), extra.get("variant")))
                    if other:
                        merge_language(product, other)
            except (requests.RequestException, ValueError) as error:
                logger.warning("  arabic catalog skipped: %s", error)
            if max_products:
                products = products[:max_products]
        elif site.get("cards"):
            products = collect_cards(session, site, max_products)
            logger.info("  %d catalog cards", len(products))
        else:
            urls = gather_product_urls(session, site, max_products, delay)
            already = {
                row["source_url"]
                for row in connection.execute(
                    """
                    SELECT source_url FROM manufacturer_products
                    WHERE manufacturer_id = ? AND trade_name_ar IS NOT NULL
                    """,
                    (manufacturer_id,),
                )
            }
            pending = [url for url in urls if url not in already]
            logger.info("  %d product pages, %d to fetch", len(urls), len(pending))
            products = []
            # For threaded workers, each thread gets its own session to avoid
            # requests.Session thread-safety issues, but we share the response cache.
            local_cache: dict[str, str] = {}

            def _fetch_product(url: str) -> dict | None:
                worker_session = create_session()
                return load_product(worker_session, url, site["english_name"], download, local_cache)

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(_fetch_product, url)
                    for url in pending
                ]
                for index, future in enumerate(as_completed(futures), start=1):
                    product = future.result()
                    if product and store_parsed_product(connection, manufacturer_id, product, download=False):
                        saved += 1
                    if index % 40 == 0:
                        connection.commit()
                        logger.info("  fetched %d/%d", index, len(pending))
            products = []

        for product in products:
            if not any(product.get(key) for key in ("trade_name", "trade_name_ar", "composition", "composition_ar")):
                continue
            product_id = save_product(connection, manufacturer_id, product)
            if product_id is None:
                continue
            attach_photos(
                connection,
                session,
                product_id,
                site["english_name"],
                product.get("photos") or [],
                photo_cache,
                download,
            )
            saved += 1
    except (requests.RequestException, ValueError) as error:
        status = "error"
        note = str(error)
        logger.error("  failed: %s", error)

    total = connection.execute(
        "SELECT COUNT(*) AS n FROM manufacturer_products WHERE manufacturer_id = ?",
        (manufacturer_id,),
    ).fetchone()["n"]
    connection.execute(
        "UPDATE manufacturers SET status = ?, product_count = ?, note = ? WHERE id = ?",
        (status, total, note, manufacturer_id),
    )
    connection.commit()
    logger.info("  saved %d new rows (%d total)", saved, total)
    return saved