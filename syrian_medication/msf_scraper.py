"""MSF Unicat catalog scraper -> standalone database (data/msf_unicat.sqlite).

https://unicat.msf.org is an Odoo-based catalog of everything MSF procures.
Only the pharmacy-relevant sections are scraped by default:

    D - Drugs                          (oral, injectable, infusions, vaccines, external ...)
    E - Medical equipment              (small equipment, dispensing, lab, sterilisation ...)
    S - Renewable medical supplies     (consumables: injection, dressings, catheters, lab reagents ...)
    K - Kits (medical families only)   (KMED medical kits, KSUR/KSUD/KSUI surgical instrument sets)
    N - Nutrition (specialised food)   (NFOS / NSFS therapeutic foods)

Notes about the site:
  * Listing pages accept ``?ppg=N`` (products per page). Requesting a page
    beyond the end returns the last item again, so pagination stops when a
    page adds no new article codes.
  * Public prices are a hidden 1.00 EUR placeholder, so no price is stored.
  * The first four characters of an article code are its family (DORA, SINS ...).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

from .constants import MSF_CSV_PATH, MSF_DB_PATH, MSF_PHOTO_DIR, ROOT
from .formatter import ProgressBar, bold, cyan, dim, fmt, green, red, section
from .http_client import create_session, fetch
from .msf_database import connect_msf
from .text_utils import slugify

logger = logging.getLogger(__name__)

MSF_BASE = "https://unicat.msf.org"
MSF_CATALOG = f"{MSF_BASE}/cat"
MSF_DRUGS = f"{MSF_BASE}/cat/category/d-drugs-20510"
MSF_PAGE_SIZE = 100

# Pharmacy-relevant scope. A whole section is taken when its value is None;
# otherwise only the listed 4-letter families inside it are kept.
PHARMACY_SCOPE: dict[str, set[str] | None] = {
    "D": None,
    "E": None,
    "S": None,
    "K": {"KMED", "KSUR", "KSUD", "KSUI"},
    "N": {"NFOS", "NSFS"},
}

SECTION_NAMES = {
    "A": "Administration / Office",
    "C": "Camps & Construction",
    "D": "Drugs",
    "E": "Medical equipment",
    "K": "Kits",
    "L": "Library",
    "N": "Nutrition",
    "P": "Programme support",
    "S": "Renewable medical supplies",
    "T": "Transport",
    "X": "Services",
    "Y": "Mechanical spare parts",
}

# Badge attributes on the detail page are identified by their description text.
ATTRIBUTE_KEYS = (
    ("classification of the medicines", "therapeutic_group"),
    ("anatomical therapeutic chemical", "atc_code"),
    ("needs to be justified", "restriction_code"),
    ("thermosensitive", "thermosensitive_code"),
    ("medical device nomenclature", "emdn_code"),
    ("classified into 4 classes", "device_class"),
    ("supplied (and stored) sterile", "sterile"),
    ("single-use device", "single_use"),
    ("ce marking", "ce_marked"),
    ("part of at least one kit", "part_of_kit"),
)
FLAG_KEYS = {"sterile", "single_use", "ce_marked", "part_of_kit"}

SHEET_KEYS = {
    "therapeutic action": "therapeutic_action",
    "indications": "indications",
    "instructions for use": "instructions",
    "precautions for use": "precautions",
    "precautions": "precautions",
    "storage": "storage",
    "definition": "definition",
    "specifications": "specifications",
    "technical specifications": "specifications",
    "packaging & labelling": "packaging",
    "packaging": "packaging",
    "quality standards": "quality_standards",
    "msf requirements": "msf_requirements",
}

ITEM_COLUMNS = (
    "code", "name", "section", "section_name", "family_code", "family_name",
    "standardization", "article_status", "account_code", "hs_code", "former_codes",
    "last_updated", "therapeutic_group", "atc_code", "who_eml", "restriction_code",
    "thermosensitive_code", "emdn_code", "device_class", "sterile", "single_use",
    "ce_marked", "part_of_kit", "therapeutic_action", "indications", "instructions",
    "precautions", "storage", "definition", "specifications", "packaging",
    "quality_standards", "msf_requirements", "notes", "sheets_json",
    "attributes_json", "image_url", "image_local", "url", "scraped_at",
)

_local = threading.local()


def _session() -> requests.Session:
    """One requests session per worker thread."""
    if not hasattr(_local, "session"):
        _local.session = create_session()
    return _local.session


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def _absolute(href: str) -> str:
    return urllib.parse.urljoin(MSF_BASE, href or "")


def _product_url(href: str) -> str:
    """Canonical product URL without the ?category= listing context."""
    return _absolute(href).split("?")[0]


# ── Category discovery ───────────────────────────────────────────────────────

def discover_categories(session: requests.Session) -> list[tuple[str, str]]:
    """Return every (name, absolute_url) category link on the catalog index."""
    try:
        response = fetch(session, MSF_CATALOG, use_cache=False)
    except requests.RequestException as error:
        logger.error("Cannot reach MSF catalog: %s", error)
        return []
    soup = BeautifulSoup(response.text, "html.parser")
    categories: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select('a[href*="/cat/category/"]'):
        url = _absolute(anchor["href"])
        name = _text(anchor)
        if not name or url in seen:
            continue
        seen.add(url)
        categories.append((name, url))
    return categories


def _category_code(name: str) -> str:
    """'DORA - Oral drugs' -> 'DORA', 'D - Drugs' -> 'D'."""
    match = re.match(r"\s*([A-Z]{1,4})\s*-", name)
    return match.group(1) if match else ""


def store_categories(connection: sqlite3.Connection, categories: list[tuple[str, str]]) -> dict[str, str]:
    """Persist categories and return a family_code -> name map."""
    names: dict[str, str] = {}
    for name, url in categories:
        code = _category_code(name)
        if not code:
            continue
        names[code] = name
        connection.execute(
            """
            INSERT INTO categories (code, section, name, url) VALUES (?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET name = excluded.name, url = excluded.url
            """,
            (code, code[0], name, url),
        )
    connection.commit()
    return names


def section_urls(categories: list[tuple[str, str]], sections: list[str]) -> list[tuple[str, str]]:
    """Top-level section listing URLs (e.g. 'D - Drugs') for the wanted letters."""
    wanted = {letter.upper() for letter in sections}
    chosen = []
    for name, url in categories:
        code = _category_code(name)
        if len(code) == 1 and code in wanted:
            chosen.append((name, url))
    return chosen


# ── Listing pages ────────────────────────────────────────────────────────────

def parse_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for card in soup.select("form.oe_product_cart"):
        code_el = card.select_one(".o_wsale_product_information_text span.valid_article")
        title = card.select_one(".o_wsale_products_item_title a")
        if not code_el or not title:
            continue
        code = _text(code_el)
        image = card.select_one(".oe_product_image img")
        items.append({
            "code": code,
            "name": title.get("content") or _text(title),
            "url": _product_url(title.get("href", "")),
            "thumbnail": _absolute(image.get("src", "")) if image else "",
        })
    return items


def list_section(
    session: requests.Session,
    url: str,
    delay: float,
    max_items: int = 0,
    keep=lambda code: True,
) -> list[dict]:
    """Walk every listing page of a section until a page adds no new codes.

    ``keep(code)`` filters articles; ``max_items`` caps the kept articles.
    """
    found: dict[str, dict] = {}
    seen: set[str] = set()
    page = 1
    while page < 400:
        page_url = f"{url}/page/{page}?ppg={MSF_PAGE_SIZE}" if page > 1 else f"{url}?ppg={MSF_PAGE_SIZE}"
        try:
            response = fetch(session, page_url, use_cache=False)
        except requests.RequestException as error:
            logger.warning("MSF listing failed %s: %s", page_url, error)
            break
        new = 0
        for item in parse_listing(response.text):
            if item["code"] in seen:
                continue
            seen.add(item["code"])
            new += 1
            if keep(item["code"]):
                found[item["code"]] = item
        print(f"\r    {dim(f'listing page {page}: {fmt(len(seen))} articles, {fmt(len(found))} in scope')}", end="", flush=True)
        if new == 0 or (max_items and len(found) >= max_items):
            break
        page += 1
        time.sleep(delay)
    print()
    items = list(found.values())
    return items[:max_items] if max_items else items


def in_scope(code: str, scope: dict[str, set[str] | None]) -> bool:
    if not code:
        return False
    families = scope.get(code[0])
    if code[0] not in scope:
        return False
    return families is None or code[:4] in families


# ── Detail pages ─────────────────────────────────────────────────────────────

def parse_detail(html: str, url: str) -> dict:
    """Parse one MSF product page into an ``items`` row plus child rows."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    item: dict = {key: "" for key in ITEM_COLUMNS}
    for flag in FLAG_KEYS | {"who_eml"}:
        item[flag] = 0
    item["url"] = url

    name_el = soup.select_one("h1[itemprop=name]") or soup.select_one("[itemprop=name]")
    item["name"] = _text(name_el)

    header = soup.select_one("#product_details div.valid_article") or soup.select_one("div.valid_article")
    if header:
        spans = header.select(":scope > div:first-child > span")
        if spans:
            item["code"] = _text(spans[-1])
            if len(spans) > 1:
                item["standardization"] = _text(spans[0])
        item["article_status"] = _text(header.select_one(".status_content"))
        for row in header.select(".account_code > div"):
            parts = [_text(span) for span in row.find_all("span")]
            if len(parts) < 2:
                continue
            label = parts[0].lower()
            if label.startswith("account code"):
                item["account_code"] = parts[1]
            elif label.startswith("hs code"):
                item["hs_code"] = parts[1]
            elif label.startswith("last updated"):
                item["last_updated"] = parts[1]

    former_label = soup.find(string=re.compile(r"Former\s+Code"))
    if former_label is not None and former_label.parent is not None and former_label.parent.parent is not None:
        container = former_label.parent.parent
        text = container.get_text(" ", strip=True)
        text = re.sub(r"^Former\s+Code\(s\)\s*:\s*", "", text)
        item["former_codes"] = re.sub(r"^-X\b\s*", "", text).strip()[:300]

    attributes: dict[str, str] = {}
    for block in soup.select(".inline-attribute"):
        description = _text(block.select_one(".description_text"))
        cols = block.select(".col")
        value = _text(cols[1]) if len(cols) > 1 else ""
        attributes[description[:120] or f"attribute_{len(attributes)}"] = value
        lowered = description.lower()
        for needle, key in ATTRIBUTE_KEYS:
            if needle in lowered:
                if key in FLAG_KEYS:
                    item[key] = 1
                else:
                    item[key] = value
                break
    item["attributes_json"] = json.dumps(attributes, ensure_ascii=False)

    sheets: dict[str, str] = {}
    notes: list[str] = []
    for sheet in soup.select(".tech-sheets"):
        title = _text(sheet.select_one(".tech-sheet-titles"))
        body = _text(sheet.select_one(".technical-sheet-description")) or _text(sheet)
        if not body:
            continue
        if "essential medicines" in body.lower():
            item["who_eml"] = 1
        if not title:
            notes.append(body)
            continue
        body = body.removeprefix(title).strip()
        sheets[title] = f"{sheets[title]}\n{body}" if title in sheets else body
        column = SHEET_KEYS.get(title.lower())
        if column and not item[column]:
            item[column] = body
    item["sheets_json"] = json.dumps(sheets, ensure_ascii=False)
    item["notes"] = "\n".join(notes)[:4000]

    images: list[str] = []
    main_image = soup.select_one("span[itemprop=image]")
    if main_image and _text(main_image).startswith("http"):
        images.append(_text(main_image))
    for img in soup.select("img[src*='/web/image/product.image/']"):
        src = re.sub(r"/image_\d+/", "/image_1024/", _absolute(img["src"]))
        if src not in images:
            images.append(src)
    item["image_url"] = images[0] if images else ""

    kit_components = []
    for table in soup.select("table"):
        for tr in table.select("tr"):
            cells = tr.find_all("td")
            if len(cells) < 6 or not cells[0].select_one("img"):
                continue
            values = [_text(cell) for cell in cells[1:6]]
            link = cells[1].select_one("a")
            kit_components.append({
                "component_code": values[0],
                "standardization": values[1],
                "label": values[2],
                "quantity": values[3],
                "requirement": values[4],
                "url": _product_url(link["href"]) if link else "",
            })

    related = []
    tabs = {
        tab.get("href", "").lstrip("#"): _text(tab)
        for tab in soup.select("ul.nav-pills a.nav-link[href^='#link-']")
    }
    for pane_id, relation in tabs.items():
        pane = soup.find(id=pane_id)
        if not pane:
            continue
        for card in pane.select(".oe_product_cart"):
            code_el = card.select_one("span[style*='grey']")
            name_el = card.select_one("span[title]")
            if code_el:
                related.append({
                    "related_code": _text(code_el),
                    "relation": relation,
                    "related_name": name_el.get("title", "") if name_el else "",
                })

    breadcrumb = [_text(li) for li in soup.select(".breadcrumb li")]
    return {
        "item": item,
        "images": images,
        "kit_components": kit_components,
        "related": related,
        "breadcrumb": breadcrumb,
    }


def download_image(url: str, code: str) -> str:
    """Save an MSF product image under data/photos/msf-unicat/ and return the relative path."""
    filename = slugify(code) or "item"
    MSF_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    for extension in (".jpg", ".png", ".webp", ".gif"):
        existing = MSF_PHOTO_DIR / f"{filename}{extension}"
        if existing.exists() and existing.stat().st_size > 1000:
            return str(existing.relative_to(ROOT))
    try:
        response = _session().get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException:
        return ""
    content_type = response.headers.get("content-type", "")
    if "image" not in content_type or len(response.content) < 1000:
        return ""
    extension = ".png" if "png" in content_type else ".webp" if "webp" in content_type else ".gif" if "gif" in content_type else ".jpg"
    path = MSF_PHOTO_DIR / f"{filename}{extension}"
    path.write_bytes(response.content)
    return str(path.relative_to(ROOT))


def fetch_detail(listing: dict, download_photos: bool, delay: float) -> dict | None:
    time.sleep(delay)
    try:
        response = fetch(_session(), listing["url"], use_cache=False)
    except requests.RequestException as error:
        logger.debug("MSF detail failed %s: %s", listing["url"], error)
        return None
    parsed = parse_detail(response.text, listing["url"])
    item = parsed["item"]
    item["code"] = item["code"] or listing["code"]
    item["name"] = item["name"] or listing["name"]
    if not item["image_url"] and listing.get("thumbnail"):
        item["image_url"] = listing["thumbnail"]
        parsed["images"] = [listing["thumbnail"]]
    if download_photos and item["image_url"]:
        item["image_local"] = download_image(item["image_url"], item["code"])
    return parsed


def save_item(connection: sqlite3.Connection, parsed: dict, family_names: dict[str, str]) -> None:
    item = parsed["item"]
    code = item["code"]
    item["section"] = code[:1]
    item["section_name"] = SECTION_NAMES.get(code[:1], "")
    item["family_code"] = code[:4]
    item["family_name"] = family_names.get(code[:4], "")
    if not item["family_name"]:
        crumbs = [crumb for crumb in parsed.get("breadcrumb", []) if crumb.startswith(code[:4])]
        item["family_name"] = crumbs[0] if crumbs else ""
    item["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    placeholders = ", ".join("?" for _ in ITEM_COLUMNS)
    updates = ", ".join(f"{column} = excluded.{column}" for column in ITEM_COLUMNS if column != "code")
    connection.execute(
        f"INSERT INTO items ({', '.join(ITEM_COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(code) DO UPDATE SET {updates}",
        [item[column] for column in ITEM_COLUMNS],
    )
    connection.execute("DELETE FROM item_images WHERE item_code = ?", (code,))
    for index, url in enumerate(parsed["images"]):
        connection.execute(
            "INSERT OR IGNORE INTO item_images (item_code, url, local_path) VALUES (?, ?, ?)",
            (code, url, item["image_local"] if index == 0 else ""),
        )
    connection.execute("DELETE FROM kit_components WHERE kit_code = ?", (code,))
    for component in parsed["kit_components"]:
        connection.execute(
            """
            INSERT OR IGNORE INTO kit_components
                (kit_code, component_code, standardization, label, quantity, requirement, url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (code, component["component_code"], component["standardization"], component["label"],
             component["quantity"], component["requirement"], component["url"]),
        )
    connection.execute("DELETE FROM related_items WHERE item_code = ?", (code,))
    for related in parsed["related"]:
        connection.execute(
            "INSERT OR IGNORE INTO related_items (item_code, related_code, relation, related_name) VALUES (?, ?, ?, ?)",
            (code, related["related_code"], related["relation"], related["related_name"]),
        )


# ── Orchestration ────────────────────────────────────────────────────────────

def scrape_msf_catalog(
    session: requests.Session,
    sections: list[str] | None = None,
    all_families: bool = False,
    max_items: int = 0,
    workers: int = 4,
    delay: float = 0.2,
    download_photos: bool = True,
    refresh: bool = False,
) -> dict[str, int]:
    """Scrape the pharmacy-relevant MSF Unicat sections into data/msf_unicat.sqlite.

    Args:
        sections: section letters to scrape (default: D, E, S, K, N).
        all_families: keep every family of the chosen sections, not only the
            pharmacy-relevant ones (e.g. all kits, not just medical kits).
        max_items: cap articles per section (0 = all), for testing.
        refresh: re-fetch detail pages already in the database.
    """
    connection = connect_msf()
    sections = [letter.upper() for letter in (sections or list(PHARMACY_SCOPE))]
    scope: dict[str, set[str] | None] = {
        letter: None if all_families else PHARMACY_SCOPE.get(letter) for letter in sections
    }

    section(f"MSF Unicat → {MSF_DB_PATH.name}")
    categories = discover_categories(session)
    if not categories:
        print(f"    {red('✗')}  Cannot reach {MSF_CATALOG}")
        return {}
    family_names = store_categories(connection, categories)

    totals: dict[str, int] = {}
    for index, (name, url) in enumerate(section_urls(categories, sections), start=1):
        letter = _category_code(name)
        print(f"  {cyan(f'[{index}/{len(sections)}]')}  {bold(name)}  {dim(url)}")
        listing = list_section(session, url, delay, max_items, keep=lambda code: in_scope(code, scope))
        known = {row["code"] for row in connection.execute("SELECT code FROM items WHERE section = ?", (letter,))}
        pending = listing if refresh else [item for item in listing if item["code"] not in known]
        print(f"    {dim(f'{fmt(len(listing))} pharmacy articles, {fmt(len(pending))} to fetch')}", flush=True)

        saved = failed = 0
        if pending:
            bar = ProgressBar(total=len(pending), label="Details")
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = [pool.submit(fetch_detail, item, download_photos, delay) for item in pending]
                for done, future in enumerate(as_completed(futures), start=1):
                    parsed = future.result()
                    if parsed and parsed["item"]["code"]:
                        save_item(connection, parsed, family_names)
                        saved += 1
                    else:
                        failed += 1
                    if done % 25 == 0:
                        connection.commit()
                    bar.update(done, extra=f"{fmt(saved)} saved")
            connection.commit()
            bar.finish(message=f"{fmt(saved)} saved" + (f", {fmt(failed)} failed" if failed else ""))
        totals[letter] = connection.execute(
            "SELECT COUNT(*) AS n FROM items WHERE section = ?", (letter,)
        ).fetchone()["n"]

    export_msf_csv(connection)
    connection.close()
    return totals


def export_msf_csv(connection: sqlite3.Connection, path: Path = MSF_CSV_PATH) -> int:
    frame = pd.read_sql_query(
        """
        SELECT i.*,
               (SELECT COUNT(*) FROM kit_components k WHERE k.kit_code = i.code) AS kit_component_count
        FROM items i ORDER BY i.code
        """,
        connection,
    ).drop(columns=["id", "sheets_json", "attributes_json"])
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"  {green('✓')}  MSF CSV exported: {bold(fmt(len(frame)))} articles → {dim(str(path))}")
    return len(frame)


# Backwards-compatible names used by older scripts.
def scrape_msf(connection, session, category_url: str = MSF_DRUGS, max_pages: int = 0,
               max_products: int = 0, workers: int = 4, delay: float = 0.2,
               download_photos: bool = False) -> int:
    """Deprecated: scrapes the Drugs section into the standalone MSF database."""
    totals = scrape_msf_catalog(session, ["D"], max_items=max_products, workers=workers,
                                delay=delay, download_photos=download_photos)
    return sum(totals.values())


def scrape_all_msf(connection, session, category_names=None, max_pages: int = 0,
                   max_products: int = 0, workers: int = 4, delay: float = 0.2,
                   download_photos: bool = False) -> int:
    """Deprecated: scrapes every pharmacy section into the standalone MSF database."""
    letters = [_category_code(name) or name[:1] for name in category_names] if category_names else None
    totals = scrape_msf_catalog(session, letters, max_items=max_products, workers=workers,
                                delay=delay, download_photos=download_photos)
    return sum(totals.values())
