"""Ministry of Health (MoH) medication scraping."""

from __future__ import annotations

import logging
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup

from .constants import DOSAGE_FORMS, MOH_CSV, MOH_URL
from .http_client import fetch
from .text_utils import english_text, split_languages


logger = logging.getLogger(__name__)


def manufacturer_english(name: str, sites=None) -> str:
    """Look up the English manufacturer name from the site registry."""
    if sites is None:
        from manufacturer_sites import SITES as sites
    for site in sites:
        if site["moh_name"] == name:
            return site["english_name"]
    return ""


def extract_table(table) -> tuple[list[str], list[list[str]]]:
    """Extract headers and valid rows from an HTML table."""
    headers = [
        header.get_text(" ", strip=True)
        for header in table.select("thead th, tr:first-child th")
    ]
    if not headers:
        headers = [header.get_text(" ", strip=True) for header in table.find_all("th")]

    rows = []
    for tr in table.select("tr"):
        cells = tr.find_all("td")
        if len(cells) != len(headers):
            continue
        rows.append([cell.get_text(" ", strip=True) for cell in cells])
    return headers, rows

def moh_record(cells: list[str], source_url: str, sites=None) -> tuple:
    trade, maker, composition, strength, pack, form_ar = cells[:6]
    trade_en, trade_ar = split_languages(trade)
    composition_en, composition_ar = split_languages(composition)
    strength_en, strength_ar = split_languages(strength)
    pack_en, pack_ar = split_languages(pack)
    return (
        trade_en or english_text(trade),
        trade_ar,
        maker,
        manufacturer_english(maker, sites),
        composition_en,
        composition_ar,
        strength_en,
        strength_ar,
        pack_en,
        pack_ar,
        DOSAGE_FORMS.get(form_ar, ""),
        form_ar,
        source_url,
    )

def store_moh_rows(connection: sqlite3.Connection, rows: list[tuple]) -> int:
    connection.executemany(
        """
        INSERT OR IGNORE INTO moh_medications (
            trade_name, trade_name_ar, manufacturer_moh, manufacturer,
            composition, composition_ar, strength, strength_ar, pack, pack_ar,
            dosage_form, dosage_form_ar, source_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    connection.commit()
    return len(rows)

def load_moh_csv(connection, sites=None) -> int:
    frame = pd.read_csv(MOH_CSV)
    rows = [
        moh_record(list(record), MOH_URL, sites)
        for record in frame.itertuples(index=False, name=None)
    ]
    connection.execute("DELETE FROM moh_medications")
    store_moh_rows(connection, rows)
    stored = connection.execute("SELECT COUNT(*) AS n FROM moh_medications").fetchone()["n"]
    logger.info("Ministry list: %d unique rows from %d lines in %s", stored, len(rows), MOH_CSV.name)
    return stored

def scrape_moh_live(connection, session: requests.Session, max_pages: int, sites=None) -> int:
    """Scrape the Ministry of Health website page by page with progress logging."""
    connection.execute("DELETE FROM moh_medications")
    connection.commit()
    stored = 0
    total_rows = 0
    start_time = time.time()
    
    logger.info("="*60)
    logger.info("Starting MOH live scrape from %s", MOH_URL)
    logger.info("="*60)
    
    for page_number in range(1, max_pages + 1):
        try:
            response = fetch(session, f"{MOH_URL}?page={page_number}")
        except requests.RequestException as error:
            logger.warning("Skipping Ministry page %d: %s", page_number, error)
            continue
        
        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.find("table")
        if table is None:
            logger.info("No table on Ministry page %d; stopping.", page_number)
            break
        
        _, page_rows = extract_table(table)
        if not page_rows:
            logger.info("No data on Ministry page %d; stopping.", page_number)
            break
        
        source = f"{MOH_URL}?page={page_number}"
        stored += store_moh_rows(
            connection,
            [moh_record(row, source, sites) for row in page_rows],
        )
        total_rows += len(page_rows)
        
        # Log progress every 10 pages
        if page_number % 10 == 0:
            elapsed = time.time() - start_time
            rate = page_number / elapsed if elapsed > 0 else 0
            logger.info(
                "Progress: %d pages, %d total rows, %d unique stored (%.1f pages/sec)",
                page_number, total_rows, stored, rate
            )
        else:
            logger.debug("Ministry page %d: %d rows", page_number, len(page_rows))
        
        time.sleep(0.2)
    
    elapsed = time.time() - start_time
    logger.info("="*60)
    logger.info("MOH scrape complete: %d unique rows from %d total rows in %.1f seconds", stored, total_rows, elapsed)
    logger.info("="*60)
    return stored