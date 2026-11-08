"""Find manufacturer websites and missing product pages with web search.

Two steps, both cached and resumable:

1. :func:`discover_manufacturer_websites` - for every MoH manufacturer that
   has no configured website, search the web (English + Arabic name), drop
   directories / social networks, and *verify* each candidate by visiting it.
   A candidate is accepted only with real evidence: the site mentions the
   manufacturer's own MoH trade names, or its Arabic name together with Syria.
   Accepted sites are stored on the ``manufacturers`` row and then crawled by
   the normal manufacturer scraper; every candidate is kept in
   ``manufacturer_site_candidates`` for review.

2. :func:`enrich_from_web` - for MoH medicines that still have no matching
   manufacturer product, search ``site:<manufacturer domain> <trade name>``
   and parse the product page found (price, photos, pack, leaflet ...).
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from .formatter import ProgressBar, bold, cyan, dim, fmt, green, section, yellow
from .http_client import fetch
from .moh_scraper import trade_stem
from .product_parser import attach_photos, parse_product_page, save_product
from .text_utils import normalize_name
from .url_utils import sitemap_locations
from .web_search import WebSearch

logger = logging.getLogger(__name__)

GENERIC_EN = {
    "PHARMA", "PHARMACEUTICAL", "PHARMACEUTICALS", "INDUSTRIES", "INDUSTRY", "LABORATORIES",
    "LABORATORY", "LABS", "LAB", "COMPANY", "CO", "LTD", "LLC", "LIMITED", "LIABILITY", "THE",
    "AL", "EL", "FOR", "AND", "MEDICAL", "MEDICINE", "MEDICINES", "HUMAN", "SYRIA", "SYRIAN",
    "GROUP", "BRANCH", "DRUG", "DRUGS", "CHEMICAL", "CHEMICALS", "INTERNATIONAL", "FACTORY",
    "OF", "MIDDLE", "EAST", "NATIONAL", "UNITED", "NEW", "DAMASCUS", "ALEPPO", "HOMS",
}
GENERIC_AR = {
    "شركة", "معمل", "مختبرات", "المختبرات", "للصناعات", "الصناعات", "الدوائية", "الصيدلانية",
    "المحدودة", "محدودة", "المسؤولية", "المساهمة", "المغفلة", "الخاصة", "فرع", "ريف", "دمشق",
    "الطبية", "للادوية", "للأدوية", "الأدوية", "البشرية", "للدواء", "الدواء", "حلب", "حمص",
    "الشركة", "السورية", "سوريا", "و", "ال",
}
BLOCKED_DOMAINS = (
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com",
    "tiktok.com", "t.me", "telegram.", "whatsapp.com", "pinterest.", "reddit.com", "wikipedia.org",
    "wikimapia.org", "google.", "yahoo.com", "bing.com", "doctors.sy", "industrysy.com",
    "info-clipper.com", "aeroleads.com", "aleppochamber.com", "dnb.com", "zoominfo.com",
    "kompass.com", "crunchbase.com", "opencorporates.com", "alibaba.com", "indiamart.com",
    "made-in-china.com", "scribd.com", "moh.gov.sy", "who.int", "drugs.com", "medscape.com",
    "webmd.com", "rxlist.com", "yellowpages", "2gis.", "foursquare.com", "tripadvisor.",
    "glassdoor.", "bayt.com", "rocketreach.co", "signalhire.com", "apollo.io", "emis.com",
    "businesslist", "cybo.com", "yelp.", "mapcarta.com", "waze.com", "amazon.", "ebay.",
    "fda.gov", "ema.europa.eu", "researchgate.net", "sciencedirect.com", "ncbi.nlm.nih.gov",
    # Syrian directories / news / syndicates: they list manufacturers but are not their sites
    "damaspharmacy.sy", "syriadirectory.net", "findglocal.com", "commercialsy.com", "manhom.com",
    "syria-report.com", "einnews.com", "gulfnews.com", "sana.sy", "enabbaladi.net",
)
SOCIAL_DOMAINS = ("facebook.com", "instagram.com", "linkedin.com")
SYRIA_RE = re.compile(r"syria|syrian|damascus|aleppo|homs|hama|latakia|tartus|سوري|دمشق|حلب|حمص|حماة|اللاذقية|طرطوس", re.I)
OTHER_COUNTRY_RE = re.compile(r"\b(jordan|amman|egypt|cairo|india|pakistan|iran|turkey|istanbul|saudi|riyadh)\b", re.I)
GENERIC_PRODUCT_RE = r"/(?:[a-z]{2}/)?(?:products?|drugs?|medicines?|items?|portfolio|our-products)/[^/?#]+"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _origin(url: str) -> str:
    parts = urllib.parse.urlparse(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def _domain(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


def _blocked(domain: str) -> bool:
    return any(blocked in domain for blocked in BLOCKED_DOMAINS)


def distinctive_tokens(english: str, arabic: str) -> tuple[set[str], set[str]]:
    en = {token for token in normalize_name(english).split() if token not in GENERIC_EN and len(token) >= 3}
    ar_words = re.findall(r"[؀-ۿ]+", arabic or "")
    ar = {word for word in ar_words if word not in GENERIC_AR and len(word) >= 3}
    return en, ar


def _page_text(session: requests.Session, url: str) -> tuple[str, str, list[str]]:
    """(final url, visible text, internal links) of a page; ('', '', []) on failure."""
    try:
        response = fetch(session, url, use_cache=False)
    except requests.RequestException:
        return "", "", []
    if "html" not in response.headers.get("content-type", "html"):
        return "", "", []
    soup = BeautifulSoup(response.text, "html.parser")
    links = [urllib.parse.urljoin(response.url, anchor["href"]) for anchor in soup.select("a[href]")]
    for tag in soup(["script", "style", "noscript", "select", "option", "datalist"]):  # country pickers are not evidence
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return response.url, f"{title}\n{soup.get_text(' ', strip=True)}", links


def verify_candidate(
    session: requests.Session,
    url: str,
    english: str,
    arabic: str,
    product_stems: list[str],
) -> tuple[int, dict]:
    """Visit a candidate site and score how likely it is the manufacturer's own site."""
    final_url, text, links = _page_text(session, _origin(url) or url)
    if not text:
        return 0, {"error": "unreachable"}
    domain = _domain(final_url)
    product_pages = [
        link for link in links
        if _domain(link) == domain and re.search(r"product|drug|medicine|catalog|منتج", link, re.I)
    ][:3]
    for link in product_pages:
        _, extra, _ = _page_text(session, link)
        text += " " + extra
    title = text.split("\n")[0][:200]
    try:
        slugs = " ".join(sitemap_locations(session, final_url, [])[:3000])
    except requests.RequestException:
        slugs = ""
    upper = normalize_name(text + " " + re.sub(r"[/_.:-]+", " ", urllib.parse.unquote(slugs)))
    en_tokens, ar_tokens = distinctive_tokens(english, arabic)

    evidence: dict = {"url": final_url}
    score = 0
    domain_label = domain.split(".")[0].replace("-", "").upper()
    domain_hits = [
        token for token in en_tokens
        if len(token) >= 4 and (token in domain_label or _similar(token, domain_label))
    ]
    if domain_hits:
        score += 25
        evidence["domain_tokens"] = domain_hits
    title_words = normalize_name(title).split()
    title_hit = any(_similar(token, word) for token in en_tokens if len(token) >= 4 for word in title_words)
    if en_tokens and (title_hit or any(re.search(rf"\b{re.escape(token)}\b", upper) for token in en_tokens)):
        score += 10
        evidence["english_name"] = True
    if title_hit:
        evidence["title_name"] = True
    arabic_hit = bool(ar_tokens) and all(token in text for token in ar_tokens)
    if arabic_hit:
        score += 20
        evidence["arabic_name"] = True
    syria = bool(SYRIA_RE.search(text) or domain.endswith(".sy"))
    if syria:
        score += 15
        evidence["mentions_syria"] = True
    elif OTHER_COUNTRY_RE.search(text):
        score -= 20
        evidence["other_country"] = OTHER_COUNTRY_RE.search(text).group(0)
    hits = [stem for stem in product_stems if re.search(rf"\b{re.escape(stem)}\b", upper)]
    if hits:
        score += min(40, 8 * len(hits))
        evidence["product_hits"] = hits[:15]
    evidence["accepted_rule"] = (
        len(hits) >= 2                                   # sells this maker's own trade names
        or (arabic_hit and syria)                        # carries its Arabic name, Syrian site
        or (bool(domain_hits) and title_hit and syria)   # name in domain and page title, Syrian site
    )
    return score, evidence


