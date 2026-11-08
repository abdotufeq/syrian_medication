"""Schema for the standalone MSF Unicat database (data/msf_unicat.sqlite).

The MSF catalog is an international reference of drugs, medical equipment,
renewable supplies (consumables), medical kits and therapeutic nutrition. It
is kept in its own database so it can be used as a broader reference next to
the Syrian (MoH) medication database.
"""

from __future__ import annotations

import sqlite3

from .constants import DATA_DIR, MSF_DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE,            -- e.g. DORA, SINS, EMEQ (4-letter family)
    section TEXT,                -- top-level letter: D, E, S, K, N ...
    name TEXT,
    url TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,   -- MSF article code, e.g. DORAABCV3T-
    name TEXT,
    section TEXT,                -- D, E, S, K, N ...
    section_name TEXT,           -- Drugs, Medical equipment, ...
    family_code TEXT,            -- first 4 characters of the code (DORA ...)
    family_name TEXT,            -- DORA - Oral drugs
    standardization TEXT,        -- STD (standard) / NST (non-standard) / ...
    article_status TEXT,         -- Valid Article / Phased out ...
    account_code TEXT,
    hs_code TEXT,
    former_codes TEXT,
    last_updated TEXT,
    therapeutic_group TEXT,      -- WHO EML based group, drugs only
    atc_code TEXT,               -- WHO ATC code, drugs only
    who_eml INTEGER DEFAULT 0,   -- 1 when listed in the WHO Essential Medicines list
    restriction_code TEXT,       -- order needs justification (S, PM ...)
    thermosensitive_code TEXT,   -- cold-chain / storage temperature code
    emdn_code TEXT,              -- European Medical Device Nomenclature
    device_class TEXT,           -- MDR class I, IIa, IIb, III
    sterile INTEGER DEFAULT 0,
    single_use INTEGER DEFAULT 0,
    ce_marked INTEGER DEFAULT 0,
    part_of_kit INTEGER DEFAULT 0,
    therapeutic_action TEXT,
    indications TEXT,
    instructions TEXT,
    precautions TEXT,
    storage TEXT,
    definition TEXT,
    specifications TEXT,
    packaging TEXT,
    quality_standards TEXT,
    msf_requirements TEXT,
    notes TEXT,
    sheets_json TEXT,            -- every technical-sheet section, title -> text
    attributes_json TEXT,        -- every badge attribute, description -> value
    image_url TEXT,
    image_local TEXT,
    url TEXT,
    scraped_at TEXT
);

CREATE TABLE IF NOT EXISTS item_images (
    id INTEGER PRIMARY KEY,
    item_code TEXT,
    url TEXT,
    local_path TEXT,
    UNIQUE (item_code, url)
);

CREATE TABLE IF NOT EXISTS kit_components (
    id INTEGER PRIMARY KEY,
    kit_code TEXT,
    component_code TEXT,
    standardization TEXT,
    label TEXT,
    quantity TEXT,
    requirement TEXT,            -- mandatory / optional
    url TEXT,
    UNIQUE (kit_code, component_code)
);

CREATE TABLE IF NOT EXISTS related_items (
    id INTEGER PRIMARY KEY,
    item_code TEXT,
    related_code TEXT,
    relation TEXT,               -- First choice / Related to / Alternative ...
    related_name TEXT,
    UNIQUE (item_code, related_code, relation)
);

CREATE INDEX IF NOT EXISTS idx_items_section ON items(section);
CREATE INDEX IF NOT EXISTS idx_items_family ON items(family_code);
CREATE INDEX IF NOT EXISTS idx_items_atc ON items(atc_code);
CREATE INDEX IF NOT EXISTS idx_items_name ON items(name);
CREATE INDEX IF NOT EXISTS idx_kit_components_kit ON kit_components(kit_code);
CREATE INDEX IF NOT EXISTS idx_related_item ON related_items(item_code);
"""


def connect_msf() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(MSF_DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    connection.commit()
    return connection
