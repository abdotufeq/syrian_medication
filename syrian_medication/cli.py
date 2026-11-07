"""Command-line interface and main entry point."""

from __future__ import annotations

import argparse
import logging

from manufacturer_sites import SITES

from .constants import CSV_PATH, DB_PATH, MOH_CSV
from .database import connect, upsert_manufacturer
from .export import export_database
from .http_client import create_session
from .manufacturer_scraper import scrape_site
from .moh_scraper import load_moh_csv, scrape_moh_live
from .site_health import check_all_sites


logger = logging.getLogger(__name__)


def selected_sites(only: list[str]) -> list[dict]:
    if not only:
        return SITES
    needles = [item.lower() for item in only]
    return [
        site
        for site in SITES
        if any(needle in site["english_name"].lower() or needle in site["moh_name"].lower() for needle in needles)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build an English database of Syrian medicines from MOH and manufacturer sites.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scrape_medications.py                    # Full run from CSV\n"
            "  python scrape_medications.py --moh live         # Scrape MOH live\n"
            "  python scrape_medications.py --check-sites      # Check manufacturer site status\n"
            "  python scrape_medications.py --only Alpha       # Only Alpha manufacturer\n"
            "  python scrape_medications.py --max-products 20  # Limit to 20 products per site\n"
            "  python scrape_medications.py --no-photos        # Skip photo downloads\n"
            "  python scrape_medications.py --skip-manufacturers  # Only load MOH data\n"
        ),
    )
    parser.add_argument(
        "--moh",
        choices=("csv", "live", "skip"),
        default="csv" if MOH_CSV.exists() else "live",
        help="Load the Ministry list from medications.csv, scrape it live, or leave it unchanged.",
    )
    parser.add_argument("--max-moh-pages", type=int, default=1999, help="Maximum pages to scrape from MOH site.")
    parser.add_argument("--only", action="append", default=[], help="Manufacturer English name, repeatable.")
    parser.add_argument("--max-products", type=int, default=0, help="Cap product pages per manufacturer. 0 means all.")
    parser.add_argument("--delay", type=float, default=0.05, help="Delay between listing page requests (seconds).")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent workers for product page fetching.")
    parser.add_argument("--no-photos", action="store_true", help="Skip downloading product photos.")
    parser.add_argument("--skip-manufacturers", action="store_true", help="Only load MOH data, skip manufacturer sites.")
    parser.add_argument("--check-sites", action="store_true", help="Check manufacturer site accessibility and exit.")
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # If only checking sites, do that and exit
    if args.check_sites:
        logger.info("Site check mode - checking all manufacturer sites")
        results = check_all_sites()
        
        # Print detailed report
        print("\n" + "="*80)
        print("MANUFACTURER SITE STATUS REPORT")
        print("="*80)
        
        for status_group in ["accessible", "no_website", "forbidden", "connection_error", "timeout", "error", "ssl_error"]:
            group = [r for r in results if r["status"] == status_group]
            if not group:
                continue
            
            icon = {
                "accessible": "✓ ACCESSIBLE",
                "no_website": "○ NO WEBSITE",
                "forbidden": "✗ FORBIDDEN",
                "connection_error": "✗ CONNECTION ERROR",
                "timeout": "✗ TIMEOUT",
                "error": "✗ ERROR",
                "ssl_error": "⚠ SSL ERROR",
            }.get(status_group, status_group)
            
            print(f"\n{icon} ({len(group)} sites):")
            for r in group:
                product_info = f" (~{r['product_count']} product links)" if r.get('product_count') else ""
                print(f"  - {r['name']}")
                print(f"    Website: {r['website']}")
                print(f"    Details: {r['message']}{product_info}")
        
        print("\n" + "="*80)
        return

    logger.info("="*60)
    logger.info("Starting Syrian medication scraper")
    logger.info("="*60)
    
    connection = connect()
    for site in SITES:
        upsert_manufacturer(connection, site)
    connection.commit()

    session = create_session()
    if args.moh == "csv":
        load_moh_csv(connection, SITES)
    elif args.moh == "live":
        scrape_moh_live(connection, session, args.max_moh_pages, SITES)

    if not args.skip_manufacturers:
        limit = args.max_products or None
        sites = selected_sites(args.only)
        configured = [s for s in sites if s.get("parser")]
        
        logger.info("="*60)
        logger.info("Scraping %d manufacturer sites", len(configured))
        logger.info("="*60)
        
        for idx, site in enumerate(configured, start=1):
            logger.info("[%d/%d] %s", idx, len(configured), site["english_name"])
            scrape_site(
                connection,
                session,
                site,
                limit,
                args.delay,
                download=not args.no_photos,
                workers=max(1, args.workers),
            )

    export_database(connection, CSV_PATH)
    counts = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM moh_medications) AS ministry,
            (SELECT COUNT(*) FROM manufacturer_products) AS manufacturer_products,
            (SELECT COUNT(*) FROM product_photos) AS photos,
            (SELECT COUNT(*) FROM manufacturers WHERE website != '') AS websites
        """
    ).fetchone()
    
    logger.info("="*60)
    logger.info("Database %s:", DB_PATH.name)
    logger.info("  Ministry rows: %d", counts["ministry"])
    logger.info("  Manufacturer rows: %d", counts["manufacturer_products"])
    logger.info("  Photos: %d", counts["photos"])
    logger.info("  Sites configured: %d", counts["websites"])
    logger.info("CSV exported to: %s", CSV_PATH)
    logger.info("="*60)
    
    connection.close()
    logger.info("Done")


if __name__ == "__main__":
    main()