def _similar(token: str, word: str) -> bool:
    """Equal, or a close transliteration (MYAMED ~ MIAMED, ALRAZI ~ ALRAZY)."""
    if token == word:
        return True
    if min(len(token), len(word)) < 5 or abs(len(token) - len(word)) > 2:
        return False
    return difflib.SequenceMatcher(None, token, word).ratio() >= 0.8


def _maker_stems(connection: sqlite3.Connection, moh_name: str, limit: int = 80) -> list[str]:
    """Trade-name stems registered *only* by this manufacturer.

    Stems shared with other manufacturers are usually ingredient names
    (PARACETAMOL, DICLOFENAC ...) and prove nothing about who owns a site.
    """
    owners: dict[str, set[str]] = {}
    ingredients: set[str] = set()
    for row in connection.execute("SELECT trade_name, composition, manufacturer_moh FROM moh_medications"):
        stem = trade_stem(row["trade_name"] or "")
        if stem:
            owners.setdefault(stem, set()).add(row["manufacturer_moh"])
        ingredients.update(normalize_name(row["composition"] or "").split())
    stems: dict[str, int] = {}
    for row in connection.execute("SELECT trade_name FROM moh_medications WHERE manufacturer_moh = ?", (moh_name,)):
        stem = trade_stem(row["trade_name"] or "")
        if len(stem) >= 5 and owners.get(stem) == {moh_name} and stem not in ingredients:
            stems[stem] = stems.get(stem, 0) + 1
    return [stem for stem, _ in sorted(stems.items(), key=lambda item: -item[1])][:limit]


