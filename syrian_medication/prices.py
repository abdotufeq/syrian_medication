"""Medication prices from official MoH bulletins and unofficial Telegram price posts.

Two sources feed the ``medication_prices`` table:

* **Official** bulletins (MoH price-change lists, نشرة / تعديل أسعار) dropped by the
  user into ``data/price_sources/official/`` as PDF, XLSX or CSV. Files placed in
  ``data/price_sources/unofficial/`` are imported the same way but flagged unofficial.
* **Unofficial** price posts from public Telegram channels (``t.me/s/<channel>``),
  e.g. ``#تعديل_اسعار #ألفا  بيرفكت بون 20000 ل.س``.

Every price keeps its raw value and currency. Syria removed two zeros from the lira
on 2026-01-01 (100 old SYP = 1 new SYP), so prices dated before that are stored as
``SYP_OLD`` and ``price_syp`` always holds the value in new lira.
"""

from __future__ import annotations

import difflib
import logging
import re
import sqlite3
import time
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

from .constants import NAME_STOPWORDS, ProductRecord
from .formatter import ProgressBar, bold, dim, fmt, green, section, yellow
from .text_utils import name_score, normalize_name, strength_agrees


logger = logging.getLogger(__name__)

REDENOMINATION_DATE = date(2026, 1, 1)
REDENOMINATION_FACTOR = 100
DEFAULT_TELEGRAM_CHANNELS = ("SD_UpToDate",)
PRICE_FILE_SUFFIXES = {".pdf", ".xlsx", ".xls", ".csv"}
OFFICIAL_MATCH_THRESHOLD = 70
TELEGRAM_MATCH_THRESHOLD = 0.92


# ── schema ──────────────────────────────────────────────────────────────────

