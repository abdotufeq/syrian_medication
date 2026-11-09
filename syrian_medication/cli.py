"""Command-line interface and main entry point."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from manufacturer_sites import SITES

from .constants import CSV_PATH, DB_PATH, MOH_CSV, MSF_CSV_PATH, MSF_DB_PATH, PRICE_SOURCES_DIR
from .database import connect, register_moh_manufacturers, upsert_manufacturer
from .discovery import discover_manufacturer_websites, discovered_sites, enrich_from_web
from .export import export_database
from .formatter import (
    banner, bold, cyan, dim, fmt, green, red, section, status_icon, summary_table, yellow,
)
from .http_client import create_session
from .manufacturer_scraper import scrape_site
from .moh_scraper import load_moh_csv, scrape_moh_live
from .msf_scraper import PHARMACY_SCOPE, scrape_msf_catalog
from .prices import (
    DEFAULT_TELEGRAM_CHANNELS, import_price_files, print_price_summary, rematch_prices, scrape_telegram_prices,
)
from .site_health import check_all_sites
from .web_search import DEFAULT_PROVIDERS, WebSearch


logger = logging.getLogger(__name__)

EXAMPLES = """\
Examples:
  python scrape_medications.py --full                 # everything: MoH live, discovery, manufacturers, web search, MSF
  python scrape_medications.py --moh live             # complete MoH registry + configured manufacturer sites
  python scrape_medications.py --discover-sites       # also search the web for missing manufacturer websites
  python scrape_medications.py --web-enrich --web-limit 200   # look up 200 unmatched medicines on maker sites
  python scrape_medications.py --msf-only             # only the MSF Unicat database (drugs, equipment, supplies ...)
  python scrape_medications.py --msf-only --msf-sections D,S  # only MSF drugs + renewable supplies
  python scrape_medications.py --check-sites          # check manufacturer site status
  python scrape_medications.py --moh skip --skip-manufacturers --prices   # import bulletins + Telegram prices only
  python scrape_medications.py --moh skip --skip-manufacturers --price-files   # only the bulletin files
  python scrape_medications.py --only Alpha --max-products 20 --no-photos
