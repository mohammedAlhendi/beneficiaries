"""قراءة ملفات إكسل وتنقيتها (بدون الكتابة في قاعدة البيانات).

الخطوات: load_table -> auto_map -> clean_rows.
"""
import datetime as dt
import re
from pathlib import Path

import pandas as pd

from .textutils import clean_id, clean_text, is_blank, is_valid_id, normalize_arabic, parse_date

# الحقول الزوجية تُفحص أولاً في المطابقة حتى لا يُلتقط «رقم هوية الزوجة» كأنه رقم هوية المستفيد
FIELDS = ("wife_national_id", "wife_name", "national_id", "name", "aid_type", "date", "social_status",
          "family_size", "notes")
REQUIRED_FIELDS = ("national_id", "name")                       # كشوف التوزيع
REGISTRY_FIELDS = ("national_id", "name", "wife_name", "wife_national_id", "family_size", "notes", "social_status")
REGISTRY_REQUIRED = ("national_id", "name", "wife_name", "wife_national_id", "family_size")   # القائمة الأساسية (صارم)
FIELD_LABELS = {
    "national_id": "رقم الهوية",
    "name": "اسم المستفيد",
    "aid_type": "نوع المساعدة / الطرد",
    "date": "تاريخ الاستلام",
    "social_status": "الحالة الاجتماعية",
    "wife_name": "اسم الزوجة رباعي",
    "wife_national_id": "رقم هوية الزوجة",
    "family_size": "عدد أفراد الأسرة",
    "notes": "ملاحظات المدير",
}

# أسماء الأعمدة المحتملة لكل حقل (تُقارن بعد التوحيد)
ALIASES = {
    "national_id": ["رقم الهوية", "الهوية", "رقم هوية", "هوية", "رقم الهويه", "رقم البطاقة", "id", "national id", "id number"],
    "name": ["اسم المستفيد", "الاسم", "اسم", "الاسم الرباعي", "الاسم الكامل", "اسم المستفيد الرباعي", "name", "full name"],
    "aid_type": ["نوع المساعدة", "نوع الطرد", "المساعدة", "الطرد", "نوع المساعده", "نوع", "aid type"],
    "date": ["تاريخ الاستلام", "تاريخ التوزيع", "تاريخ الاستفادة", "التاريخ", "تاريخ", "date"],
    "social_status": ["الحالة الاجتماعية", "تفاصيل الحالة الاجتماعية", "الحالة الاجتماعيه", "الحالة", "social status"],
    "wife_name": ["اسم الزوجة", "اسم الزوجة رباعي", "الزوجة", "اسم الزوجه", "اسم الزوجة الرباعي", "wife name"],
    "wife_national_id": ["رقم هوية الزوجة", "هوية الزوجة", "رقم هويه الزوجه", "هوية الزوجه", "wife id"],
    "family_size": ["عدد أفراد الأسرة", "عدد الأفراد", "عدد افراد الاسره", "أفراد الأسرة", "عدد الاسرة", "family size"],
    "notes": ["ملاحظات", "ملاحظات المدير", "ملاحظة", "notes"],
}
# أسماء مركّبة آمنة للمطابقة الجزئية (احتواء)
_CONTAINS_SAFE = {
    "national_id": ["رقم الهوية", "رقم هوية"],
    "name": ["اسم المستفيد"],
    "aid_type": ["نوع المساعدة", "نوع الطرد"],
    "date": ["تاريخ الاستلام", "تاريخ التوزيع"],
    "social_status": ["الحالة الاجتماعية"],
    "wife_name": ["اسم الزوجة"],
    "wife_national_id": ["هوية الزوجة", "رقم هوية الزوجة"],
    "family_size": ["عدد أفراد", "عدد الأفراد"],
    "notes": ["ملاحظات المدير"],
}


def _norm_header(value) -> str:
    return re.sub(r"[^\w\s]", "", normalize_arabic(value)).strip()


