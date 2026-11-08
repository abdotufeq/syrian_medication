"""Constants, patterns, and static data for the Syrian medication scraper."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PHOTO_DIR = DATA_DIR / "photos"
DB_PATH = DATA_DIR / "syrian_medications.sqlite"
CSV_PATH = DATA_DIR / "syrian_medications.csv"
MOH_CSV = ROOT / "medications.csv"
MOH_URL = "https://www.moh.gov.sy/drugs"
MOH_ROWS_PER_PAGE = 12
MSF_DB_PATH = DATA_DIR / "msf_unicat.sqlite"
MSF_CSV_PATH = DATA_DIR / "msf_unicat.csv"
MSF_PHOTO_DIR = PHOTO_DIR / "msf-unicat"
REQUEST_TIMEOUT = 12
MAX_LISTING_PAGES = 40
HEALTH_CHECK_TIMEOUT = 10

CARD_SKIP = {
    "product search",
    "drug index a to z",
    "categories",
    "products",
    "products »",
    "sort by",
    "our products",
    "all products",
    "home",
    "contact",
    "contact us",
}

ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]+")
PRICE_RE = re.compile(
    r"(?:price|unit price)\s*[:\-]?\s*([0-9][0-9,.\s]*)\s*(SYP|USD|EUR|\$|£)?",
    re.IGNORECASE,
)
IMAGE_SKIP_RE = re.compile(
    r"logo|icon|sprite|banner|pixel|flag|facebook|instagram|youtube|whatsapp|placeholder|"
    r"/images/bg/|/images/news/|footer|slider|shape|header",
    re.IGNORECASE,
)
TITLE_SKIP = {
    "our products",
    "products",
    "home",
    "get in touch",
    "get in touch!",
    "about us",
    "contact us",
}
LABELS = {
    "chemical composition": "composition",
    "active constituents": "composition",
    "active ingredient": "composition",
    "active ingredients": "composition",
    "composition": "composition",
    "compositions": "composition",
    "strength": "strength",
    "dosage form": "dosage_form",
    "dosage form / route": "dosage_form",
    "pharmaceutical form": "dosage_form",
    "how supplied": "pack",
    "how is supplied": "pack",
    "presentation": "pack",
    "pack": "pack",
    "pack size": "pack",
    "therapeutic category": "category",
    "pharmacologic category": "category",
    "drug category": "category",
    "category": "category",
    "uses": "uses",
    "indications": "uses",
    "indication": "uses",
    "price": "price",
}

DOSAGE_FORMS = {
    "أقراص": "Tablets",
    "أقراص (قرص ضمن قرص)": "Tablet-in-tablet",
    "أقراص تحت اللسان": "Sublingual tablets",
    "أقراص ثنائية الطبقة": "Bilayer tablets",
    "أقراص ثنائية الطبقة مديدة التحرر": "Extended-release bilayer tablets",
    "أقراص فوارة": "Effervescent tablets",
    "أقراص قابلة للإسالة": "Dispersible tablets",
    "أقراص لتحضير معلق فموي": "Tablets for oral suspension",
    "أقراص للمضغ": "Chewable tablets",
    "أقراص للمضغ/قابلة للتبعثر": "Chewable / dispersible tablets",
    "أقراص متأخرة التحرر": "Delayed-release tablets",
    "أقراص متفتتة فموياً": "Orally disintegrating tablets",
    "أقراص مديدة التحرر": "Extended-release tablets",
    "أقراص مص": "Lozenges",
    "أقراص ملبسة": "Coated tablets",
    "أقراص ملبسة معوياً": "Enteric-coated tablets",
    "أقراص مهبلية": "Vaginal tablets",
    "الكسير": "Elixir",
    "بخاخ": "Spray",
    "بخاخ أنفي": "Nasal spray",
    "بخاخ فموي": "Oral spray",
    "بخة محددة": "Metered spray",
    "بودرة /فموي": "Oral powder",
    "بودرة للحقن/فيال": "Powder for injection, vial",
    "بودرة موضعية": "Topical powder",
    "بيوض مهبلية": "Vaginal ovules",
    "تحاميل": "Suppositories",
    "جل": "Gel",
    "جل أنفي": "Nasal gel",
    "جل عيني": "Eye gel",
    "جل فموي": "Oral gel",
    "جل مهبلي": "Vaginal gel",
    "حزمة (أقراص ملبسة+ أقراص ملبسة)": "Pack (coated tablets + coated tablets)",
    "حزمة (أقراص+ أقراص ملبسة)": "Pack (tablets + coated tablets)",
    "حزمة (محافظ+أقراص ملبسة+ أقراص ملبسة)": "Pack (capsules + coated tablets + coated tablets)",
    "رحضة شرجية": "Rectal enema",
    "رذاذ": "Aerosol",
    "سائل تخدير": "Anesthetic liquid",
    "سيرنغ معبأ/للحقن": "Prefilled syringe for injection",
    "سيروم": "Infusion solution",
    "شاش طبي": "Medical gauze",
    "شامبو": "Shampoo",
    "شراب": "Syrup",
    "شراب وحيد الجرعة": "Single-dose syrup",
    "ظروف": "Sachets",
    "ظروف / شراب": "Sachets for syrup",
    "علكة": "Chewing gum",
    "غسول فموي": "Mouthwash",
    "فيال فموي": "Oral vial",
    "قطرة أذن": "Ear drops",
    "قطرة انف": "Nasal drops",
    "قطرة عينية": "Eye drops",
    "قطرة عينية /أذنية": "Eye / ear drops",
    "قطرة عينية \\جرعة وحيدة": "Single-dose eye drops",
    "قطرة عينية انفيةأذنية": "Eye, nasal, and ear drops",
    "كريم": "Cream",
    "كريم شرجي": "Rectal cream",
    "كريم مهبلي": "Vaginal cream",
    "ماء محل": "Solvent water",
    "ماء معد للارواء": "Irrigation water",
    "محافظ": "Capsules",
    "محافظ جيلاتينية طرية": "Soft gelatin capsules",
    "محافظ للاستنشاق": "Capsules for inhalation",
    "محافظ متأخرة التحرر": "Delayed-release capsules",
    "محافظ مديدة التحرر": "Extended-release capsules",
    "محلول": "Solution",
    "محلول أظافر": "Nail solution",
    "محلول تسريب وريدي/ عبوة او كيس": "Intravenous infusion, bottle or bag",
    "محلول عيني": "Ophthalmic solution",
    "محلول للاستنشاق": "Solution for inhalation",
    "محلول للاستنشاق/حبابات": "Solution for inhalation, ampoules",
    "محلول للحقن/حبابات": "Solution for injection, ampoules",
    "محلول للحقن/فيال": "Solution for injection, vial",
    "محلول للحقن/كيس سيروم(P.V.C.)": "Solution for injection, PVC infusion bag",
    "مخدر سني/ خرطوش": "Dental anesthetic cartridge",
    "مرهم": "Ointment",
    "مرهم شرجي": "Rectal ointment",
    "مرهم عيني\\غرام": "Eye ointment, per gram",
    "مرهم مهبلي": "Vaginal ointment",
    "مستحلب جلدي": "Cutaneous emulsion",
    "معجون": "Paste",
    "معجون فموي": "Oral paste",
    "معلق جاف": "Dry suspension",
    "معلق فموي": "Oral suspension",
    "معلق للحقن": "Suspension for injection",
    "نقط فموية": "Oral drops",
}

ALPHA_CATEGORIES = {
    "abx": "Antibiotics",
    "antiviral": "Antivirals",
    "antifungal": "Antifungals",
    "cv": "Cardiovascular",
    "lipid": "Lipid management",
    "thromb": "Antithrombotics",
    "dm": "Diabetes",
    "gi": "Gastroenterology",
    "pain": "Pain and inflammation",
    "resp": "Respiratory and ENT",
    "allergy": "Allergy",
    "eye": "Ophthalmology",
    "bone": "Bone and joint",
    "vit": "Vitamins and supplements",
    "uro": "Urology and men's health",
    "cns": "Central nervous system",
    "steroid": "Corticosteroids",
    "derm": "Dermatology",
    "anaes": "Anaesthesia and critical care",
    "contrast": "Diagnostic imaging",
    "weight": "Weight management",
}

NAME_STOPWORDS = {
    "TAB", "TABS", "TABLET", "TABLETS", "CAP", "CAPS", "CAPSULE", "CAPSULES",
    "MG", "ML", "MCG", "G", "IU", "CTD", "F", "SYR", "SYRUP", "AMP", "FOR",
    "OF", "THE", "AND", "HCL", "INJECTION", "INJ", "SOL", "SOLUTION",
}


@dataclasses.dataclass
class ProductRecord:
    """Lightweight container for a manufacturer product row."""
    trade_name: str = ""
    trade_name_ar: str = ""
    composition: str = ""
    composition_ar: str = ""
    strength: str = ""
    strength_ar: str = ""
    dosage_form: str = ""
    dosage_form_ar: str = ""
    pack: str = ""
    pack_ar: str = ""
    category: str = ""
    category_ar: str = ""
    uses: str = ""
    uses_ar: str = ""
    price: str = ""
    price_currency: str = ""
    description: str = ""
    description_ar: str = ""
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)
    source_url: str = ""
    photos: list[str] = dataclasses.field(default_factory=list)
    saved_photos: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    language_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def has_any_name(self) -> bool:
        return bool(self.trade_name or self.trade_name_ar or self.composition or self.composition_ar)
