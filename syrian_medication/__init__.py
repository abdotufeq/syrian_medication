"""Syrian medication scraper package.

Collects and extracts details about Syrian medications from both
MOH (Ministry of Health) and manufacturer sites to build a comprehensive
database for pharmacy management applications.
"""

from __future__ import annotations

from .cli import main
from .constants import ProductRecord
from .database import connect, upsert_manufacturer
from .export import export_database
from .http_client import create_session, fetch
from .manufacturer_scraper import scrape_site, collect_cards
from .moh_scraper import load_moh_csv, scrape_moh_live
from .product_parser import parse_product_page, save_product, download_photo
from .site_health import check_all_sites
from .text_utils import normalize_name, name_score, split_languages
from .url_utils import gather_product_urls

__all__ = [
    "main",
    "ProductRecord",
    "connect",
    "upsert_manufacturer",
    "export_database",
    "create_session",
    "fetch",
    "scrape_site",
    "load_moh_csv",
    "scrape_moh_live",
    "check_all_sites",
]
