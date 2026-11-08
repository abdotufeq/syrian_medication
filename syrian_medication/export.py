"""Export database to CSV with MOH and manufacturer data merged."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from .constants import ProductRecord
from .formatter import bold, dim, fmt, green
from .text_utils import ascii_url, name_score, strength_agrees, normalize_name


logger = logging.getLogger(__name__)


def _make_product_record(product: Any, extra: dict[str, Any]) -> dict[str, Any]:
    """Build a flat dict from a manufacturer product row."""
    return {
        "trade_name": product.trade_name or "",
        "trade_name_ar": product.trade_name_ar or "",
        "manufacturer": product.manufacturer or "",
        "manufacturer_ar": product.manufacturer_moh or "",
        "composition": product.composition or "",
        "composition_ar": product.composition_ar or "",
        "strength": product.strength or "",
        "strength_ar": product.strength_ar or "",
        "pack": product.pack or "",
        "pack_ar": product.pack_ar or "",
        "dosage_form": product.dosage_form or "",
        "dosage_form_ar": product.dosage_form_ar or "",
        "category": product.category or "",
        "category_ar": product.category_ar or "",
        "uses": product.uses or "",
        "uses_ar": product.uses_ar or "",
        "price": product.price or "",
        "price_currency": product.price_currency or "",
        "description": product.description or "",
        "description_ar": product.description_ar or "",
        "photos": product.photos if hasattr(product, "photos") else "",
        "photo_urls": product.photo_urls if hasattr(product, "photo_urls") else "",
        "leaflets": " | ".join(extra.get("leaflets") or []),
        "ministry_url": "",
        "manufacturer_url": product.source_url or "",
        "manufacturer_website": product.manufacturer_website or "",
        "moh_id": None,
        "manufacturer_product_id": int(product.id),
        "match_score": 0,
        "record_source": "manufacturer",
    }


def export_database(connection: sqlite3.Connection, path: Path) -> None:
    """Export the merged database to CSV.

    Uses a dict-based lookup keyed on (manufacturer_moh, normalized_name) so
    that matching is O(n) instead of O(n*m).
    """
    moh = pd.read_sql_query("SELECT * FROM moh_medications", connection).fillna("")
    products = pd.read_sql_query(
        """
        SELECT p.*, m.english_name AS manufacturer, m.moh_name AS manufacturer_moh,
               m.website AS manufacturer_website
        FROM manufacturer_products p
        JOIN manufacturers m ON m.id = p.manufacturer_id
        WHERE COALESCE(m.parser, '') != 'msf'   -- MSF Unicat lives in its own database
        """,
        connection,
    ).fillna("")
    photos = pd.read_sql_query(
        "SELECT manufacturer_product_id, source_url, local_path FROM product_photos",
        connection,
    ).fillna("")
    local = photos[photos["local_path"] != ""]
    photo_map = (
        local.groupby("manufacturer_product_id")["local_path"].apply(lambda values: " | ".join(values)).to_dict()
        if not local.empty
        else {}
    )
    photo_url_map = (
        photos.groupby("manufacturer_product_id")["source_url"].apply(lambda values: " | ".join(values)).to_dict()
        if not photos.empty
        else {}
    )
    if not products.empty:
        products["photos"] = products["id"].map(photo_map).fillna("")
        products["photo_urls"] = products["id"].map(photo_url_map).fillna("")
    websites = {
        row["moh_name"]: row["website"] or ""
        for row in connection.execute("SELECT moh_name, website FROM manufacturers")
    }

    # Build lookup: (manufacturer_moh, normalized_trade_name) -> best product row
    product_lookup: dict[tuple[str, str], Any] = {}
    # Build manufacturer-indexed candidate lists for fallback matching
    manufacturer_candidates: dict[str, list[Any]] = {}
    if not products.empty:
        for product in products.itertuples(index=False):
            key = (product.manufacturer_moh, normalize_name(product.trade_name))
            product_lookup[key] = product
            manufacturer_candidates.setdefault(product.manufacturer_moh, []).append(product)

    compiled: list[dict] = []
    matched_ids: set[int] = set()

    for row in moh.itertuples(index=False):
        # Fast lookup by normalized name
        norm_key = (row.manufacturer_moh, normalize_name(row.trade_name))
        best = product_lookup.get(norm_key)
        best_score = 0

        if best is not None:
            best_score = name_score(row.trade_name, best.trade_name, row.manufacturer)
            if best_score and not strength_agrees(
                row.trade_name,
                ProductRecord(
                    trade_name=best.trade_name,
                    composition=best.composition,
                    strength=best.strength,
                    pack=best.pack,
                ),
            ):
                best_score = 0

        # If exact normalized match failed, try only candidates for this manufacturer
        if best_score < 70:
            candidates = manufacturer_candidates.get(row.manufacturer_moh, [])
            for candidate in candidates:
                score = name_score(row.trade_name, candidate.trade_name, row.manufacturer)
                if score and not strength_agrees(
                    row.trade_name,
                    ProductRecord(
                        trade_name=candidate.trade_name,
                        composition=candidate.composition,
                        strength=candidate.strength,
                        pack=candidate.pack,
                    ),
                ):
                    score = 0
                if score > best_score:
                    best = candidate
                    best_score = score

        record = {
            "trade_name": row.trade_name,
            "trade_name_ar": row.trade_name_ar,
            "manufacturer": row.manufacturer,
            "manufacturer_ar": row.manufacturer_moh,
            "composition": row.composition,
            "composition_ar": row.composition_ar,
            "strength": row.strength,
            "strength_ar": row.strength_ar,
            "pack": row.pack,
            "pack_ar": row.pack_ar,
            "dosage_form": row.dosage_form,
            "dosage_form_ar": row.dosage_form_ar,
            "category": "",
            "category_ar": "",
            "uses": "",
            "uses_ar": "",
            "price": "",
            "price_currency": "",
            "description": "",
            "description_ar": "",
            "photos": "",
            "photo_urls": "",
            "leaflets": "",
            "ministry_url": row.source_url,
            "manufacturer_url": "",
            "manufacturer_website": websites.get(row.manufacturer_moh, ""),
            "moh_id": int(row.id),
            "manufacturer_product_id": None,
            "match_score": 0,
            "record_source": "ministry",
        }
        if best is not None and best_score >= 70:
            matched_ids.add(int(best.id))
            extra = json.loads(best.extra_json or "{}")
            record.update(
                {
                    "category": best.category,
                    "category_ar": best.category_ar,
                    "uses": best.uses,
                    "uses_ar": best.uses_ar,
                    "price": best.price,
                    "price_currency": best.price_currency,
                    "description": best.description,
                    "description_ar": best.description_ar,
                    "photos": best.photos,
                    "photo_urls": best.photo_urls,
                    "leaflets": " | ".join(extra.get("leaflets") or []),
                    "manufacturer_url": best.source_url,
                    "manufacturer_product_id": int(best.id),
                    "match_score": best_score,
                    "record_source": "ministry+manufacturer",
                }
            )
            for english_key, arabic_key in (
                ("trade_name", "trade_name_ar"),
                ("composition", "composition_ar"),
                ("strength", "strength_ar"),
                ("dosage_form", "dosage_form_ar"),
                ("pack", "pack_ar"),
            ):
                if not record[english_key]:
                    record[english_key] = getattr(best, english_key) or ""
                if not record[arabic_key]:
                    record[arabic_key] = getattr(best, arabic_key) or ""
            if not record["composition"]:
                record["composition"] = best.composition
            if not record["dosage_form"]:
                record["dosage_form"] = best.dosage_form
            if not record["pack"]:
                record["pack"] = best.pack
        compiled.append(record)

    # Add manufacturer-only products not matched to any MOH row
    if not products.empty:
        for product in products.itertuples(index=False):
            if int(product.id) in matched_ids:
                continue
            extra = json.loads(product.extra_json or "{}")
            compiled.append(_make_product_record(product, extra))

    frame = pd.DataFrame(compiled)
    for column in ("leaflets", "ministry_url", "manufacturer_url", "photo_urls", "manufacturer_website"):
        frame[column] = frame[column].map(
            lambda value: " | ".join(ascii_url(part) for part in str(value).split(" | ") if part and part != "nan")
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    write_medications_table(connection, frame)
    matched = int((frame["record_source"] == "ministry+manufacturer").sum()) if len(frame) else 0
    print(f"\n  {green('✓')}  Unified table `medications`: {bold(fmt(len(frame)))} rows "
          f"({fmt(matched)} MoH rows enriched from manufacturer sites)")
    print(f"  {green('✓')}  CSV exported → {dim(str(path))}")


def write_medications_table(connection: sqlite3.Connection, frame: pd.DataFrame) -> None:
    """Store the merged MoH + manufacturer rows as the `medications` table for the pharmacy app."""
    table = frame.copy()
    table.insert(0, "id", range(1, len(table) + 1))
    for column in ("moh_id", "manufacturer_product_id"):
        table[column] = table[column].astype("Int64")
    connection.execute("DROP TABLE IF EXISTS medications")
    table.to_sql("medications", connection, index=False)
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_medications_trade ON medications(trade_name);
        CREATE INDEX IF NOT EXISTS idx_medications_composition ON medications(composition);
        CREATE INDEX IF NOT EXISTS idx_medications_manufacturer ON medications(manufacturer_ar);
        CREATE INDEX IF NOT EXISTS idx_medications_moh ON medications(moh_id);
        """
    )
    connection.commit()
