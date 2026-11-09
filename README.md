# Syrian Medication Database

Builds two SQLite databases for a future pharmacy-management app:

1. **`data/syrian_medications.sqlite`**: every medication registered with the Syrian
   Ministry of Health (MoH), enriched with what manufacturer websites publish
   (price, photos, pack, category, uses, description, leaflets).
2. **`data/msf_unicat.sqlite`**: a broader international reference built from the
   MSF Unicat catalog: drugs, medical equipment, renewable supplies/consumables,
   medical kits and therapeutic nutrition.

## Pipeline

| Step | What happens | Flag |
|------|--------------|------|
| MoH registry | Scrapes all of `moh.gov.sy/drugs` (~13.4k rows). The page count is read from the site's own `totalItems`, pages are fetched in parallel with retries, and a **gap-fill** pass re-queries trade names with `?search=`, because the site's pagination is unstable and otherwise skips rows. The table is only replaced when the run completes. The raw registry is saved to `medications.csv`. | `--moh live` |
| Website discovery | For manufacturers with no website in `manufacturer_sites.py`, searches the web (English and Arabic names), drops directories and social networks, and **verifies** each candidate by visiting it. A site is accepted only when it lists the maker's own (unique) MoH trade names, carries its Arabic name on a Syrian site, or names it in both domain and title on a Syrian site. Every candidate and its evidence is kept for review. | `--discover-sites` |
| Manufacturer sites | Crawls the configured sites plus the discovered ones (sitemaps, listing pages, product pages) for name, composition, strength, pack, category, uses, price, photos and leaflets. | (default) |
| Web enrichment | For MoH medicines still without a product page, searches `site:<maker domain> <trade name>` and parses the page it finds. Resumable: each trade name is searched once. | `--web-enrich` |
| Price bulletins | Imports price lists you saved into `data/price_sources/`: PDF, XLSX or CSV. Files under `official/` are MoH bulletins (نشرة / تعديل أسعار), files under `unofficial/` are anything else. Columns are found by their Arabic or English headers (اسم المستحضر, اسم المعمل, التركيب, العيار, العبوة, سعر العموم ...), and each row is matched to the registry by name, maker, strength and pack. | `--price-files [DIR]` |
| Telegram prices | Reads the public history of price channels (default `t.me/SD_UpToDate`, 2015 onwards) and parses lines like `بيرفكت بون 20000 ل.س` with the maker taken from the post's hashtag. Arabic names are matched to the English registry by consonant skeleton within that maker, with strength, pack and dosage-form checks. Resumable: posts already stored are skipped. | `--telegram-prices` |
| Merge | Matches MoH rows to manufacturer products (token-based name scoring plus strength checks) and writes the unified **`medications`** table and `data/syrian_medications.csv`. | (always) |
| MSF Unicat | Scrapes the pharmacy sections of `unicat.msf.org` with full detail pages into its own database. | `--msf` / `--msf-only` |

## Installation

```bash
uv sync            # or: pip install beautifulsoup4 pandas requests "certifi>=2026.10.18" "urllib3>=2.8.0"
```

## Usage

```bash
# Everything: MoH live, website discovery, manufacturer crawl, web enrichment, MSF
python scrape_medications.py --full

# Complete MoH registry + configured manufacturer sites
python scrape_medications.py --moh live

# Find websites for manufacturers that have none, then crawl them
python scrape_medications.py --discover-sites

# Look up unmatched medicines on their maker's site (start small, it is resumable)
python scrape_medications.py --moh skip --web-enrich --web-limit 200

# MSF Unicat only (pharmacy sections D, E, S, K, N)
python scrape_medications.py --msf-only
python scrape_medications.py --msf-only --msf-sections D,S      # drugs + supplies only
python scrape_medications.py --msf-only --msf-all-families      # also logistic kits, all food

# Prices: bulletins in data/price_sources/ + Telegram channel history
python scrape_medications.py --moh skip --skip-manufacturers --prices
python scrape_medications.py --moh skip --skip-manufacturers --price-files            # files only
python scrape_medications.py --moh skip --skip-manufacturers --telegram-prices --telegram-channel SD_UpToDate
python scrape_medications.py --moh skip --skip-manufacturers --rematch-prices         # re-link after a registry change

# Testing / tuning
python scrape_medications.py --only Alpha --max-products 20 --no-photos
python scrape_medications.py --max-moh-pages 5 --skip-manufacturers
python scrape_medications.py --check-sites
python -m syrian_medication --help
```

### Web search providers

Keyless providers work out of the box: Yahoo first, Bing as a weak fallback. Both
are rate limited, so searches are spaced by `--search-delay` (2s) and every answer
is cached in the `web_search_cache` table. A re-run never repeats a query. For
large runs, set one of these and it is used first:

| Provider | Environment variables |
|----------|-----------------------|
| Brave Search API | `BRAVE_API_KEY` |
| Google Programmable Search | `GOOGLE_CSE_KEY`, `GOOGLE_CSE_ID` |
| SerpAPI | `SERPAPI_KEY` |

Choose the order with `--search-providers brave,yahoo`.

### Prices

Syria has no live public price database. The sources, and how each is handled:

| Source | Official | How it gets in |
|--------|----------|----------------|
| MoH price bulletins (Directorate of Pharmaceutical Affairs, also posted on its Facebook page and re-hosted by news sites) | yes | Download in a browser into `data/price_sources/official/`. Put the effective date in the file name (`2023-12-03.pdf`, `تعديل أسعار 17-01-2023.pdf`), or pass `--price-date`. |
| Company price circulars, pharmacy lists | no | Same, under `data/price_sources/unofficial/`. |
| Telegram price channels | no | `--telegram-prices` (public `t.me/s/<channel>` pages, 1.5 s between pages). Posts that only carry a price-list **image** are kept in `telegram_posts` (`price_lines = 0`, `photo_count > 0`) but not read. |

