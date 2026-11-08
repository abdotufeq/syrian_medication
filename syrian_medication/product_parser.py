"""Product page parsing, photo downloads, and database saving."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .constants import (
    ALPHA_CATEGORIES, CARD_SKIP, IMAGE_SKIP_RE, LABELS, PHOTO_DIR, PRICE_RE,
    REQUEST_TIMEOUT, TITLE_SKIP,
)
from .http_client import fetch
from .text_utils import ascii_url, english_text, slugify, split_languages
from .url_utils import language_link


logger = logging.getLogger(__name__)


def known_product_urls(connection: sqlite3.Connection, manufacturer_id: int) -> set[str]:
    rows = connection.execute(
        "SELECT source_url FROM manufacturer_products WHERE manufacturer_id = ?",
        (manufacturer_id,),
    )
    return {row["source_url"] for row in rows}


def save_product(
    connection: sqlite3.Connection,
    manufacturer_id: int,
    product: dict,
) -> int | None:
    cursor = connection.execute(
        """
        INSERT INTO manufacturer_products (
            manufacturer_id, trade_name, trade_name_ar, composition, composition_ar,
            strength, strength_ar, dosage_form, dosage_form_ar, pack, pack_ar,
            category, category_ar, uses, uses_ar, price, price_currency,
            description, description_ar, extra_json, source_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(manufacturer_id, source_url, trade_name, composition, strength, pack)
        DO UPDATE SET
            trade_name_ar = excluded.trade_name_ar,
            composition_ar = excluded.composition_ar,
            strength_ar = excluded.strength_ar,
            dosage_form_ar = excluded.dosage_form_ar,
            pack_ar = excluded.pack_ar,
            category_ar = excluded.category_ar,
            uses_ar = excluded.uses_ar,
            description_ar = excluded.description_ar
        """,
        (
            manufacturer_id,
            product.get("trade_name") or "",
            product.get("trade_name_ar") or "",
            product.get("composition") or "",
            product.get("composition_ar") or "",
            product.get("strength") or "",
            product.get("strength_ar") or "",
            product.get("dosage_form") or "",
            product.get("dosage_form_ar") or "",
            product.get("pack") or "",
            product.get("pack_ar") or "",
            product.get("category") or "",
            product.get("category_ar") or "",
            product.get("uses") or "",
            product.get("uses_ar") or "",
            product.get("price") or "",
            product.get("price_currency") or "",
            product.get("description") or "",
            product.get("description_ar") or "",
            json.dumps(product.get("extra") or {}, ensure_ascii=True),
            product.get("source_url") or "",
        ),
    )
    if cursor.lastrowid and cursor.rowcount:
        return int(cursor.lastrowid)
    row = connection.execute(
        """
        SELECT id FROM manufacturer_products
        WHERE manufacturer_id = ? AND source_url = ? AND trade_name = ?
          AND composition = ? AND strength = ? AND pack = ?
        """,
        (
            manufacturer_id,
            product.get("source_url") or "",
            product.get("trade_name") or "",
            product.get("composition") or "",
            product.get("strength") or "",
            product.get("pack") or "",
        ),
    ).fetchone()
    return int(row["id"]) if row else None


def download_photo(
    session: requests.Session,
    manufacturer: str,
    source_url: str,
    cache: dict[str, str],
) -> str:
    if source_url in cache:
        return cache[source_url]
    if IMAGE_SKIP_RE.search(source_url):
        return ""
    folder = PHOTO_DIR / slugify(manufacturer)
    filename = slugify(Path(urllib.parse.urlparse(source_url).path).stem)[:80] or "photo"
    for existing_extension in (".webp", ".jpg", ".jpeg", ".png", ".gif"):
        existing = folder / f"{filename}{existing_extension}"
        if existing.exists() and existing.stat().st_size > 1500:
            relative = str(existing.relative_to(ROOT))
            cache[source_url] = relative
            return relative
    try:
        response = fetch(session, source_url)
    except requests.RequestException:
        return ""
    content_type = response.headers.get("content-type", "")
    if "image" not in content_type and not re.search(r"\.(webp|jpe?g|png|gif)(\?|$)", source_url, re.I):
        return ""
    if len(response.content) < 1500:
        return ""
    extension = ".jpg"
    if "png" in content_type or source_url.lower().endswith(".png"):
        extension = ".png"
    elif "webp" in content_type or source_url.lower().endswith(".webp"):
        extension = ".webp"
    elif "gif" in content_type:
        extension = ".gif"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{filename}{extension}"
    counter = 2
    while path.exists() and path.read_bytes() != response.content:
        path = folder / f"{filename}-{counter}{extension}"
        counter += 1
    if not path.exists():
        path.write_bytes(response.content)
    relative = str(path.relative_to(ROOT))
    cache[source_url] = relative
    return relative


def attach_photos(
    connection: sqlite3.Connection,
    session: requests.Session,
    product_id: int,
    manufacturer: str,
    photo_urls: list[str],
    cache: dict[str, str],
    download: bool,
) -> None:
    for photo_url in photo_urls:
        local_path = download_photo(session, manufacturer, photo_url, cache) if download else ""
        if download and not local_path:
            continue
        connection.execute(
            """
            INSERT OR IGNORE INTO product_photos
                (manufacturer_product_id, source_url, local_path)
            VALUES (?, ?, ?)
            """,
            (product_id, photo_url, local_path),
        )


def parse_alpha(html: str) -> list[dict]:
    match = re.search(r"var DATA=(\[.*?\]);\s*\n", html, re.S)
    if not match:
        raise ValueError("Alpha product data was not found on the page")
    items = json.loads(match.group(1))
    products = []
    for item in items:
        categories = item.get("cats") or ([item["cat"]] if item.get("cat") else [])
        category = ", ".join(ALPHA_CATEGORIES.get(code, code) for code in categories)
        variants = item.get("v") or [{}]
        for index, variant in enumerate(variants):
            packs = variant.get("packs") or [""]
            sizes = variant.get("sizes") or []
            pack_text = ", ".join(str(pack) for pack in packs if pack)
            if sizes:
                pack_text = ", ".join(
                    part for part in [pack_text, ", ".join(str(size) for size in sizes)] if part
                )
            name_en, name_ar = split_languages(item.get("name"))
            composition_en, composition_ar = split_languages(variant.get("comp"))
            form_en, form_ar = split_languages(variant.get("form"))
            pack_en, pack_ar = split_languages(pack_text)
            products.append(
                {
                    "trade_name": name_en,
                    "trade_name_ar": name_ar,
                    "composition": composition_en,
                    "composition_ar": composition_ar,
                    "strength": "",
                    "strength_ar": "",
                    "dosage_form": form_en,
                    "dosage_form_ar": form_ar,
                    "pack": pack_en,
                    "pack_ar": pack_ar,
                    "category": category,
                    "category_ar": "",
                    "uses": "",
                    "uses_ar": "",
                    "price": "",
                    "price_currency": "",
                    "description": "",
                    "description_ar": "",
                    "extra": {
                        "alpha_id": item.get("id"),
                        "variant": index,
                        "export": bool(variant.get("export")),
                    },
                    "source_url": f"https://alpha-syria.com/products.html#{item.get('id')}-{index}",
                    "photos": [
                        "https://alpha-syria.com/img/products/"
                        + urllib.parse.quote(photo)
                        + ".webp"
                        for photo in item.get("photos") or []
                    ],
                }
            )
    return products


ARABIC_LABELS = {
    "التركيب": "composition",
    "التركيب الكيميائي": "composition",
    "المادة الفعالة": "composition",
    "المواد الفعالة": "composition",
    "الشكل الصيدلاني": "dosage_form",
    "العبوة": "pack",
    "التعبئة": "pack",
    "الاستطبابات": "uses",
    "الاستطباب": "uses",
    "الاستعمال": "uses",
    "دواعي الاستعمال": "uses",
    "الزمرة العلاجية": "category",
    "الزمرة الدوائية": "category",
    "التصنيف": "category",
    "السعر": "price",
    "العيار": "strength",
}


def label_key(line: str) -> str:
    cleaned = line.strip().rstrip(":").strip().lower()
    arabic_cleaned = re.sub(r"\s+", " ", line).strip().rstrip(":").strip()
    if arabic_cleaned in ARABIC_LABELS:
        return ARABIC_LABELS[arabic_cleaned]
    if cleaned in LABELS:
        return LABELS[cleaned]
    for label, key in LABELS.items():
        if cleaned.startswith(label + " ") or cleaned.startswith(label + ":"):
            return key
    return ""


def product_heading(soup: BeautifulSoup):
    found = []
    for tag in soup.find_all(["h1", "h2"]):
        english, arabic = split_languages(tag.get_text(" ", strip=True))
        text = english or arabic
        if text and text.lower() not in TITLE_SKIP and len(text) < 140:
            found.append(tag)
    return found[0] if found else None


def content_node(soup: BeautifulSoup):
    heading = product_heading(soup)
    if heading is not None and heading.parent is not None:
        best = heading.parent
        node = heading.parent
        while node.parent is not None and node.parent.name not in {"body", "html"}:
            parent = node.parent
            if len(parent.get_text(strip=True)) > 14000:
                break
            best = parent
            node = parent
        if len(best.get_text(strip=True)) > 40:
            return best
    for selector in (
        ".inside-content",
        ".woocommerce-product-details__short-description",
        "article .entry-content",
        "article",
        "main",
    ):
        node = soup.select_one(selector)
        if node and len(node.get_text(strip=True)) > 40:
            return node
    return soup.body or soup


def extract_labels(node) -> dict[str, str]:
    fields: dict[str, list[str]] = {}

    def add(key: str, value: str) -> None:
        value = re.sub(r"\s+", " ", value or "").strip()
        value = re.split(r"For full information|Download Package Leaflet|Download leaflet", value, maxsplit=1)[0]
        value = re.sub(r"\bComments\b", "", value)
        value = re.sub(r"\bLeaflets?\b", "", value).strip(" .")
        if key == "category" and len(value) > 80:
            return
        if not key or not value or len(value) > 220:
            return
        bucket = fields.setdefault(key, [])
        if value not in bucket and len(bucket) < 3:
            bucket.append(value)

    for row in node.select("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) != 2:
            continue
        add(
            label_key(english_text(cells[0].get_text(" ", strip=True))),
            cells[1].get_text(" ", strip=True),
        )

    lines = [re.sub(r"\s+", " ", line).strip() for line in node.get_text("\n", strip=True).splitlines()]
    lines = [line for line in lines if line]
    index = 0
    while index < len(lines):
        key = label_key(lines[index])
        if not key:
            index += 1
            continue
        inline = lines[index].split(":", 1)[1].strip() if ":" in lines[index] else ""
        if inline:
            add(key, inline)
            index += 1
            continue
        collected = []
        index += 1
        while index < len(lines) and not label_key(lines[index]) and len(collected) < 2:
            if lines[index].lower().startswith(("for full information", "download", "leaflet")):
                break
            collected.append(lines[index])
            index += 1
        if collected:
            add(key, " ".join(collected))
    return {key: " | ".join(values) for key, values in fields.items()}


def extract_price(soup: BeautifulSoup, fields: dict[str, str]) -> tuple[str, str]:
    amount = soup.select_one(".woocommerce-Price-amount, span.price, .product-price")
    if amount:
        text = english_text(amount.get_text(" ", strip=True))
        currency = ""
        if "$" in text or "USD" in text.upper():
            currency = "USD"
        elif "EUR" in text.upper() or "£" in text:
            currency = "EUR"
        elif re.search(r"SYP|S\.P", text, re.I):
            currency = "SYP"
        number = re.search(r"[0-9][0-9,.\s]*", text)
        if number:
            return number.group(0).strip(), currency
    if fields.get("price"):
        match = PRICE_RE.search(fields["price"])
        if match:
            return match.group(1).strip(), (match.group(2) or "").replace("$", "USD")
        return fields["price"], ""
    return "", ""


def extract_images(soup: BeautifulSoup, page_url: str, node) -> list[str]:
    urls: list[str] = []
    candidates = list(node.find_all("img")) if node is not None else []
    candidates.extend(soup.select("img.prod-pic, .woocommerce-product-gallery img, meta[property='og:image']"))
    for image in candidates:
        raw = image.get("content") or image.get("src") or image.get("data-src") or ""
        if not raw or raw.startswith("data:"):
            continue
        absolute = urllib.parse.urljoin(page_url, raw)
        if IMAGE_SKIP_RE.search(absolute):
            continue
        if absolute not in urls:
            urls.append(absolute)
    page_ids = [number for number in re.findall(r"\d+", urllib.parse.urlparse(page_url).path) if len(number) >= 3]
    identified = [url for url in urls if any(number in url for number in page_ids)]
    product_urls = [url for url in urls if re.search(r"product|prod-pic|/files/images/", url, re.I)]
    chosen = identified or product_urls or urls
    return chosen[:4]


def parse_product_page(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    node = content_node(soup)
    fields = extract_labels(node)
    heading = product_heading(soup)
    title_en, title_ar = split_languages(heading.get_text(" ", strip=True) if heading else "")
    if not title_en and not title_ar and soup.title and soup.title.string:
        title_en, title_ar = split_languages(re.split(r"[|\-]", soup.title.string)[0])
    paragraphs = [re.sub(r"\s+", " ", paragraph.get_text(" ", strip=True)) for paragraph in node.find_all("p")]
    description_parts = [
        part for part in paragraphs
        if len(part) > 40 and "produces in excess" not in part.lower() and "all rights reserved" not in part.lower()
    ]
    description_en, description_ar = split_languages(" ".join(description_parts)[:1200])
    bilingual = {key: split_languages(value) for key, value in fields.items()}
    price, currency = extract_price(soup, {key: english_text(value) for key, value in fields.items()})
    leaflets = []
    for anchor in soup.select("a[href]"):
        href = anchor["href"]
        if href.lower().split("?")[0].endswith(".pdf"):
            leaflets.append(urllib.parse.urljoin(url, href))
    def field(name: str) -> tuple[str, str]:
        return bilingual.get(name, ("", ""))

    composition_en, composition_ar = field("composition")
    strength_en, strength_ar = field("strength")
    form_en, form_ar = field("dosage_form")
    pack_en, pack_ar = field("pack")
    category_en, category_ar = field("category")
    uses_en, uses_ar = field("uses")
    return {
        "trade_name": title_en,
        "trade_name_ar": title_ar,
        "composition": composition_en,
        "composition_ar": composition_ar,
        "strength": strength_en,
        "strength_ar": strength_ar,
        "dosage_form": form_en,
        "dosage_form_ar": form_ar,
        "pack": pack_en,
        "pack_ar": pack_ar,
        "category": category_en,
        "category_ar": category_ar,
        "uses": uses_en,
        "uses_ar": uses_ar,
        "price": price,
        "price_currency": currency,
        "description": description_en,
        "description_ar": description_ar,
        "extra": {"leaflets": leaflets[:3]},
        "source_url": url,
        "photos": extract_images(soup, url, node),
        "language_url": language_link(soup, url),
    }