"""


def matches(site: dict, needles: list[str]) -> bool:
    return any(
        needle in (site.get("english_name") or "").lower() or needle in site["moh_name"].lower()
        for needle in needles
    )


def selected_sites(only: list[str], sites: list[dict] = SITES) -> list[dict]:
    if not only:
        return sites
    needles = [item.lower() for item in only]
    return [site for site in sites if matches(site, needles)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a Syrian medication database (MoH registry enriched from manufacturer "
            "websites and web search) and a separate MSF Unicat pharmacy reference database."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    parser.add_argument("--full", action="store_true",
                        help="Run every step: --moh live --discover-sites --web-enrich --msf.")

    moh = parser.add_argument_group("Ministry of Health registry")
    moh.add_argument("--moh", choices=("csv", "live", "skip"), default=None,
                     help="Scrape moh.gov.sy live, reload medications.csv, or keep the current table "
                          "(default: csv when medications.csv exists, otherwise live).")
    moh.add_argument("--max-moh-pages", type=int, default=0, help="Cap MoH pages for testing (0 = all).")
    moh.add_argument("--no-gap-fill", action="store_true",
                     help="Skip the search-based pass that recovers rows the MoH pagination skips.")

    makers = parser.add_argument_group("Manufacturer websites")
    makers.add_argument("--only", action="append", default=[], help="Manufacturer name filter, repeatable.")
    makers.add_argument("--max-products", type=int, default=0, help="Cap products per manufacturer (0 = all).")
    makers.add_argument("--delay", type=float, default=0.05, help="Delay between listing requests (seconds).")
    makers.add_argument("--workers", type=int, default=8, help="Concurrent workers for page fetching.")
    makers.add_argument("--no-photos", action="store_true", help="Do not download product photos.")
    makers.add_argument("--skip-manufacturers", action="store_true", help="Do not crawl manufacturer sites.")
    makers.add_argument("--check-sites", action="store_true", help="Check manufacturer site accessibility and exit.")

    web = parser.add_argument_group("Web search")
    web.add_argument("--discover-sites", action="store_true",
                     help="Search the web for websites of manufacturers that have none configured, "
                          "verify them, then crawl the accepted ones.")
    web.add_argument("--rediscover", action="store_true",
                     help="With --discover-sites, re-check websites accepted by an earlier search.")
    web.add_argument("--web-enrich", action="store_true",
                     help="Search manufacturer domains for MoH medicines that still have no product page.")
    web.add_argument("--web-limit", type=int, default=0, help="Max trade names to search with --web-enrich (0 = all).")
    web.add_argument("--search-delay", type=float, default=2.0, help="Seconds between search queries per provider.")
    web.add_argument("--search-providers", default=",".join(DEFAULT_PROVIDERS),
                     help="Comma-separated provider order (API providers need BRAVE_API_KEY, "
                          "GOOGLE_CSE_KEY+GOOGLE_CSE_ID or SERPAPI_KEY).")

    prices = parser.add_argument_group("Prices")
    prices.add_argument("--prices", action="store_true",
                        help="Import price bulletin files and read Telegram price posts (= --price-files --telegram-prices).")
    prices.add_argument("--price-files", nargs="?", const=str(PRICE_SOURCES_DIR), default=None, metavar="DIR",
                        help=f"Import PDF/XLSX/CSV price lists from DIR (default data/price_sources): files under "
                             "official/ are MoH bulletins, files under unofficial/ are not. Put the date in the file name.")
    prices.add_argument("--price-date", default=None,
                        help="Effective date (YYYY-MM-DD) for bulletin files whose name has no date.")
    prices.add_argument("--telegram-prices", action="store_true",
                        help="Read price posts from public Telegram channels (resumable).")
    prices.add_argument("--telegram-channel", action="append", default=[],
                        help=f"Channel username, repeatable (default: {', '.join(DEFAULT_TELEGRAM_CHANNELS)}).")
    prices.add_argument("--telegram-pages", type=int, default=0, help="Max channel pages per run (0 = all).")
    prices.add_argument("--telegram-full", action="store_true",
                        help="Walk the whole channel history again instead of stopping at stored posts.")
    prices.add_argument("--rematch-prices", action="store_true",
                        help="Re-link stored prices to MoH rows (run after the registry changes; "
                             "implied by --moh live).")

    msf = parser.add_argument_group("MSF Unicat (separate database)")
    msf.add_argument("--msf", "--msf-all", dest="msf", action="store_true",
                     help=f"Also scrape MSF Unicat into {MSF_DB_PATH.name}.")
    msf.add_argument("--msf-only", action="store_true", help="Only scrape MSF Unicat; skip the Syrian database.")
    msf.add_argument("--msf-sections", default=",".join(PHARMACY_SCOPE),
                     help="Section letters: D drugs, E equipment, S supplies, K kits, N nutrition (default: all five).")
    msf.add_argument("--msf-all-families", action="store_true",
                     help="Keep every family in the chosen sections (e.g. logistics kits), not only pharmacy ones.")
    msf.add_argument("--msf-refresh", action="store_true", help="Re-fetch MSF articles already in the database.")

    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging.")
    return parser


def run_site_check() -> None:
    banner("Manufacturer Site Health Check")
    results = check_all_sites()
    status_order = ["accessible", "ssl_error", "no_website", "forbidden", "connection_error", "timeout", "error"]
    group_labels = {
        "accessible": green("✓ ACCESSIBLE"),
        "ssl_error": yellow("⚠ SSL ERROR (works with verify=False)"),
        "no_website": dim("○ NO WEBSITE"),
        "forbidden": red("✗ FORBIDDEN (blocks scrapers)"),
        "connection_error": red("✗ CONNECTION ERROR"),
        "timeout": red("✗ TIMEOUT"),
        "error": red("✗ ERROR"),
    }
    for status_group in status_order:
        group = [r for r in results if r["status"] == status_group]
        if not group:
            continue
        section(f"{group_labels.get(status_group, status_group.upper())}  ({len(group)} sites)")
        for r in group:
            pc = r.get("product_count")
            product_info = f"  {dim(f'~{pc} products')}" if pc else ""
            print(f"  {status_icon(r['status'])}  {r['name']}{product_info}")
            if r.get("website"):
                print(f"      {dim(r['website'])}")
            if r.get("message"):
                print(f"      {dim(r['message'])}")
        print()
    accessible = sum(1 for r in results if r["status"] == "accessible")
    no_website = sum(1 for r in results if r["status"] == "no_website")
    errors = sum(1 for r in results if r["status"] not in ("accessible", "no_website"))
    summary_table(
        [
            ("Total sites", str(len(results))),
            ("Accessible", green(fmt(accessible))),
            ("No website", dim(fmt(no_website))),
            ("Errors", red(fmt(errors))),
        ],
        title="Summary",
    )


def run_msf(args, session) -> dict[str, int]:
    sections = [letter.strip().upper() for letter in args.msf_sections.split(",") if letter.strip()]
    return scrape_msf_catalog(
        session,
        sections=sections,
        all_families=args.msf_all_families,
        max_items=args.max_products,
        workers=min(max(1, args.workers), 6),
        delay=max(args.delay, 0.15),
        download_photos=not args.no_photos,
        refresh=args.msf_refresh,
    )


def main() -> None:
    args = build_parser().parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    if args.full:
        args.moh = args.moh or "live"
        args.discover_sites = args.web_enrich = args.msf = args.prices = True
    if args.prices:
        args.price_files = args.price_files or str(PRICE_SOURCES_DIR)
        args.telegram_prices = True

    if args.check_sites:
        run_site_check()
        return

    session = create_session()

    if args.msf_only:
        banner("MSF Unicat Pharmacy Database")
        totals = run_msf(args, session)
        summary_table(
            [(f"Section {letter}", green(fmt(count))) for letter, count in totals.items()]
            + [("Database", str(MSF_DB_PATH)), ("CSV export", str(MSF_CSV_PATH))],
            title="MSF Unicat",
        )
        return

    banner("Syrian Medication Database Builder")
    connection = connect()
    for site in SITES:
        upsert_manufacturer(connection, site)
    connection.commit()

    moh_mode = args.moh or ("csv" if MOH_CSV.exists() else "live")
    if moh_mode == "csv":
        load_moh_csv(connection, SITES)
    elif moh_mode == "live":
        scrape_moh_live(
            connection, session, args.max_moh_pages, SITES,
            workers=min(max(1, args.workers), 6), fill_gaps=not args.no_gap_fill,
        )
    added = register_moh_manufacturers(connection)
    if added:
        print(f"  {dim(f'{added} MoH manufacturers missing from manufacturer_sites.py were registered')}")

    searcher = None
    if args.discover_sites or args.web_enrich:
        providers = [name.strip() for name in args.search_providers.split(",") if name.strip()]
        searcher = WebSearch(connection, providers=providers, delay=args.search_delay)
        print(f"  {dim('Search providers: ' + (', '.join(searcher.providers) or 'none'))}")

    if args.discover_sites and searcher:
        discover_manufacturer_websites(connection, session, searcher, SITES, only=args.only, recheck=args.rediscover)

    limit = args.max_products or None
    if not args.skip_manufacturers:
        all_sites = discovered_sites(connection, SITES)
        configured = [site for site in selected_sites(args.only, all_sites) if site.get("parser") and site.get("website")]
        if configured:
            section(f"Manufacturer Sites  ({len(configured)} sites to scrape)")
        for idx, site in enumerate(configured, start=1):
            tag = f"  {dim('(found by web search)')}" if site.get("discovered") else ""
            print(f"  {cyan(f'[{idx}/{len(configured)}]')}  {site['english_name'] or site['moh_name']}{tag}")
            scrape_site(
                connection, session, site, limit, args.delay,
                download=not args.no_photos, workers=max(1, args.workers),
            )

    if args.web_enrich and searcher:
        enrich_from_web(connection, session, searcher, only=args.only, limit=args.web_limit,
                        download=not args.no_photos)

    # moh_medications ids change whenever the registry is reloaded, so re-link stored prices first.
    has_prices = connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'medication_prices'").fetchone()
    if args.rematch_prices or (has_prices and moh_mode in ("live", "csv")):
        rematch_prices(connection)
    if args.price_files:
        import_price_files(connection, Path(args.price_files), default_date=args.price_date)
    if args.telegram_prices:
        scrape_telegram_prices(
            connection, session, args.telegram_channel or DEFAULT_TELEGRAM_CHANNELS,
            max_pages=args.telegram_pages, delay=max(args.delay, 1.0), full=args.telegram_full,
        )
    if args.price_files or args.telegram_prices or args.rematch_prices:
        print_price_summary(connection)

    export_database(connection, CSV_PATH)
    counts = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM moh_medications) AS ministry,
            (SELECT COUNT(*) FROM manufacturer_products p JOIN manufacturers m ON m.id = p.manufacturer_id
                WHERE COALESCE(m.parser, '') != 'msf') AS manufacturer_products,
            (SELECT COUNT(*) FROM product_photos WHERE local_path != '') AS photos,
            (SELECT COUNT(*) FROM manufacturers WHERE website != '' AND COALESCE(parser, '') != 'msf') AS websites,
            (SELECT COUNT(*) FROM manufacturers WHERE website_source = 'web_search') AS discovered,
            (SELECT COUNT(*) FROM medications WHERE record_source = 'ministry+manufacturer') AS enriched,
            (SELECT COUNT(*) FROM medications WHERE price_source != ''
                AND price_source != 'manufacturer website') AS priced
        """
    ).fetchone()
    connection.close()

    msf_totals = run_msf(args, session) if args.msf else {}

    banner("Scrape Complete")
    rows = [
        ("Database", str(DB_PATH)),
        ("MoH registrations", green(fmt(counts["ministry"]))),
        ("Enriched from manufacturers", green(fmt(counts["enriched"]))),
        ("Manufacturer products", green(fmt(counts["manufacturer_products"]))),
        ("Photos downloaded", green(fmt(counts["photos"]))),
        ("Priced from bulletins/Telegram", green(fmt(counts["priced"]))),
        ("Manufacturer websites", f"{counts['websites']} ({counts['discovered']} found by web search)"),
        ("CSV export", str(CSV_PATH)),
    ]
    if msf_totals:
        rows += [("MSF Unicat articles", green(fmt(sum(msf_totals.values())))), ("MSF database", str(MSF_DB_PATH))]
    summary_table(rows, title="Results")
    print(f"  {green('✓')}  {bold('All done!')}\n")


if __name__ == "__main__":
    main()