Currency: Syria removed two zeros from the lira on 2026-01-01 (100 old SYP = 1 new SYP). Prices dated before
that are stored as `SYP_OLD`; `price_syp` is always in new lira. Prices are per pack as registered.

In `medications`, `price` is the latest **official** price for the row, or, when there is none, the latest
unofficial one, then the manufacturer website's. `price_official`, `price_date`, `price_source` and
`price_source_url` say where it came from. Unofficial Telegram prices are mostly from 2016–2023, so check `price_date`.

## Output

| File | Description |
|------|-------------|
| `data/syrian_medications.sqlite` | Syrian database (schema below) |
| `data/syrian_medications.csv` | The `medications` table as CSV |
| `medications.csv` | Raw MoH registry from the last complete live scrape (`--moh csv` reloads it offline) |
| `data/photos/<manufacturer>/` | Manufacturer product photos |
| `data/msf_unicat.sqlite` | MSF Unicat database (schema below) |
| `data/msf_unicat.csv` | MSF `items` table as CSV |
| `data/photos/msf-unicat/` | MSF product photos, named by article code |

### `syrian_medications.sqlite`

- **`medications`**: the table for the app. One row per MoH registration (`moh_id`), merged with the best-matching manufacturer product (`manufacturer_product_id`, `match_score`), plus manufacturer products that matched no MoH row. Bilingual columns (`*_ar`), `price`/`price_currency`, `photos` (local paths), `photo_urls`, `leaflets`, `ministry_url`, `manufacturer_url`, `manufacturer_website`, `record_source` (`ministry`, `ministry+manufacturer`, `manufacturer`).
- **`moh_medications`**: the official registry rows.
- **`manufacturers`**: MoH name, English name, website, `website_source` (`config` / `web_search`), `website_confidence`, `website_evidence`, `social_url`, scrape status.
- **`manufacturer_products`**, **`product_photos`**: raw data scraped from manufacturer sites.
- **`manufacturer_site_candidates`**: every website candidate the discovery step checked, with score and evidence. Review these to confirm or add sites to `manufacturer_sites.py`.
- **`web_enrichment_log`**, **`web_search_cache`**: bookkeeping that makes the web-search steps resumable.
- **`price_sources`**: one row per imported bulletin file or Telegram post (`kind`, `official`, `url`, `published_on`).
- **`medication_prices`**: every price found, matched or not: `moh_id`, raw name/maker/strength/pack, `price`, `currency` (`SYP_OLD`/`SYP`), `price_syp`, `effective_date`, `official`, `match_score`, `match_method` (`name+maker`, `skeleton+maker`, `(pack?)` when the pack size could not be confirmed), `raw_line`.
- **`telegram_posts`**: raw channel posts, so parsing can be improved and re-run without fetching again.

### `msf_unicat.sqlite`

- **`items`**: one row per article code: `name`, `section` (D/E/S/K/N), `family_code`/`family_name` (e.g. `DORA - Oral drugs`), `standardization` (STD/NST), `article_status`, `atc_code`, `therapeutic_group` (WHO EML based), `who_eml`, `thermosensitive_code` (cold chain), `restriction_code`, `emdn_code`, `device_class`, `sterile`, `single_use`, `ce_marked`, `part_of_kit`, `hs_code`, `former_codes`, technical-sheet text (`therapeutic_action`, `indications`, `instructions`, `precautions`, `storage`, `definition`, `specifications`, `packaging`, `quality_standards`, ...), all sheets/attributes as JSON, and the image.
- **`kit_components`**: what each kit contains (component code, label, quantity, mandatory/optional).
- **`related_items`**: first-choice alternatives and related articles.
- **`item_images`**, **`categories`**.

MSF does not publish prices: the catalog shows a hidden 1.00 EUR placeholder, so no price is stored.

## Adding or fixing a manufacturer

Edit `manufacturer_sites.py`. A website set there always overrides one found by search:

```python
{
    "moh_name": "Arabic name exactly as on moh.gov.sy",
    "english_name": "English name",
    "website": "https://example.com",
    "parser": "crawl",                    # or "alpha" for JSON-embedded catalogs
    "seeds": ["https://example.com/products"],
    "product_re": r"/product/[^/]+",      # regex identifying product pages
}
```

## Project structure

```
syrian_medication/
├── syrian_medication/
│   ├── cli.py                   # CLI and pipeline order
│   ├── constants.py             # Paths, patterns, dosage-form translations
│   ├── database.py              # Syrian DB schema, manufacturer upsert
│   ├── moh_scraper.py           # MoH registry: parallel pages + search gap fill
│   ├── web_search.py            # Search providers (Brave/Google/SerpAPI/Yahoo/Bing) + cache
│   ├── discovery.py             # Website discovery + per-medicine web enrichment
│   ├── manufacturer_scraper.py  # Manufacturer site crawling
│   ├── product_parser.py        # Product page parsing, photos
│   ├── url_utils.py             # Sitemaps, pagination, listing links
│   ├── export.py                # MoH ↔ manufacturer matching, `medications` table, CSV
│   ├── prices.py                # Price bulletins (PDF/XLSX/CSV), Telegram prices, matching
│   ├── msf_database.py          # MSF DB schema
│   ├── msf_scraper.py           # MSF Unicat scraper
│   ├── site_health.py           # --check-sites
│   ├── formatter.py, text_utils.py, http_client.py
├── manufacturer_sites.py        # Manufacturer site configuration
├── scrape_medications.py        # Entry point (same as python -m syrian_medication)
├── main.py                      # MoH-only shortcut
└── data/                        # Generated output (git-ignored)
```
