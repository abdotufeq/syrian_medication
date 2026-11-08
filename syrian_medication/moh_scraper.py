"""Ministry of Health (MoH) medication scraping."""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from bs4 import BeautifulSoup

from .constants import DOSAGE_FORMS, MOH_CSV, MOH_ROWS_PER_PAGE, MOH_URL
from .formatter import ProgressBar, bold, dim, fmt, fmt_duration, green, red, yellow
from .http_client import create_session, fetch
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
    """Insert MOH rows, returning the count of actually inserted (new) rows."""
    cursor = connection.executemany(
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
    return cursor.rowcount

MOH_CSV_COLUMNS = ["الاسم التجاري", "اسم المعمل", "التركيب", "العيار", "العبوة", "الشكل الصيدلاني", "source_url"]


def load_moh_csv(connection, sites=None) -> int:
    """Load a previously saved Ministry list (medications.csv) instead of scraping."""
    frame = pd.read_csv(MOH_CSV, dtype=str, keep_default_na=False)
    rows = []
    for record in frame.itertuples(index=False, name=None):
        cells = list(record)
        source = cells[6] if len(cells) > 6 and cells[6] else MOH_URL
        rows.append(moh_record(cells[:6], source, sites))
    with connection:
        connection.execute("DELETE FROM moh_medications")
        store_moh_rows(connection, rows)
    stored = connection.execute("SELECT COUNT(*) AS n FROM moh_medications").fetchone()["n"]
    print(f"  {green('✓')}  Ministry CSV loaded: {bold(fmt(stored))} unique rows from {fmt(len(rows))} lines in {dim(MOH_CSV.name)}")
    return stored


_local = threading.local()


def _thread_session() -> requests.Session:
    if not hasattr(_local, "session"):
        _local.session = create_session()
    return _local.session


def moh_total_items(html: str) -> int:
    """Read the registry size the Next.js page embeds (``totalItems``)."""
    match = re.search(r'totalItems\\?"\s*:\s*(\d+)', html)
    return int(match.group(1)) if match else 0


def fetch_moh_page(page_number: int, attempts: int = 3, search: str = "") -> tuple[int, list[list[str]]] | None:
    """Return (totalItems, table rows) for one registry page; None when it keeps failing.

    ``search`` uses the site's own filter (``?search=``), which matches a
    substring of the trade name, manufacturer or composition.
    """
    params = {"page": page_number} if page_number > 1 else {}
    if search:
        params["search"] = search
    url = f"{MOH_URL}?{urllib.parse.urlencode(params)}" if params else MOH_URL
    for attempt in range(attempts):
        try:
            response = fetch(_thread_session(), url, use_cache=False)
        except requests.RequestException as error:
            logger.debug("MOH %s attempt %d failed: %s", url, attempt + 1, error)
            time.sleep(1.5 * (attempt + 1))
            continue
        table = BeautifulSoup(response.text, "html.parser").find("table")
        if table is None:
            return moh_total_items(response.text), []
        _, rows = extract_table(table)
        return moh_total_items(response.text), rows
    return None


def _source(page: int, search: str = "") -> str:
    params = {"search": search} if search else {}
    if page > 1:
        params["page"] = page
    return f"{MOH_URL}?{urllib.parse.urlencode(params)}" if params else MOH_URL


def search_all(search: str) -> tuple[int, list[tuple[tuple[str, ...], str]]]:
    """Every row the site returns for a search (all pages). Returns (reported total, rows)."""
    first = fetch_moh_page(1, search=search)
    if first is None:
        return 0, []
    total, rows = first
    found = [(tuple(cells[:6]), _source(1, search)) for cells in rows]
    for page in range(2, math.ceil(total / MOH_ROWS_PER_PAGE) + 1):
        result = fetch_moh_page(page, search=search)
        if result:
            found.extend((tuple(cells[:6]), _source(page, search)) for cells in result[1])
    return total, found


def trade_stem(trade_name: str) -> str:
    """First meaningful word of a trade name ('VOLTARILE-75 AMP.' -> 'VOLTARILE')."""
    for token in re.split(r"[\s\-/().,+]+", trade_name.upper()):
        if len(token) >= 3 and not token.isdigit():
            return token
    return ""


def gap_fill(
    rows: dict[tuple[str, ...], str],
    workers: int,
    delay: float,
) -> int:
    """Recover rows the unstable pagination skipped.

    The registry's ordering is not stable between pages, so a plain page walk
    sees some rows twice and misses others. For every manufacturer whose
    reported count (via ``?search=<manufacturer>``) exceeds what was collected,
    re-query each of its trade-name stems; small result sets fit on one page
    and are therefore complete. Returns the number of rows recovered.
    """
    by_maker: dict[str, set[str]] = {}
    count_by_maker: dict[str, int] = {}
    for cells in rows:
        maker = cells[1]
        count_by_maker[maker] = count_by_maker.get(maker, 0) + 1
        stem = trade_stem(cells[0])
        if stem:
            by_maker.setdefault(maker, set()).add(stem)

    def maker_total(maker: str) -> tuple[str, int]:
        time.sleep(delay)
        result = fetch_moh_page(1, search=maker)
        return maker, result[0] if result else 0

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        totals = dict(pool.map(maker_total, list(count_by_maker)))
    gaps = {maker: totals[maker] - count_by_maker[maker] for maker in count_by_maker
            if totals.get(maker, 0) > count_by_maker[maker]}
    if not gaps:
        return 0
    stems = sorted({stem for maker in gaps for stem in by_maker.get(maker, ())})
    print(f"  {dim(f'Gap fill: {len(gaps)} manufacturers short by {fmt(sum(gaps.values()))} rows → {fmt(len(stems))} trade-name searches')}")

    before = len(rows)
    bar = ProgressBar(total=len(stems), label="MOH gap fill")

    def task(stem: str):
        time.sleep(delay)
        return search_all(stem)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for done, (_, found) in enumerate(pool.map(task, stems), start=1):
            for cells, source in found:
                if len(cells) == 6 and cells not in rows:
                    rows[cells] = source
            bar.update(done, extra=f"+{fmt(len(rows) - before)} rows")
    bar.finish(message=f"+{fmt(len(rows) - before)} rows recovered")
    return len(rows) - before


def scrape_moh_live(
    connection,
    session: requests.Session,
    max_pages: int = 0,
    sites=None,
    workers: int = 4,
    delay: float = 0.1,
    fill_gaps: bool = True,
) -> int:
    """Scrape the complete Ministry of Health registry (moh.gov.sy/drugs).

    1. Read ``totalItems`` from page 1 to know the page count (12 rows/page).
    2. Fetch every page concurrently with retries; retry failed pages again.
    3. Gap fill: re-query trade names of manufacturers that came up short
       (the site's pagination is unstable, see :func:`gap_fill`).
    4. Replace the table only when nothing failed, so a partial run never
       wipes good data; also save the raw registry to medications.csv.
    """
    start_time = time.time()
    print(f"\n  {bold('Scraping MOH live from')} {dim(MOH_URL)}")
    first = fetch_moh_page(1)
    if first is None:
        print(f"  {red('✗')}  Ministry site unreachable")
        return 0
    expected = first[0]
    total_pages = math.ceil(expected / MOH_ROWS_PER_PAGE) if expected else 2000
    if max_pages:
        total_pages = min(total_pages, max_pages)
    print(f"  {dim(f'Registry reports {fmt(expected)} medications → {fmt(total_pages)} pages')}")

    pages: dict[int, list[list[str]]] = {1: first[1]}
    failed: list[int] = []
    bar = ProgressBar(total=total_pages, label="MOH pages")

    def task(number: int):
        time.sleep(delay)
        return number, fetch_moh_page(number)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(task, number) for number in range(2, total_pages + 1)]
        for done, future in enumerate(as_completed(futures), start=2):
            number, result = future.result()
            if result is None:
                failed.append(number)
            else:
                pages[number] = result[1]
            bar.update(done, extra=f"{fmt(sum(len(r) for r in pages.values()))} rows, {len(failed)} failed")
    bar.finish(message=f"{fmt(sum(len(r) for r in pages.values()))} rows")

    for number in sorted(failed):
        result = fetch_moh_page(number, attempts=5)
        if result is not None:
            pages[number] = result[1]
    still_failed = [number for number in failed if number not in pages]

    # Pages past the reported end, in case the registry grew during the run.
    number = total_pages + 1
    while not max_pages:
        result = fetch_moh_page(number)
        if not result or not result[1]:
            break
        pages[number] = result[1]
        number += 1

    rows: dict[tuple[str, ...], str] = {}
    page_rows = 0
    for number in sorted(pages):
        for cells in pages[number]:
            page_rows += 1
            rows.setdefault(tuple(cells[:6]), _source(number))
    after_pages = len(rows)

    recovered = 0
    if fill_gaps and not max_pages and not still_failed:
        recovered = gap_fill(rows, workers, delay)

    records = [moh_record(list(cells), source, sites) for cells, source in rows.items()]
    complete = not still_failed and not max_pages
    with connection:
        if complete:
            connection.execute("DELETE FROM moh_medications")
        store_moh_rows(connection, records)
    stored = connection.execute("SELECT COUNT(*) AS n FROM moh_medications").fetchone()["n"]

    if complete:
        pd.DataFrame(
            [[*cells, source] for cells, source in rows.items()], columns=MOH_CSV_COLUMNS,
        ).to_csv(MOH_CSV, index=False, encoding="utf-8-sig")

    elapsed = time.time() - start_time
    print(f"  {green('✓')}  {fmt(page_rows)} rows read from pages → {fmt(after_pages)} distinct"
          + (f" + {fmt(recovered)} recovered by gap fill" if recovered else "")
          + f" → {bold(fmt(stored))} in database (site reports {fmt(expected)})")
    if still_failed:
        print(f"  {yellow('⚠')}  {len(still_failed)} pages failed ({', '.join(map(str, still_failed[:10]))}"
              f"{' …' if len(still_failed) > 10 else ''}); existing rows were kept and merged. Re-run to fill gaps.")
    elif complete:
        if expected and stored < expected:
            print(f"  {dim(f'{fmt(expected - stored)} reported rows not seen: duplicates in the registry or products the gap fill could not reach')}")
        print(f"  {dim(f'Raw registry saved to {MOH_CSV.name} (use --moh csv to reload offline)')}")
    print(f"  {dim(f'{fmt_duration(elapsed)} elapsed')}\n")
    return stored
