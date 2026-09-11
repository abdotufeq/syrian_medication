from __future__ import annotations

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://www.moh.gov.sy/drugs"
MAX_PAGES = 1999
REQUEST_TIMEOUT = 15


def create_session() -> requests.Session:
    """Create a requests session with retry support."""
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )

    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {"User-Agent": "Mozilla/5.0 (compatible; medication-scraper/1.0)"}
    )
    return session


def extract_table(table) -> tuple[list[str], list[list[str]]]:
    """Extract headers and valid rows from an HTML table."""
    headers = [
        header.get_text(" ", strip=True)
        for header in table.select("thead th, tr:first-child th")
    ]

    if not headers:
        headers = [
            header.get_text(" ", strip=True)
            for header in table.find_all("th")
        ]

    rows = []

    for tr in table.select("tr"):
        cells = tr.find_all("td")
        if len(cells) != len(headers):
            continue

        rows.append([
            cell.get_text(" ", strip=True)
            for cell in cells
        ])

    return headers, rows


def fetch_page(
    session: requests.Session,
    page_number: int,
) -> tuple[list[str], list[list[str]]]:
    """Fetch and parse one page."""
    response = session.get(
        BASE_URL,
        params={"page": page_number},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.find("table")

    if table is None:
        return [], []

    return extract_table(table)


def main() -> pd.DataFrame:
    all_rows: list[list[str]] = []
    columns: list[str] = []

    with create_session() as session:
        for page_number in range(1, MAX_PAGES + 1):
            try:
                page_columns, page_rows = fetch_page(session, page_number)
            except requests.RequestException as error:
                print(f"Skipping page {page_number}: {error}")
                continue

            if not page_rows:
                print(f"No data found on page {page_number}; stopping.")
                break

            if not columns:
                columns = page_columns
            elif page_columns != columns:
                raise ValueError(
                    f"Column mismatch on page {page_number}: "
                    f"{page_columns!r} != {columns!r}"
                )

            all_rows.extend(page_rows)
            print(f"Page {page_number}: collected {len(page_rows)} rows")

    medications = pd.DataFrame(all_rows, columns=columns)
    print(medications)
    return medications


if __name__ == "__main__":
    medications = main()