"""Syrian medication scraper package.

Collects and extracts details about Syrian medications from both
MOH (Ministry of Health) and manufacturer sites to build a comprehensive
database for pharmacy management applications.
"""

from __future__ import annotations

from .cli import main
from .constants import ProductRecord
from .database import connect, register_moh_manufacturers, upsert_manufacturer
from .discovery import discover_manufacturer_websites, discovered_sites, enrich_from_web
from .export import export_database
from .formatter import (
    ProgressBar, banner, bold, box, c, cyan, dim, divider, fmt,
    fmt_duration, green, red, section, status_icon, summary_table, yellow,
)
from .http_client import create_session, fetch
from .manufacturer_scraper import scrape_site, collect_cards
from .moh_scraper import load_moh_csv, scrape_moh_live
from .msf_database import connect_msf
from .msf_scraper import discover_categories, scrape_all_msf, scrape_msf, scrape_msf_catalog
from .product_parser import parse_product_page, save_product, download_photo
from .site_health import check_all_sites
from .text_utils import normalize_name, name_score, split_languages
from .web_search import SearchResult, WebSearch
from .url_utils import gather_product_urls

__all__ = [
    # CLI
    "main",
    # Data models
    "ProductRecord",
    # Database
    "connect",
    "connect_msf",
    "register_moh_manufacturers",
    "upsert_manufacturer",
    # Web search / discovery
    "WebSearch",
    "SearchResult",
    "discover_manufacturer_websites",
    "discovered_sites",
    "enrich_from_web",
    # Export
    "export_database",
    # HTTP
    "create_session",
    "fetch",
    # Formatting
    "ProgressBar",
    "banner",
    "bold",
    "box",
    "c",
    "cyan",
    "dim",
    "divider",
    "fmt",
    "fmt_duration",
    "green",
    "red",
    "section",
    "status_icon",
    "summary_table",
    "yellow",
    # Scraping
    "scrape_site",
    "collect_cards",
    "load_moh_csv",
    "scrape_moh_live",
    "scrape_msf",
    "scrape_all_msf",
    "scrape_msf_catalog",
    "discover_categories",
    "parse_product_page",
    "save_product",
    "download_photo",
    "check_all_sites",
    # Text utilities
    "normalize_name",
    "name_score",
    "split_languages",
    # URL utilities
    "gather_product_urls",
]