PHARMA_HINT_RE = re.compile(r"pharm|drug|medic|laborator|labs|دوائ|صيدل|أدوية|ادوية|دواء|مختبر", re.I)


def relevant(result, english: str, arabic: str) -> bool:
    """A search hit is worth visiting when it names the maker and looks pharmaceutical."""
    en_tokens, ar_tokens = distinctive_tokens(english, arabic)
    blob = f"{result.title} {result.snippet} {result.url}"
    upper = normalize_name(blob) + " " + result.domain.upper().replace("-", "")
    named = any(token in upper for token in en_tokens) or any(token in blob for token in ar_tokens)
    return named and bool(PHARMA_HINT_RE.search(blob))


def discover_manufacturer_websites(
    connection: sqlite3.Connection,
    session: requests.Session,
    searcher: WebSearch,
    sites: list[dict],
    only: list[str] | None = None,
    min_score: int = 50,
    recheck: bool = False,
) -> int:
    """Search for websites of manufacturers that have none configured. Returns sites accepted."""
    configured_domains = {_domain(site["website"]) for site in sites if site.get("website")}
    rows = connection.execute(
        """
        SELECT m.id, m.moh_name, m.english_name, m.website, m.website_source,
               (SELECT COUNT(*) FROM moh_medications mm WHERE mm.manufacturer_moh = m.moh_name) AS registrations
        FROM manufacturers m
        WHERE m.parser != 'msf'
        ORDER BY registrations DESC
        """
    ).fetchall()
    needles = [needle.lower() for needle in (only or [])]
    targets = [
        row for row in rows
        if (not row["website"] or (recheck and row["website_source"] == "web_search"))
        and row["registrations"] > 0
        and (not needles or any(n in (row["english_name"] or "").lower() or n in row["moh_name"].lower() for n in needles))
    ]
    section(f"Website discovery  ({len(targets)} manufacturers without a website)")
    if not searcher.available:
        print(f"    {yellow('⚠')}  No search provider available")
        return 0

    accepted = 0
    for index, row in enumerate(targets, start=1):
        english, arabic = row["english_name"] or "", row["moh_name"]
        print(f"  {cyan(f'[{index}/{len(targets)}]')}  {english or arabic}  {dim(f'({row["registrations"]} MoH registrations)')}")
        queries = [
            f'"{english}" Syria pharmaceutical' if english else "",
            f"{arabic} سوريا",
            f"{english} pharma Syria products" if english else "",
        ]
        candidates: dict[str, str] = {}
        social = ""
        for query in filter(None, queries):
            for result in searcher.search(query, limit=10):
                domain = result.domain
                if not social and any(s in domain for s in SOCIAL_DOMAINS):
                    en_tokens, ar_tokens = distinctive_tokens(english, arabic)
                    blob = f"{result.title} {result.url}".upper()
                    if any(token in blob for token in en_tokens) or any(token in result.title for token in ar_tokens):
                        social = result.url
                if _blocked(domain) or domain in configured_domains or not relevant(result, english, arabic):
                    continue
                candidates.setdefault(domain, result.url)

        stems = _maker_stems(connection, arabic)
        best: tuple[int, str, dict] = (0, "", {})
        for domain, url in list(candidates.items())[:6]:
            score, evidence = verify_candidate(session, url, english, arabic, stems)
            connection.execute(
                """
                INSERT INTO manufacturer_site_candidates (manufacturer_id, domain, url, score, evidence_json, checked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(manufacturer_id, domain) DO UPDATE SET
                    url = excluded.url, score = excluded.score,
                    evidence_json = excluded.evidence_json, checked_at = excluded.checked_at
                """,
                (row["id"], domain, evidence.get("url", url), score, json.dumps(evidence, ensure_ascii=False), _now()),
            )
            if evidence.get("accepted_rule") and score > best[0]:
                best = (score, evidence.get("url", url), evidence)
        if social:
            connection.execute("UPDATE manufacturers SET social_url = ? WHERE id = ?", (social, row["id"]))

        if best[0] >= min_score:
            website = _origin(best[1])
            connection.execute(
                """
                UPDATE manufacturers
                SET website = ?, parser = 'crawl', website_source = 'web_search',
                    website_confidence = ?, website_evidence = ?, status = 'pending'
                WHERE id = ?
                """,
                (website, best[0], json.dumps(best[2], ensure_ascii=False), row["id"]),
            )
            configured_domains.add(_domain(website))
            accepted += 1
            print(f"      {green('✓')}  {website}  {dim(f'score {best[0]}')}")
        else:
            tried = ", ".join(list(candidates)[:4]) or "no candidates"
            print(f"      {dim(f'○  not found ({tried})')}")
        connection.commit()

    print(f"\n  {bold('Discovery:')} {green(fmt(accepted))} websites accepted · "
          f"{fmt(searcher.queries)} searches, {fmt(searcher.cache_hits)} cached\n")
    return accepted