_ALIAS_NORM = {f: {_norm_header(a) for a in al} for f, al in ALIASES.items()}
_CONTAINS_NORM = {f: [_norm_header(a) for a in al] for f, al in _CONTAINS_SAFE.items()}


def match_field(cell) -> str | None:
    c = _norm_header(cell)
    if not c:
        return None
    for field in FIELDS:
        if c in _ALIAS_NORM[field]:
            return field
    for field in FIELDS:
        if any(a in c for a in _CONTAINS_NORM[field]):
            return field
    return None


def find_header_row(raw: pd.DataFrame, scan: int = 15) -> int:
    best_idx, best_score = 0, 0
    for i in range(min(scan, len(raw))):
        fields = {match_field(c) for c in raw.iloc[i].tolist()} - {None}
        if len(fields) > best_score:
            best_idx, best_score = i, len(fields)
    return best_idx


def _read_sheets(path: Path) -> dict:
    ext = path.suffix.lower()
    if ext == ".csv":
        for enc in ("utf-8-sig", "cp1256"):
            try:
                return {"CSV": pd.read_csv(path, header=None, dtype=object, encoding=enc)}
            except UnicodeDecodeError:
                continue
        raise ValueError("تعذّر قراءة ملف CSV (الترميز غير مدعوم).")
    if ext in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path, header=None, dtype=object, sheet_name=None)
    raise ValueError("صيغة الملف غير مدعومة. استخدم xlsx أو xls أو csv.")


def load_table(path) -> tuple[pd.DataFrame, list[str]]:
    """يقرأ كل الأوراق ويعيد جدولاً واحداً بأعمدة نصية، مع عمودين مساعدين __row__ و __sheet__."""
    path = Path(path)
    notes: list[str] = []
    frames = []
    for sheet_name, raw in _read_sheets(path).items():
        raw = raw.dropna(how="all").dropna(axis=1, how="all")
        if raw.empty:
            notes.append(f"الورقة «{sheet_name}» فارغة وتم تجاهلها.")
            continue
        header_pos = find_header_row(raw.reset_index(drop=True))
        header_cells = raw.iloc[header_pos].tolist()
        columns, seen = [], {}
        for j, cell in enumerate(header_cells):
            name = clean_text(cell) or f"عمود {j + 1}"
            seen[name] = seen.get(name, 0) + 1
            columns.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
        data = raw.iloc[header_pos + 1:].copy()
        row_numbers = [int(i) + 1 for i in data.index]
        data.columns = columns
        data["__row__"] = row_numbers
        data["__sheet__"] = str(sheet_name)
        frames.append(data)
    if not frames:
        raise ValueError("الملف لا يحتوي بيانات.")
    table = pd.concat(frames, ignore_index=True)
    return table, notes


def table_columns(table: pd.DataFrame) -> list[str]:
    return [c for c in table.columns if not str(c).startswith("__")]


def auto_map(columns) -> dict:
    """يقترح مطابقة بين حقول النظام وأعمدة الملف."""
    mapping = {f: "" for f in FIELDS}
    used = set()
    for field in FIELDS:
        for col in columns:
            if col in used:
                continue
            if match_field(col) == field:
                mapping[field] = col
                used.add(col)
                break
    return mapping


def _is_quad(name: str) -> bool:
    return len(name.split()) >= 4


