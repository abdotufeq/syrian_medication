# Syrian Medication Database

Scrape and merge every detail about Syrian medications from the Ministry of Health (MOH) registry and individual manufacturer websites into a single enriched database, ready for building a pharmacy management application.

## What it does

1. **MOH registry** — Fetches the official drug list from `moh.gov.sy/drugs` (live or from a pre-saved CSV).
2. **Manufacturer catalogs** — Crawls 30+ Syrian pharmaceutical manufacturer sites to collect trade names, composition, dosage form, pack size, therapeutic category, price, photos, and package leaflets.
3. **Bilingual support** — Splits mixed Arabic/English text so every field has both `*_en` and `*_ar` columns.
4. **Name matching** — Matches MOH entries to manufacturer products using token-based fuzzy scoring with strength validation.
5. **Export** — Produces a SQLite database and a single enriched CSV (`data/syrian_medications.csv`).

## Installation

```bash
# Using uv (recommended)
uv sync

# Or with pip
pip install beautifulsoup4 pandas requests urllib3
```

## Usage

```bash
# Full run: load MOH from CSV + scrape all manufacturer sites
python scrape_medications.py

# Scrape MOH live from moh.gov.sy
python scrape_medications.py --moh live

# Only scrape specific manufacturers
python scrape_medications.py --only Alpha --only Asia

# Limit to 20 products per manufacturer (testing)
python scrape_medications.py --max-products 20

# Skip photo downloads (faster)
python scrape_medications.py --no-photos

# Only load MOH data, skip manufacturers
python scrape_medications.py --skip-manufacturers

# Quick MOH-only scrape (shortcut)
python main.py

# Verbose / debug logging
python scrape_medications.py -v
```

## Output

| File | Description |
|------|-------------|
| `data/syrian_medications.sqlite` | Full SQLite database with 4 tables |
| `data/syrian_medications.csv` | Merged CSV: MOH + manufacturer data |
| `data/photos/<manufacturer>/` | Downloaded product photos |

## Database schema

- **manufacturers** — MOH Arabic name, English name, website, scrape status
- **moh_medications** — Official MOH drug registry rows
- **manufacturer_products** — Enriched product data from manufacturer sites
- **product_photos** — Photo URLs and local file paths

## CSV columns

Each row contains: `trade_name`, `trade_name_ar`, `manufacturer`, `manufacturer_ar`,
`composition`, `composition_ar`, `strength`, `strength_ar`, `pack`, `pack_ar`,
`dosage_form`, `dosage_form_ar`, `category`, `category_ar`, `uses`, `uses_ar`,
`price`, `price_currency`, `description`, `description_ar`, `photos`, `leaflets`,
`ministry_url`, `manufacturer_url`, `match_score`, `record_source`.

## Adding a new manufacturer

Edit `manufacturer_sites.py` and append a new entry to the `SITES` list:

```python
{
    "moh_name": "Arabic name from MOH",
    "english_name": "English name",
    "website": "https://example.com",
    "parser": "crawl",          # or "alpha" for JSON-embedded catalogs
    "seeds": ["https://example.com/products"],
    "product_re": r"/product/[^/]+",   # regex to identify product pages
}
```

## Usage

```bash
# All commands work via the package or the legacy wrapper:

python -m syrian_medication --moh live
python scrape_medications.py --moh live    # backward compatible
python main.py                              # MOH-only shortcut
```

## Project structure

```
syrian_medication/
├── syrian_medication/           # Python package (modular code)
│   ├── __init__.py              # Package exports
│   ├── __main__.py              # python -m syrian_medication entry
│   ├── constants.py             # Constants, patterns, ProductRecord
│   ├── text_utils.py            # Text processing (name matching, etc.)
│   ├── http_client.py           # HTTP session, fetch, caching
│   ├── database.py              # SQLite schema, manufacturer upsert
│   ├── moh_scraper.py           # MOH CSV import and live scraping
│   ├── product_parser.py        # Product page parsing, photos
│   ├── url_utils.py             # URL utilities, sitemap, pagination
│   ├── manufacturer_scraper.py  # Manufacturer site orchestration
│   ├── export.py                # CSV export with MOH + manufacturer merge
│   ├── site_health.py           # Manufacturer site health checking
│   └── cli.py                   # CLI argument parsing and main()
├── scrape_medications.py        # Backward-compatible wrapper
├── manufacturer_sites.py        # Manufacturer site configurations
├── main.py                      # Quick MOH-only entry point
├── data/                        # Output directory
│   ├── syrian_medications.sqlite
│   ├── syrian_medications.csv
│   └── photos/
├── pyproject.toml
└── README.md
```