def ensure_price_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS price_sources (
            id INTEGER PRIMARY KEY,
            kind TEXT,                 -- bulletin_file | telegram
            official INTEGER,          -- 1 = Ministry of Health bulletin, 0 = unofficial
            name TEXT,
            url TEXT UNIQUE,           -- file:// path or https://t.me/<channel>/<post>
            published_on TEXT,         -- ISO date the prices take effect / were posted
            imported_at TEXT,
            row_count INTEGER DEFAULT 0,
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS medication_prices (
            id INTEGER PRIMARY KEY,
            source_id INTEGER REFERENCES price_sources(id) ON DELETE CASCADE,
            moh_id INTEGER,            -- moh_medications.id when matched
            raw_name TEXT,
            raw_manufacturer TEXT,
            raw_composition TEXT,
            raw_strength TEXT,
            raw_pack TEXT,
            price REAL,                -- as published
            currency TEXT,             -- SYP_OLD (before 2026-01-01) | SYP (new lira)
            price_syp REAL,            -- in new lira
            effective_date TEXT,
            official INTEGER,
            match_score REAL,
            match_method TEXT,         -- name+maker | name | skeleton+maker | ''
            raw_line TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_prices_moh ON medication_prices(moh_id);
        CREATE INDEX IF NOT EXISTS idx_prices_source ON medication_prices(source_id);
        CREATE TABLE IF NOT EXISTS telegram_posts (
            id INTEGER PRIMARY KEY,
            channel TEXT,
            post_id INTEGER,
            posted_at TEXT,
            text TEXT,
            photo_count INTEGER,
            document_name TEXT,
            hashtags TEXT,
            price_lines INTEGER,       -- lines parsed as prices (0 + photos = image-only list)
            UNIQUE (channel, post_id)
        );
        """
    )
    connection.commit()


# ── text helpers ────────────────────────────────────────────────────────────

_EASTERN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_ARABIC_NORMALIZE = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ـ": None})
_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670]")


def clean_text(value) -> str:
    """NFKC (folds PDF presentation forms), Western digits, collapsed whitespace."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = unicodedata.normalize("NFKC", str(value)).translate(_EASTERN_DIGITS)
    text = text.replace("٫", ".").replace("٬", ",").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_arabic(value: str) -> str:
    text = _ARABIC_DIACRITICS.sub("", clean_text(value)).translate(_ARABIC_NORMALIZE)
    return re.sub(r"\s+", " ", text).strip()


MAKER_NOISE = {
    "شركه", "شركة", "معمل", "معامل", "مختبرات", "مخابر", "للصناعات", "الصناعات", "صناعات", "الدوائيه",
    "الدوائية", "الدوائي", "دوائيه", "الطبيه", "الطبية", "المحدوده", "المحدودة", "المسؤوليه", "المسؤولية",
    "محدوده", "محدودة", "مسؤوليه", "ذ", "م", "م.", "و", "فارما", "pharma", "pharmaceutical",
    "pharmaceuticals", "industries", "labs", "co", "ltd",
}


def maker_tokens(value: str) -> set[str]:
    text = normalize_arabic(value).lower().replace("_", " ")
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = set()
    for token in text.split():
        if token in MAKER_NOISE or len(token) < 2:
            continue
        tokens.add(token[2:] if token.startswith("ال") and len(token) > 4 else token)
    return tokens


def parse_price(value) -> float | None:
    text = clean_text(value)
    text = re.sub(r"(ل\.?\s*س\.?|ليره|ليرة|syp|s\.p)", "", text, flags=re.IGNORECASE).strip()
    match = re.search(r"\d[\d,\s]*(?:\.\d+)?", text)
    if not match:
        return None
    number = re.sub(r"[,\s]", "", match.group(0))
    try:
        price = float(number)
    except ValueError:
        return None
    return price if price > 0 else None


def currency_for(effective: str | None, text: str = "") -> tuple[str, float]:
    """Return (currency, factor to new lira)."""
    if re.search(r"(الجديد|جديدة|new)", text or "", re.IGNORECASE):
        return "SYP", 1.0
    if effective:
        try:
            if date.fromisoformat(effective[:10]) >= REDENOMINATION_DATE:
                return "SYP", 1.0
        except ValueError:
            pass
    return "SYP_OLD", 1.0 / REDENOMINATION_FACTOR


def date_from_name(name: str) -> str | None:
    text = clean_text(name)
    for pattern, order in (
        (r"(20\d{2})[-_. /](\d{1,2})[-_. /](\d{1,2})", "ymd"),
        (r"(\d{1,2})[-_. /](\d{1,2})[-_. /](20\d{2})", "dmy"),
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        a, b, c = (int(part) for part in match.groups())
        year, month, day = (a, b, c) if order == "ymd" else (c, b, a)
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return None


# ── Arabic ↔ Latin name skeletons (for Arabic Telegram names vs English MoH names) ──

_AR_SKELETON = {
    "ب": "B", "ت": "T", "ط": "T", "ث": "S", "س": "S", "ص": "S", "ش": "S", "ز": "S", "ذ": "S", "ظ": "S",
    "ج": "G", "غ": "G", "ك": "K", "ق": "K", "خ": "K", "د": "D", "ض": "D", "ف": "F", "ل": "L",
    "م": "M", "ن": "N", "ر": "R",
}
_LATIN_SKELETON = {
    "B": "B", "P": "B", "T": "T", "D": "D", "S": "S", "Z": "S", "G": "G", "J": "G", "K": "K",
    "Q": "K", "F": "F", "V": "F", "L": "L", "M": "M", "N": "N", "R": "R",
}
ARABIC_FORM_WORDS = {
    "بخاخ", "شراب", "معلق", "كريم", "مرهم", "جل", "نقط", "قطره", "قطرة", "حبوب", "مضغوط", "مضغوطات", "اقراص",
    "كبسول", "كبسولات", "امبول", "امبولات", "حقن", "فموي", "فمويه", "فموية", "عيار", "اطفال", "كبار", "مل",
    "ملغ", "غ", "غرام", "عبوه", "عبوة", "سكاكر", "شامبو", "لوشن", "مستحلب", "فوار", "اكياس", "ظرف", "تحاميل",
    "عموم", "العموم", "سعر", "ل", "س", "فورت", "بلس", "اكسترا", "انفي", "انفيه", "عيني", "عينيه", "مهبلي",
    "حبه", "حب", "قرص", "كبسوله", "مضغوطه", "محلول", "ميني", "نيو",
}
ENGLISH_FORM_WORDS = NAME_STOPWORDS | {
    "CREAM", "OINT", "OINTMENT", "GEL", "NASAL", "SPRAY", "DROPS", "DROP", "SOLU", "EMULSION", "LOTION", "SHAMPOO",
    "SUSP", "SUSPENSION", "VIAL", "VIALS", "ORAL", "SOFT", "EFF", "EFFERVESCENT", "SACHETS", "SUPP", "EYE", "EAR",
    "FORTE", "PLUS", "EXTRA", "NEW", "KIDS", "MINI", "POWDER", "CHEWABLE", "SR", "XR", "ER", "DS",
}
# Generic first words: identical skeletons here say nothing about the product.
GENERIC_SKELETONS = {
    "FTMN", "KLSM", "MGNSM", "SNK", "RN", "MG", "FLK", "SD", "SDM", "BTSM", "KLSTRL", "MLTFTMN", "BRBTK", "GNSNG",
}


def skeleton(text: str) -> str:
    """Consonant skeleton that is comparable across Arabic and Latin spellings."""
    out: list[str] = []
    upper = clean_text(text).upper()
    upper = upper.replace("PH", "F").replace("CH", "K").replace("X", "KS")
    upper = re.sub(r"C(?=[EIY])", "S", upper).replace("C", "K")
    for char in upper:
        letter = _AR_SKELETON.get(char) or _LATIN_SKELETON.get(char)
        if letter and (not out or out[-1] != letter):
            out.append(letter)
    return "".join(out)


def arabic_name_core(name: str) -> str:
    words = [word for word in normalize_arabic(name).split() if word not in ARABIC_FORM_WORDS]
    return " ".join(word for word in words if not re.fullmatch(r"[\d.,%+/\-]+", word))


def english_name_core(name: str) -> str:
    words = [word for word in normalize_name(name).split() if word not in ENGLISH_FORM_WORDS and not word.isdigit()]
    return " ".join(words)


def skeleton_score(arabic: str, english: str) -> float:
    a_words, e_words = arabic_name_core(arabic).split(), english_name_core(english).split()
    if not a_words or not e_words:
        return 0.0
    first_a, first_e = skeleton(a_words[0]), skeleton(e_words[0])
    if len(first_a) < 3 or len(first_e) < 3:
        return 0.0
    if first_a == first_e and first_a not in GENERIC_SKELETONS:
        second_a = skeleton(a_words[1]) if len(a_words) > 1 else ""
        second_e = skeleton(e_words[1]) if len(e_words) > 1 else ""
        if second_a and second_e and second_a != second_e:
            return 0.9  # فوكس كيدز ≠ FIX D3: same first word, different product
        return 1.0
    # Near misses on the first word alone are too loose (BRMTRN permethrin ≈ BRMTN Brimoton),
    # so a non-identical name must be close over its whole length.
    return difflib.SequenceMatcher(None, skeleton(" ".join(a_words)), skeleton(" ".join(e_words))).ratio()


# ── matching against the MoH registry ───────────────────────────────────────

FORM_CLASSES = {
    "tablet": r"(?<![A-Z])TABS?(?![A-Z])|TABLET|(?<![A-Z])CTD(?![A-Z])|حب|حبه|حبات|اقراص|قرص|مضغوط",
    "capsule": r"(?<![A-Z])CAPS?(?![A-Z])|CAPSUL|كبسول",
    "liquid": r"SYR|SYRUP|SUSP|شراب|معلق|ORAL SOL",
    "drops": r"DROP|نقط|قطره|قطرة",
    "injection": r"(?<![A-Z])AMP|VIAL|INJ|امبول|أمبول|ابر|حقن|فيال",
    "topical": r"CREAM|OINT|\bGEL\b|LOTION|كريم|مرهم|\bجل\b|لوشن",
    "spray": r"SPRAY|بخاخ",
    "suppository": r"SUPP|تحاميل|تحميله",
}


def form_classes(text: str) -> set[str]:
    upper = normalize_arabic(text).upper()
    return {name for name, pattern in FORM_CLASSES.items() if re.search(pattern, upper)}


def _decimal_numbers(text: str) -> set[str]:
    return {number.rstrip("0").rstrip(".") if "." in number else number
            for number in re.findall(r"\d+(?:\.\d+)?", clean_text(text))}

class RegistryMatcher:
    def __init__(self, connection: sqlite3.Connection):
        self.rows = [dict(row) for row in connection.execute(
            "SELECT id, trade_name, manufacturer_moh, manufacturer, composition, strength, pack FROM moh_medications"
        )]
        makers = {row["manufacturer_moh"]: row["manufacturer"] for row in self.rows}
        self.maker_tokens = {
            moh: maker_tokens(moh) | maker_tokens(english or "") for moh, english in makers.items()
        }
        self.maker_skeletons = {
            moh: {skeleton(token) for token in tokens if len(skeleton(token)) >= 3}
            for moh, tokens in self.maker_tokens.items()
        }
        self.by_maker: dict[str, list[dict]] = {}
        self.by_name: dict[str, list[dict]] = {}
        for row in self.rows:
            self.by_maker.setdefault(row["manufacturer_moh"], []).append(row)
            self.by_name.setdefault(normalize_name(row["trade_name"]), []).append(row)

    def makers_for(self, raw: str) -> list[str]:
        """MoH manufacturer names matching a free-text maker (Arabic, English or a hashtag)."""
        if not raw:
            return []
        wanted = maker_tokens(raw) - {"ابن", "بن", "ibn", "al"}
        if not wanted:
            return []
        hits = []
        for moh, tokens in self.maker_tokens.items():
            # every distinctive word must be there (ابن رشد ≠ ابن سينا), by spelling or by skeleton
            if all(token in tokens or (len(skeleton(token)) >= 3 and skeleton(token) in self.maker_skeletons[moh])
                   for token in wanted):
                hits.append(moh)
        return hits

    @staticmethod
    def _numbers_agree(text: str, row: dict) -> bool:
        percent = re.compile(r"(\d+(?:\.\d+)?)\s*%")
        wanted = set(percent.findall(text))
        offered = set(percent.findall(" ".join(filter(None, (row["trade_name"], row["strength"])))))
        if wanted and offered and wanted.isdisjoint(offered):
            return False  # مينوكسيدين 5% is not MINOXIDINE 2 %
        # Strength: a number with a unit (50 ملغ) must be the row's strength; every other number
        # must be the strength, the pack count or the pack volume (CORVOS 320/10 is not 320/5).
        strength = _decimal_numbers(" ".join(filter(None, (row["trade_name"], row["strength"]))))
        pack = _decimal_numbers(row["pack"] or "")
        dosed = {_decimal_numbers(found).pop() for found in re.findall(
            r"(\d+(?:\.\d+)?)\s*(?:MG|MCG|ملغ|مغ|ميكرو|IU)", clean_text(text), flags=re.IGNORECASE)}
        if dosed and strength and not dosed <= strength:
            return False  # نيتروفان 50 ملغ is not NITROFAN-100 even in a pack of 50
        stated = _decimal_numbers(text)
        if stated and (strength or pack) and not stated <= (strength | pack):
            return False  # Cefadroxil 500 is not CEFADROXIL-250
        forms_text = form_classes(text)
        forms_row = form_classes(" ".join(filter(None, (row["trade_name"], row["pack"]))))
        if forms_text and forms_row and forms_text.isdisjoint(forms_row):
            return False  # a syrup price is not the capsule's
        return strength_agrees(text, ProductRecord(
            trade_name=row["trade_name"], composition=row["composition"], strength=row["strength"], pack=row["pack"],
        ))

    @staticmethod
    def _pick_pack(best: dict, best_score: float, scored: list[tuple[float, dict]], text: str) -> tuple[dict, str]:
        """Same product in several packs: pick the one whose pack count appears in the price text."""
        tied = [row for score, row in scored if score >= best_score - 1e-9]
        if len({row["id"] for row in tied}) <= 1:
            return best, ""
        numbers = set(re.findall(r"\d+", text))
        packed = [row for row in tied if numbers & set(re.findall(r"/\s*(\d+)\s*/", row["pack"] or ""))]
        if len(packed) == 1:
            return packed[0], ""
        return best, " (pack?)"

    def match_official(self, name: str, maker: str, composition: str, strength: str,
                       pack: str = "") -> tuple[int | None, float, str]:
        probe = " ".join(part for part in (name, strength) if part)
        makers = self.makers_for(maker)
        candidates = [row for moh in makers for row in self.by_maker[moh]] if makers else []
        method = "name+maker"
        if not candidates:
            candidates = self.by_name.get(normalize_name(name), [])
            method = "name"
        best, best_score, scored = None, 0.0, []
        for row in candidates:
            score = float(name_score(name, row["trade_name"], row["manufacturer"]))
            if score and not self._numbers_agree(probe, row):
                score = 0
            if score and composition and row["composition"]:
                if normalize_name(composition).split()[:1] == normalize_name(row["composition"]).split()[:1]:
                    score += 5
            scored.append((score, row))
            if score > best_score:
                best, best_score = row, score
        if best is None or best_score < OFFICIAL_MATCH_THRESHOLD:
            return None, best_score, ""
        if method == "name" and len({row["manufacturer_moh"] for row in candidates}) > 1:
            return None, best_score, ""  # same name at several makers: ambiguous without a maker
        best, note = self._pick_pack(best, best_score, scored, f"{pack} {name}")
        return int(best["id"]), min(best_score, 100.0), method + note

    def match_arabic(self, name: str, maker_hints: list[str]) -> tuple[int | None, float, str]:
        makers = [moh for hint in maker_hints for moh in self.makers_for(hint)]
        if not makers:
            return None, 0.0, ""
        best, best_score, scored = None, 0.0, []
        latin = not re.search(r"[\u0600-\u06FF]", name)
        for moh in dict.fromkeys(makers):
            for row in self.by_maker[moh]:
                if latin:
                    # Latin names keep their vowels, so the token scorer is far more precise than skeletons
                    # score without form words: ATROPINE EYE DROP must not match VOLTAMED EYE DROP
                    score = 1.0 if name_score(english_name_core(name), english_name_core(row["trade_name"]),
                                              row["manufacturer"]) >= 70 else 0.0
                    if not score:  # spelling variants: AMPICLOXAME / AMPICLOXAM, KENZOHAIR / KENZO-HAIR
                        mine = english_name_core(name).replace(" ", "")
                        theirs = english_name_core(row["trade_name"]).replace(" ", "")
                        if len(mine) >= 5 and difflib.SequenceMatcher(None, mine, theirs).ratio() >= 0.9:
                            score = 0.95
                else:
                    # a generic name (بيرمترين) can match the composition; a trade-name match ranks first
                    score = max(skeleton_score(name, row["trade_name"]),
                                0.95 * skeleton_score(name, row["composition"] or ""))
                if score >= TELEGRAM_MATCH_THRESHOLD and not self._numbers_agree(name, row):
                    score = 0.0
                scored.append((score, row))
                if score > best_score:
                    best, best_score = row, score
        if best is None or best_score < TELEGRAM_MATCH_THRESHOLD:
            return None, round(best_score * 100, 1), ""
        best, note = self._pick_pack(best, best_score, scored, name)
        return int(best["id"]), round(best_score * 100, 1), ("name+maker" if latin else "skeleton+maker") + note


# ── storage ─────────────────────────────────────────────────────────────────

def upsert_source(connection, *, kind: str, official: bool, name: str, url: str,
                  published_on: str | None, note: str = "") -> int:
    connection.execute(
        """
        INSERT INTO price_sources (kind, official, name, url, published_on, imported_at, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET kind = excluded.kind, official = excluded.official, name = excluded.name,
            published_on = excluded.published_on, imported_at = excluded.imported_at, note = excluded.note
        """,
        (kind, int(official), name, url, published_on, datetime.now(timezone.utc).isoformat(timespec="seconds"), note),
    )
    source_id = connection.execute("SELECT id FROM price_sources WHERE url = ?", (url,)).fetchone()[0]
    connection.execute("DELETE FROM medication_prices WHERE source_id = ?", (source_id,))
    return int(source_id)


def store_prices(connection, source_id: int, official: bool, effective: str | None, items: list[dict]) -> int:
    rows = []
    for item in items:
        currency, factor = currency_for(effective, item.get("currency_hint", ""))
        rows.append((
            source_id, item.get("moh_id"), item.get("name", ""), item.get("maker", ""), item.get("composition", ""),
            item.get("strength", ""), item.get("pack", ""), item["price"], currency, round(item["price"] * factor, 2),
            effective, int(official), item.get("score", 0), item.get("method", ""), item.get("raw_line", ""),
        ))
    connection.executemany(
        """
        INSERT INTO medication_prices (
            source_id, moh_id, raw_name, raw_manufacturer, raw_composition, raw_strength, raw_pack,
            price, currency, price_syp, effective_date, official, match_score, match_method, raw_line
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    connection.execute("UPDATE price_sources SET row_count = ? WHERE id = ?", (len(rows), source_id))
    return len(rows)


# ── official bulletin files (PDF / XLSX / CSV) ──────────────────────────────

COLUMN_KEYWORDS = {
    "name": ("اسم المستحضر", "الاسم التجاري", "المستحضر", "trade name", "product", "name"),
    "maker": ("اسم المعمل", "المعمل", "الشركه", "الشركة", "المصنع", "manufacturer", "company", "factory"),
    "composition": ("التركيب", "الماده الفعاله", "المادة الفعالة", "composition", "active", "generic"),
    "strength": ("العيار", "عيار", "strength", "dose"),
    "pack": ("العبوه", "العبوة", "التعبئه", "pack"),
    "form": ("الشكل الصيدلاني", "الشكل", "form"),
}


def pick_columns(headers: list[str]) -> dict[str, int]:
    normalized = [normalize_arabic(header).lower() for header in headers]
    mapping: dict[str, int] = {}
    for field, keywords in COLUMN_KEYWORDS.items():
        for keyword in keywords:
            keyword = normalize_arabic(keyword).lower()
            for index, header in enumerate(normalized):
                if keyword in header and index not in mapping.values():
                    mapping.setdefault(field, index)
            if field in mapping:
                break
    # Public (retail) price: prefer a column naming العموم, avoid pharmacist/warehouse prices.
    price_columns = [
        index for index, header in enumerate(normalized)
        if ("سعر" in header or "price" in header or "عموم" in header) and index not in mapping.values()
    ]
    public = [index for index in price_columns if "عموم" in normalized[index] or "public" in normalized[index]]
    other = [index for index in price_columns if not re.search(r"صيدل|مستودع|pharmac|whole|قديم|old", normalized[index])]
    if public or other or price_columns:
        mapping["price"] = (public or other or price_columns)[-1]
    return mapping


def tables_from_file(path: Path) -> list[list[list[str]]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(path, dtype=str, header=None, keep_default_na=False, encoding="utf-8-sig")
        return [frame.values.tolist()]
    if suffix in {".xlsx", ".xls"}:
        sheets = pd.read_excel(path, dtype=str, header=None, sheet_name=None)
        return [sheet.fillna("").values.tolist() for sheet in sheets.values()]
    try:
        import pdfplumber
    except ImportError as error:  # pragma: no cover - dependency is declared in pyproject
        raise SystemExit("pdfplumber is required to read PDF bulletins: uv sync") from error
    tables = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                tables.append([[cell or "" for cell in row] for row in table])
    return tables


def fix_rtl(cell: str) -> str:
    """pdfminer sometimes emits Arabic in visual (reversed) order; undo that per word run."""
    text = clean_text(cell)
    if not re.search(r"[\u0600-\u06FF]", text):
        return text
    words = text.split()
    # A word can end with ة but never start with it, and "ال" never ends one: "ةكرش" is "شركة" reversed.
    if any(word.startswith("ة") or (len(word) > 3 and word.endswith("لا")) for word in words if re.fullmatch(r"[\u0600-\u06FF]+", word)):
        return " ".join(word[::-1] if re.search(r"[\u0600-\u06FF]", word) else word for word in reversed(words))
    return text


def rows_from_tables(tables: list[list[list[str]]]) -> tuple[list[dict], dict[str, int]]:
    """Find the header row in each table (carrying it over to headerless continuation pages)."""
    records: list[dict] = []
    mapping: dict[str, int] = {}
    for table in tables:
        start = 0
        for index, row in enumerate(table[:10]):
            candidate = pick_columns([fix_rtl(cell) for cell in row])
            if "name" in candidate and "price" in candidate:
                mapping, start = candidate, index + 1
                break
        if not mapping:
            continue
        for row in table[start:]:
            cells = [fix_rtl(cell) for cell in row]
            get = lambda field: cells[mapping[field]] if field in mapping and mapping[field] < len(cells) else ""
            price = parse_price(get("price"))
            name = get("name")
            if not name or price is None or not re.search(r"[A-Za-z\u0600-\u06FF]{2}", name):
                continue
            records.append({
                "name": name, "maker": get("maker"), "composition": get("composition"),
                "strength": get("strength"), "pack": " ".join(filter(None, (get("pack"), get("form")))),
                "price": price, "raw_line": " | ".join(cell for cell in cells if cell),
            })
    return records, mapping


def import_price_files(connection: sqlite3.Connection, folder: Path, default_date: str | None = None) -> int:
    ensure_price_schema(connection)
    files = sorted(
        path for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in PRICE_FILE_SUFFIXES
    ) if folder.exists() else []
    section(f"Price bulletins  ({len(files)} files in {folder})")
    if not files:
        print(f"  {yellow('!')}  Put MoH price bulletins in {folder / 'official'} "
              f"(and other lists in {folder / 'unofficial'}), named with their date, e.g. 2023-12-03.pdf")
        return 0
    matcher = RegistryMatcher(connection)
    total = 0
    for path in files:
        official = "unofficial" not in {part.lower() for part in path.relative_to(folder).parts[:-1]}
        effective = date_from_name(path.name) or default_date
        try:
            records, mapping = rows_from_tables(tables_from_file(path))
        except Exception as error:  # a broken file must not stop the others
            print(f"  {yellow('!')}  {path.name}: could not read ({error})")
            continue
        if not records:
            print(f"  {yellow('!')}  {path.name}: no table with a name and a price column found")
            continue
        for record in records:
            record["moh_id"], record["score"], record["method"] = matcher.match_official(
                record["name"], record["maker"], record["composition"], record["strength"], record["pack"],
            )
        source_id = upsert_source(
            connection, kind="bulletin_file", official=official, name=path.name,
            url=path.resolve().as_uri(), published_on=effective,
            note="" if effective else "no date in file name; currency assumed old lira",
        )
        stored = store_prices(connection, source_id, official, effective, records)
        connection.commit()
        matched = sum(1 for record in records if record["moh_id"])
        label = "official" if official else "unofficial"
        print(f"  {green('✓')}  {path.name} ({label}, {effective or 'undated'}): {fmt(stored)} prices, "
              f"{fmt(matched)} matched to MoH rows  {dim('columns: ' + ', '.join(sorted(mapping)))}")
        if not effective:
            print(f"      {yellow('!')}  no date in the file name: add one (e.g. 2023-12-03) so the currency is right")
        total += stored
    return total


# ── Telegram channels ───────────────────────────────────────────────────────

SKIP_LINE_RE = re.compile(r"https?://|t\.me/|انضموا|تابعونا|واتس|whatsapp|telegram|التلغرام", re.IGNORECASE)
PRICE_LINE_RE = re.compile(
    r"^(?P<name>.*?)[\s:\-–=]*(?:سعر\s*)?(?:ال)?(?:عموم|سعر)?\s*[:\-–=]?\s*(?P<price>\d{1,3}(?:,\d{3})+|\d{3,7})"
    r"\s*(?P<unit>ل\.?\s*س\.?|ليره|ليرة|جديده|جديدة)?\s*$"
)
NON_MAKER_TAGS = {
    "تعديل_اسعار", "تعديل_الاسعار", "تعميم", "اسعار", "عاجل", "هام", "تنويه", "المولد_النبوي",
    "اضحى_مبارك", "عيد", "رمضان", "تجميلي", "متممات", "مكملات",
}


def parse_telegram_text(text: str) -> tuple[list[str], list[dict]]:
    """Return (maker hashtags, price lines) of one post."""
    hashtags = [tag.replace("_", " ") for tag in re.findall(r"#([\w\u0600-\u06FF]+)", text)
                if normalize_arabic(tag) not in {normalize_arabic(t) for t in NON_MAKER_TAGS}
                and not re.search(r"اسعار|قائمه|تعديل|تعميم|تتمه", normalize_arabic(tag))]
    items: list[dict] = []
    context = ""
    for raw_line in text.splitlines():
        line = clean_text(raw_line)
        if not line or SKIP_LINE_RE.search(line) or line.startswith("#") or set(line) <= set(".…-_ "):
            continue
        match = PRICE_LINE_RE.match(line)
        if match and line[:match.start("price")].rstrip().endswith(("/", "-", ".")):
            match = None  # "في 8/3/2023": a date, not a price
        if not match:
            if re.search(r"[\u0600-\u06FFA-Za-z]{3}", line) and len(line) < 120:
                # "تعميم ... تم تعديل اسعار سكاكر بيبرو" → keep only the product part after "اسعار"
                context = re.split(r"[اأ]سعار", line)[-1].strip(" :-–")
            continue
        name = match.group("name").strip(" :-–=")
        name = re.sub(r"\s*(ال)?عموم\s*$", "", name).strip()
        price = parse_price(match.group("price"))
        if price is None or price < 100:  # strengths like "500" are caught by the 100 floor only partially
            continue
        letters = re.sub(r"[^\u0600-\u06FFA-Za-z]", "", arabic_name_core(name))
        if len(letters) < 3 or normalize_arabic(name).split()[:1] in (["عيار"], ["اطفال"], ["كبار"]):
            name = f"{context} {name}".strip() if context else name
        if not re.search(r"[\u0600-\u06FFA-Za-z]{2}", name):
            continue
        items.append({"name": name, "price": price, "raw_line": line, "currency_hint": match.group("unit") or ""})
    return hashtags, items


def telegram_page(session: requests.Session, channel: str, before: int | None = None) -> tuple[list[dict], int | None]:
    url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
    response = session.get(url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    posts = []
    for message in soup.select("div.tgme_widget_message[data-post]"):
        try:
            post_id = int(message["data-post"].rsplit("/", 1)[1])
        except (ValueError, IndexError):
            continue
        body = message.select_one(".tgme_widget_message_text")
        for br in body.find_all("br") if body else []:
            br.replace_with("\n")
        stamp = message.select_one("time[datetime]")
        document = message.select_one(".tgme_widget_message_document_title")
        posts.append({
            "post_id": post_id,
            "posted_at": stamp["datetime"] if stamp else None,
            "text": body.get_text() if body else "",
            "photo_count": len(message.select(".tgme_widget_message_photo_wrap")),
            "document_name": document.get_text(strip=True) if document else "",
        })
    more = soup.select_one("a.tme_messages_more[href*='before=']")
    next_before = None
    if more:
        found = re.search(r"before=(\d+)", more["href"])
        next_before = int(found.group(1)) if found else None
    return posts, next_before


def scrape_telegram_prices(connection: sqlite3.Connection, session: requests.Session,
                           channels: list[str] | tuple[str, ...] = DEFAULT_TELEGRAM_CHANNELS,
                           max_pages: int = 0, delay: float = 1.5, full: bool = False) -> int:
    """Read public channel history (newest first) and store every price line found.

    Resumable: posts already stored are skipped, and the walk stops once a page holds
    only known posts, unless ``full`` asks to continue back through the whole history.
    """
    ensure_price_schema(connection)
    matcher = RegistryMatcher(connection)
    total = 0
    for channel in channels:
        section(f"Telegram prices  (t.me/{channel})")
        known = {row[0] for row in connection.execute("SELECT post_id FROM telegram_posts WHERE channel = ?", (channel,))}
        oldest_known = min(known) if known else None
        before: int | None = None
        pages = new_posts = price_count = 0
        bar = ProgressBar(total=max_pages or 0, label="Pages") if max_pages else None
        while True:
            try:
                posts, next_before = telegram_page(session, channel, before)
            except requests.RequestException as error:
                print(f"  {yellow('!')}  t.me/{channel}: {error}")
                break
            pages += 1
            fresh = [post for post in posts if post["post_id"] not in known]
            for post in fresh:
                hashtags, items = parse_telegram_text(post["text"])
                posted = (post["posted_at"] or "")[:10] or None
                connection.execute(
                    """
                    INSERT OR REPLACE INTO telegram_posts
                        (channel, post_id, posted_at, text, photo_count, document_name, hashtags, price_lines)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (channel, post["post_id"], post["posted_at"], post["text"], post["photo_count"],
                     post["document_name"], " ".join(hashtags), len(items)),
                )
                known.add(post["post_id"])
                new_posts += 1
                if not items:
                    continue
                for item in items:
                    item["maker"] = " / ".join(hashtags)
                    item["moh_id"], item["score"], item["method"] = matcher.match_arabic(item["name"], hashtags)
                source_id = upsert_source(
                    connection, kind="telegram", official=False, name=f"t.me/{channel}",
                    url=f"https://t.me/{channel}/{post['post_id']}", published_on=posted,
                )
                price_count += store_prices(connection, source_id, False, posted, items)
            connection.commit()
            if bar:
                bar.update(pages, f"{price_count} prices")
            if not next_before or (max_pages and pages >= max_pages):
                break
            if not fresh and not full:
                if oldest_known and next_before > oldest_known:
                    next_before = oldest_known  # jump over the stretch stored by an earlier run
                elif oldest_known:
                    break
            before = next_before
            time.sleep(delay)
        if bar:
            bar.finish(f"{pages} pages")
        matched = connection.execute(
            """SELECT COUNT(*) FROM medication_prices p JOIN price_sources s ON s.id = p.source_id
               WHERE s.kind = 'telegram' AND s.name = ? AND p.moh_id IS NOT NULL""", (f"t.me/{channel}",),
        ).fetchone()[0]
        print(f"  {green('✓')}  {fmt(new_posts)} new posts, {fmt(price_count)} new prices "
              f"({fmt(matched)} matched to MoH rows in total for this channel)")
        total += price_count
    return total


def rematch_prices(connection: sqlite3.Connection) -> int:
    """Re-run matching for every stored price (after a registry refresh or a matcher change).

    MoH row ids change when the registry is re-scraped, so stored ``moh_id`` values go stale.
    """
    ensure_price_schema(connection)
    matcher = RegistryMatcher(connection)
    rows = connection.execute(
        """SELECT p.id, s.kind, p.raw_name, p.raw_manufacturer, p.raw_composition, p.raw_strength, p.raw_pack
           FROM medication_prices p JOIN price_sources s ON s.id = p.source_id"""
    ).fetchall()
    updates = []
    for row in rows:
        if row["kind"] == "telegram":
            hints = [hint.strip() for hint in (row["raw_manufacturer"] or "").split(" / ") if hint.strip()]
            result = matcher.match_arabic(row["raw_name"], hints)
        else:
            result = matcher.match_official(row["raw_name"], row["raw_manufacturer"], row["raw_composition"],
                                            row["raw_strength"], row["raw_pack"])
        updates.append((*result, row["id"]))
    connection.executemany("UPDATE medication_prices SET moh_id = ?, match_score = ?, match_method = ? WHERE id = ?",
                           updates)
    connection.commit()
    matched = sum(1 for update in updates if update[0])
    print(f"  {green('✓')}  Re-matched {fmt(len(updates))} prices: {fmt(matched)} linked to MoH rows")
    return matched


# ── best price per MoH row (used by export) ────────────────────────────────

def best_prices(connection: sqlite3.Connection) -> pd.DataFrame:
    """Latest official price per moh_id, else the latest unofficial one."""
    ensure_price_schema(connection)
    return pd.read_sql_query(
        """
        SELECT moh_id, price, currency, price_syp, effective_date, official, source_name, source_url FROM (
            SELECT p.moh_id, p.price, p.currency, p.price_syp, p.effective_date, p.official,
                   s.name AS source_name, s.url AS source_url,
                   ROW_NUMBER() OVER (
                       PARTITION BY p.moh_id
                       ORDER BY p.official DESC, COALESCE(p.effective_date, '') DESC, p.id DESC
                   ) AS rank
            FROM medication_prices p JOIN price_sources s ON s.id = p.source_id
            WHERE p.moh_id IS NOT NULL
        ) WHERE rank = 1
        """,
        connection,
    )


def print_price_summary(connection: sqlite3.Connection) -> None:
    ensure_price_schema(connection)
    row = connection.execute(
        """
        SELECT COUNT(*) AS prices, COUNT(DISTINCT moh_id) AS matched,
               COUNT(DISTINCT CASE WHEN official = 1 THEN moh_id END) AS official_matched
        FROM medication_prices
        """
    ).fetchone()
    print(f"  {green('✓')}  Prices stored: {bold(fmt(row[0]))}; MoH rows priced: {fmt(row[1])} "
          f"({fmt(row[2])} from official bulletins)")