def discovered_sites(connection: sqlite3.Connection, sites: list[dict]) -> list[dict]:
    """Configured sites plus crawl configs for websites found by discovery."""
    by_name = {site["moh_name"]: dict(site) for site in sites}
    for row in connection.execute(
        "SELECT moh_name, english_name, website FROM manufacturers WHERE website_source = 'web_search' AND website != ''"
    ):
        site = by_name.get(row["moh_name"], {"moh_name": row["moh_name"], "english_name": row["english_name"]})
        if site.get("parser") and site.get("website"):
            continue
        website = row["website"].rstrip("/")
        site.update({
            "website": website,
            "parser": "crawl",
            "seeds": [website + "/", website + "/products", website + "/en/products"],
            "product_re": GENERIC_PRODUCT_RE,
            "discovered": True,
        })
        by_name[row["moh_name"]] = site
    return list(by_name.values())


def _matched_stems(connection: sqlite3.Connection, manufacturer_id: int) -> set[str]:
    stems = set()
    for row in connection.execute(
        "SELECT trade_name FROM manufacturer_products WHERE manufacturer_id = ?", (manufacturer_id,)
    ):
        for token in normalize_name(row["trade_name"]).split():
            stems.add(token)
    return stems


def enrich_from_web(
    connection: sqlite3.Connection,
    session: requests.Session,
    searcher: WebSearch,
    only: list[str] | None = None,
    limit: int = 0,
    download: bool = True,
) -> int:
    """Search manufacturer domains for MoH medicines that have no product page yet."""
    makers = connection.execute(
        """
        SELECT id, moh_name, english_name, website FROM manufacturers
        WHERE website != '' AND parser != 'msf'
        """
    ).fetchall()
    needles = [needle.lower() for needle in (only or [])]
    work: list[tuple[sqlite3.Row, str]] = []
    for maker in makers:
        if needles and not any(n in (maker["english_name"] or "").lower() or n in maker["moh_name"].lower() for n in needles):
            continue
        have = _matched_stems(connection, maker["id"])
        tried = {
            row["stem"] for row in connection.execute(
                "SELECT stem FROM web_enrichment_log WHERE manufacturer_id = ?", (maker["id"],)
            )
        }
        stems = sorted({
            stem for row in connection.execute(
                "SELECT trade_name FROM moh_medications WHERE manufacturer_moh = ?", (maker["moh_name"],)
            )
            if (stem := trade_stem(row["trade_name"] or "")) and len(stem) >= 4
        })
        work.extend((maker, stem) for stem in stems if stem not in have and stem not in tried)
    if limit:
        work = work[:limit]

    section(f"Web enrichment  ({fmt(len(work))} unmatched trade names to search)")
    if not work or not searcher.available:
        return 0

    saved = 0
    photo_cache: dict[str, str] = {}
    bar = ProgressBar(total=len(work), label="Web search")
    for done, (maker, stem) in enumerate(work, start=1):
        domain = _domain(maker["website"])
        query = f"site:{domain} {stem}"
        status, found_url = "no_result", ""
        known = {
            row["source_url"] for row in connection.execute(
                "SELECT source_url FROM manufacturer_products WHERE manufacturer_id = ?", (maker["id"],)
            )
        }
        for result in searcher.search(query, limit=5):
            if not (result.domain == domain or result.domain.endswith("." + domain)):
                continue
            if result.url in known or re.search(r"\.(pdf|jpe?g|png|webp)$", result.url, re.I):
                continue
            try:
                response = fetch(session, result.url, use_cache=False)
                product = parse_product_page(response.url, response.text)
            except (requests.RequestException, ValueError):
                continue
            title = normalize_name(f"{product.get('trade_name', '')} {result.title}")
            if stem not in title.split():
                status = "no_match"
                continue
            product.setdefault("extra", {}).update({"found_via": "web_search", "query": query})
            product_id = save_product(connection, maker["id"], product)
            if product_id:
                attach_photos(connection, session, product_id, maker["english_name"] or domain,
                              product.get("photos") or [], photo_cache, download)
                saved += 1
                status, found_url = "saved", response.url
            break
        connection.execute(
            """
            INSERT OR REPLACE INTO web_enrichment_log (manufacturer_id, stem, query, status, url, attempted_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (maker["id"], stem, query, status, found_url, _now()),
        )
        connection.commit()
        bar.update(done, extra=f"{fmt(saved)} pages saved")
    bar.finish(message=f"{fmt(saved)} product pages saved")
    return saved