def clean_rows(table, mapping, default_aid_type="", default_date=None, id_lengths=(9,), today=None,
               kind="distribution", strict_quad=True) -> dict:
    """ينقّي الصفوف ويعيد: total, valid, rejected, duplicates, id_rows.

    kind="registry": قائمة أساسية بالمستفيدين (صارمة): تتطلب الاسم والهوية واسم الزوجة رباعياً وهويتها وعدد الأفراد.
    kind="lookup": ملف مقارنة/استعلام (هوية واسم فقط) بلا إلزام.
    id_rows: لكل رقم هوية صالح الصيغة أرقام الصفوف التي ورد فيها (للكشف عن التكرار داخل الملف).
    """
    registry = kind in ("registry", "lookup")      # lookup: ملف مقارنة (هوية/اسم فقط) دون إلزام حقول الأسرة
    strict = kind == "registry"
    today = today or dt.date.today()
    default_aid_type = clean_text(default_aid_type)
    valid, rejected, duplicates = [], [], []
    seen: dict = {}
    id_rows: dict = {}
    total = 0
    length_txt = " أو ".join(str(n) for n in id_lengths)
    fields = REGISTRY_FIELDS if registry else FIELDS

    for rec in table.to_dict("records"):
        def get(field):
            col = mapping.get(field)
            return rec.get(col) if col else None

        if all(is_blank(get(f)) for f in fields):
            continue  # صف فارغ
        total += 1
        row_no, sheet = rec.get("__row__"), rec.get("__sheet__", "")
        name = clean_text(get("name"))
        nid = clean_id(get("national_id"))
        wife_name = clean_text(get("wife_name"))
        wife_id = clean_id(get("wife_national_id"))
        raw_size = get("family_size")
        notes = clean_text(get("notes"))
        reasons = []

        if not nid:
            reasons.append("رقم الهوية ناقص")
        elif not is_valid_id(nid, id_lengths):
            reasons.append(f"رقم الهوية غير صالح (يجب أن يكون {length_txt} أرقام)")
        if not name:
            reasons.append("الاسم ناقص")

        family_size = None
        if not is_blank(raw_size):
            digits = clean_id(raw_size)
            if digits.isdigit() and int(digits) >= 1:
                family_size = int(digits)
            else:
                reasons.append("عدد أفراد الأسرة غير صالح")
        if wife_id and not is_valid_id(wife_id, id_lengths):
            reasons.append(f"هوية الزوجة غير صالحة (يجب أن تكون {length_txt} أرقام)")
        elif wife_id and nid and wife_id == nid:
            reasons.append("هوية الزوجة مطابقة لهوية المستفيد")
        if wife_name and strict_quad and not _is_quad(wife_name):
            reasons.append("اسم الزوجة ليس رباعياً")
        if strict:
            if not wife_name:
                reasons.append("اسم الزوجة ناقص")
            if not wife_id:
                reasons.append("هوية الزوجة ناقصة")
            if family_size is None and is_blank(raw_size):
                reasons.append("عدد أفراد الأسرة ناقص")

        aid, date = "", None
        if not registry:
            aid = clean_text(get("aid_type")) or default_aid_type
            if not aid:
                reasons.append("نوع المساعدة غير محدد")
            raw_date = get("date")
            if not is_blank(raw_date):
                try:
                    date = parse_date(raw_date)
                except ValueError:
                    reasons.append("تاريخ غير صالح")
            elif default_date:
                date = default_date
            else:
                reasons.append("تاريخ الاستلام غير محدد")
            if date and date > today:
                reasons.append("تاريخ في المستقبل")

        if nid and is_valid_id(nid, id_lengths):
            id_rows.setdefault(nid, []).append(row_no)

        item = {
            "row": row_no, "sheet": sheet, "national_id": nid, "name": name,
            "aid_type": aid, "date": date, "social_status": clean_text(get("social_status")),
            "wife_name": wife_name, "wife_national_id": wife_id, "family_size": family_size, "notes": notes,
        }
        if reasons:
            rejected.append({**item, "reason": "، ".join(reasons)})
            continue
        key = (nid,) if registry else (nid, normalize_arabic(aid), date)
        if key in seen:
            duplicates.append({**item, "reason": f"مكرر مع الصف {seen[key]}"})
            continue
        seen[key] = row_no
        valid.append(item)

    return {"total": total, "valid": valid, "rejected": rejected, "duplicates": duplicates, "id_rows": id_rows}
