"""SQLite database connection, schema management, and manufacturer records."""

from __future__ import annotations

import sqlite3

from .constants import DATA_DIR, DB_PATH, PHOTO_DIR


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS manufacturers (
            id INTEGER PRIMARY KEY,
            moh_name TEXT UNIQUE,
            english_name TEXT,
            website TEXT,
            parser TEXT,
            status TEXT,
            product_count INTEGER DEFAULT 0,
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS moh_medications (
            id INTEGER PRIMARY KEY,
            trade_name TEXT,
            manufacturer_moh TEXT,
            manufacturer TEXT,
            composition TEXT,
            strength TEXT,
            pack TEXT,
            dosage_form TEXT,
            source_url TEXT,
            UNIQUE (trade_name, manufacturer_moh, composition, strength, pack, dosage_form)
        );
        CREATE TABLE IF NOT EXISTS manufacturer_products (
            id INTEGER PRIMARY KEY,
            manufacturer_id INTEGER,
            trade_name TEXT,
            composition TEXT,
            strength TEXT,
            dosage_form TEXT,
            pack TEXT,
            category TEXT,
            uses TEXT,
            price TEXT,
            price_currency TEXT,
            description TEXT,
            extra_json TEXT,
            source_url TEXT,
            UNIQUE (manufacturer_id, source_url, trade_name, composition, strength, pack)
        );
        CREATE TABLE IF NOT EXISTS product_photos (
            id INTEGER PRIMARY KEY,
            manufacturer_product_id INTEGER,
            source_url TEXT,
            local_path TEXT,
            UNIQUE (manufacturer_product_id, source_url)
        );
        """
    )
    ensure_column(connection, "moh_medications", "trade_name_ar", "TEXT")
    ensure_column(connection, "moh_medications", "composition_ar", "TEXT")
    ensure_column(connection, "moh_medications", "strength_ar", "TEXT")
    ensure_column(connection, "moh_medications", "pack_ar", "TEXT")
    ensure_column(connection, "moh_medications", "dosage_form_ar", "TEXT")
    for column in (
        "trade_name_ar",
        "composition_ar",
        "strength_ar",
        "dosage_form_ar",
        "pack_ar",
        "category_ar",
        "uses_ar",
        "description_ar",
    ):
        ensure_column(connection, "manufacturer_products", column, "TEXT")
    for column, declaration in (
        ("website_source", "TEXT DEFAULT 'config'"),   # config | web_search
        ("website_confidence", "INTEGER"),
        ("website_evidence", "TEXT"),
        ("social_url", "TEXT"),
    ):
        ensure_column(connection, "manufacturers", column, declaration)

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS manufacturer_site_candidates (
            id INTEGER PRIMARY KEY,
            manufacturer_id INTEGER,
            domain TEXT,
            url TEXT,
            score INTEGER,
            evidence_json TEXT,
            checked_at TEXT,
            UNIQUE (manufacturer_id, domain)
        );
        CREATE TABLE IF NOT EXISTS web_enrichment_log (
            id INTEGER PRIMARY KEY,
            manufacturer_id INTEGER,
            stem TEXT,
            query TEXT,
            status TEXT,           -- saved | no_result | no_match
            url TEXT,
            attempted_at TEXT,
            UNIQUE (manufacturer_id, stem)
        );
        """
    )

    # Create indexes for fast lookups
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_mp_manufacturer_id
            ON manufacturer_products(manufacturer_id);
        CREATE INDEX IF NOT EXISTS idx_mp_source_url
            ON manufacturer_products(source_url);
        CREATE INDEX IF NOT EXISTS idx_mp_trade_name
            ON manufacturer_products(trade_name);
        CREATE INDEX IF NOT EXISTS idx_mp_manufacturer_trade
            ON manufacturer_products(manufacturer_id, trade_name);
        CREATE INDEX IF NOT EXISTS idx_pp_product_id
            ON product_photos(manufacturer_product_id);
        CREATE INDEX IF NOT EXISTS idx_mm_trade_name
            ON moh_medications(trade_name);
        CREATE INDEX IF NOT EXISTS idx_mm_manufacturer
            ON moh_medications(manufacturer_moh);
        """
    )
    connection.commit()
    return connection


def ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def upsert_manufacturer(connection: sqlite3.Connection, site: dict) -> int:
    if site.get("discovered"):
        # Website came from web search and is already stored on the row.
        row = connection.execute("SELECT id FROM manufacturers WHERE moh_name = ?", (site["moh_name"],)).fetchone()
        if row:
            return int(row["id"])
    connection.execute(
        """
        INSERT INTO manufacturers (moh_name, english_name, website, parser, status)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(moh_name) DO UPDATE SET
            english_name = excluded.english_name,
            -- keep a website found by web search unless the config now provides one
            website = CASE
                WHEN excluded.website != '' THEN excluded.website
                WHEN manufacturers.website_source = 'web_search' THEN manufacturers.website
                ELSE excluded.website END,
            parser = CASE
                WHEN excluded.website != '' OR manufacturers.website_source != 'web_search' THEN excluded.parser
                ELSE manufacturers.parser END,
            website_source = CASE
                WHEN excluded.website != '' THEN 'config'
                ELSE manufacturers.website_source END
        """,
        (
            site["moh_name"],
            site["english_name"],
            site.get("website") or "",
            site.get("parser") or "",
            "pending" if site.get("parser") else "no public catalog configured",
        ),
    )
    row = connection.execute(
        "SELECT id FROM manufacturers WHERE moh_name = ?",
        (site["moh_name"],),
    ).fetchone()
    return int(row["id"])


def register_moh_manufacturers(connection: sqlite3.Connection) -> int:
    """Add every manufacturer named in the MoH registry that the site config lacks."""
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO manufacturers (moh_name, english_name, website, parser, status)
        SELECT DISTINCT manufacturer_moh, COALESCE(NULLIF(manufacturer, ''), ''), '', '', 'not configured'
        FROM moh_medications WHERE manufacturer_moh != ''
        """
    )
    connection.commit()
    return cursor.rowcount


def manufacturer_english(name: str, sites=None) -> str:
    """Look up the English manufacturer name from the site registry."""
    if sites is None:
        from manufacturer_sites import SITES as sites
    for site in sites:
        if site["moh_name"] == name:
            return site["english_name"]
    return ""