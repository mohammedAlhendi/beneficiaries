"""استيراد ملف الأيتام: كل سطر أسرة فيها يتيم أو أكثر (أعمدة «اسم اليتيم 2»...) يُفكَّك إلى سجل لكل يتيم."""
import datetime as dt
import re
from pathlib import Path

from django.conf import settings
from django.db import transaction

from .importer import _read_sheets
from .models import Orphan, OrphanFamily, OrphanImport
from .textutils import clean_id, clean_text, is_blank, is_valid_id, normalize_arabic, parse_date

SLOT_PATTERNS = {
    "name": r"اسم اليتيم",
    "id": r"رقم هويه اليتيم",
    "birth": r"تاريخ ميلاد اليتيم",
    "gender": r"الجنس",
    "type": r"حاله اليتم",
    "education": r"(?:الحاله التعليميه|المرحله الدراسيه|المرحله التعليميه)",
    "health": r"الحاله الصحيه",
}
FAMILY_EXACT = {
    "governorate": ["المحافظه"],
    "area": ["المنطقه"],
    "mother_name": ["اسم الوصي", "اسم الام"],
    "mother_id": ["رقم هويه الام"],
    "role": ["صفه الوصي"],
    "g_name": ["اسم الوصي المختلف عن الام"],
    "g_id": ["رقم هويه الوصي"],
}


def _norm(v):
    return re.sub(r"\s+", " ", normalize_arabic(v)).strip()


def _find_header(raw):
    for i in range(min(len(raw), 15)):
        if any(_norm(c) == "اسم اليتيم" or _norm(c).startswith("اسم اليتيم") for c in raw.iloc[i].tolist() if not is_blank(c)):
            return i
    return None


def _map_columns(columns):
    """يعيد (family_cols, slots) حيث slots: {رقم الخانة: {حقل: اسم العمود}}."""
    fam, slots = {}, {}
    for col in columns:
        n = _norm(col)
        for field, pattern in SLOT_PATTERNS.items():
            m = re.fullmatch(rf"{pattern}\s*(\d*)", n)
            if m:
                slots.setdefault(m.group(1) or "1", {})[field] = col
        for field, names in FAMILY_EXACT.items():
            if n in [_norm(x) for x in names] and field not in fam:
                fam[field] = col
        if "phone" not in fam and ("جوال" in n or "واتس" in n or "هاتف" in n):
            fam["phone"] = col
    return fam, slots


def _gender(v):
    s = _norm(v)
    if not s:
        return ""
    if "ذكر" in s:
        return "ذكر"
    if "انث" in s:
        return "أنثى"
    return ""


def _phone(v):
    s = clean_id(v)
    return re.sub(r"\D", "", s) if s else ""


def parse_file(path, id_lengths=None):
    """يقرأ الملف وينقّيه دون حفظ. يعيد قاموساً: orphans, families, rejected, warnings, duplicates, total_rows."""
    id_lengths = id_lengths or settings.NATIONAL_ID_LENGTHS
    path = Path(path)
    families, orphans, rejected, warnings, duplicates = {}, [], [], [], []
    seen_ids, seen_names = set(), set()
    total_rows = 0
    found_any = False
    for sheet, raw in _read_sheets(path).items():
        raw = raw.dropna(how="all").dropna(axis=1, how="all").reset_index()
        raw = raw.rename(columns={"index": "__row__"})
        body = raw.drop(columns="__row__")
        pos = _find_header(body)
        if pos is None:
            continue
        found_any = True
        header = [clean_text(c) or f"عمود {j}" for j, c in enumerate(body.iloc[pos].tolist())]
        fam_cols, slots = _map_columns(header)
        if "name" not in slots.get("1", {}):
            continue
        data = body.iloc[pos + 1:].copy()
        data.columns = header
        rows_idx = raw["__row__"].iloc[pos + 1:].tolist()
        for rec, rownum in zip(data.to_dict("records"), rows_idx):
            row_no = int(rownum) + 1
            g = lambda f: rec.get(fam_cols.get(f)) if fam_cols.get(f) else None
            if all(is_blank(v) for v in rec.values()):
                continue
            total_rows += 1
            mother_name, g_name = clean_text(g("mother_name")), clean_text(g("g_name"))
            mother_id = clean_id(g("mother_id"))
            if mother_id and not is_valid_id(mother_id, id_lengths):
                warnings.append({"row": row_no, "what": f"هوية الأم غير صالحة ({mother_id}) — تُركت فارغة"})
                mother_id = ""
            g_id = clean_id(g("g_id"))
            if g_id and not is_valid_id(g_id, id_lengths):
                warnings.append({"row": row_no, "what": f"هوية الوصي غير صالحة ({g_id}) — تُركت فارغة"})
                g_id = ""
            if g_name:
                guardian_name, guardian_id = g_name, g_id
            else:
                guardian_name, guardian_id = mother_name, mother_id
            fam = {
                "governorate": clean_text(g("governorate")), "area": clean_text(g("area")),
                "mother_name": mother_name, "mother_national_id": mother_id,
                "guardian_name": guardian_name, "guardian_national_id": guardian_id,
                "guardian_role": clean_text(g("role")), "phone": _phone(g("phone")),
            }
            key = ("m", mother_id) if mother_id else ("r", row_no)
            if key in families:
                for k, v in fam.items():
                    if v and not families[key][k]:
                        families[key][k] = v
            else:
                families[key] = fam

            for idx in sorted(slots, key=int):
                cols = slots[idx]
                v = {f: rec.get(c) for f, c in cols.items()}
                if all(is_blank(x) for x in v.values()):
                    continue
                name = clean_text(v.get("name"))
                label = f"{name or '(بلا اسم)'}"
                if not name:
                    rejected.append({"row": row_no, "name": "", "reason": f"اسم اليتيم رقم {idx} ناقص"})
                    continue
                notes = []
                nid = clean_id(v.get("id"))
                if nid and not is_valid_id(nid, id_lengths):
                    notes.append(f"هوية غير صالحة في الملف: {nid}")
                    warnings.append({"row": row_no, "what": f"{label}: هوية غير صالحة ({nid}) — حُفظ اليتيم بلا هوية"})
                    nid = ""
                birth = None
                if not is_blank(v.get("birth")):
                    try:
                        birth = parse_date(v.get("birth"))
                    except ValueError:
                        notes.append(f"تاريخ ميلاد غير مفهوم: {clean_text(v.get('birth'))}")
                        warnings.append({"row": row_no, "what": f"{label}: تاريخ الميلاد غير صالح — تُرك فارغاً"})
                    if birth and birth > dt.date.today():
                        warnings.append({"row": row_no, "what": f"{label}: تاريخ ميلاد في المستقبل — تُرك فارغاً"})
                        birth = None
                nkey = (normalize_arabic(name), birth)
                if nid:
                    if nid in seen_ids:
                        duplicates.append({"row": row_no, "name": name, "reason": f"هوية مكررة في الملف ({nid})"})
                        continue
                    seen_ids.add(nid)
                else:
                    if nkey in seen_names:
                        duplicates.append({"row": row_no, "name": name, "reason": "نفس الاسم وتاريخ الميلاد مكرر"})
                        continue
                    seen_names.add(nkey)
                otype = ""
                t = _norm(v.get("type"))
                if t:
                    otype = "both" if "ابوي" in t else ("mother" if "الام" in t else ("father" if "الاب" in t else ""))
                orphans.append({
                    "row": row_no, "family_key": key, "full_name": name, "national_id": nid,
                    "birth_date": birth, "gender": _gender(v.get("gender")), "orphan_type": otype,
                    "education_status": clean_text(v.get("education")), "health_status": clean_text(v.get("health")),
                    "notes": "؛ ".join(notes),
                })
    if not found_any:
        raise ValueError("لم يُعثر على عمود «اسم اليتيم» في الملف. تأكد من الترويسة.")
    return {
        "orphans": orphans, "families": families, "rejected": rejected, "warnings": warnings,
        "duplicates": duplicates, "total_rows": total_rows,
    }


def preview(parsed):
    ids = [o["national_id"] for o in parsed["orphans"] if o["national_id"]]
    existing = set(Orphan.objects.filter(national_id__in=ids).values_list("national_id", flat=True)) if ids else set()
    return {
        "rows": parsed["total_rows"], "orphans": len(parsed["orphans"]), "families": len(parsed["families"]),
        "no_id": sum(1 for o in parsed["orphans"] if not o["national_id"]),
        "no_birth": sum(1 for o in parsed["orphans"] if not o["birth_date"]),
        "existing": len(existing), "duplicates": len(parsed["duplicates"]),
        "rejected": len(parsed["rejected"]), "warnings": len(parsed["warnings"]),
        "new": len(parsed["orphans"]) - len(existing),
    }


@transaction.atomic
def commit(imp: OrphanImport, parsed, user=None):
    from .services import log

    ids = [o["national_id"] for o in parsed["orphans"] if o["national_id"]]
    existing = set(Orphan.objects.filter(national_id__in=ids).values_list("national_id", flat=True)) if ids else set()
    fam_cache, created_fams = {}, 0
    created = skipped = 0
    for o in parsed["orphans"]:
        if o["national_id"] and o["national_id"] in existing:
            skipped += 1
            continue
        key = o["family_key"]
        fam = fam_cache.get(key)
        if fam is None:
            data = parsed["families"][key]
            fam = OrphanFamily.objects.filter(mother_national_id=data["mother_national_id"]).first() if data["mother_national_id"] else None
            if fam is None:
                fam = OrphanFamily.objects.create(import_batch=imp, **data)
                created_fams += 1
            fam_cache[key] = fam
        Orphan.objects.create(
            family=fam, import_batch=imp, source_row=o["row"],
            **{k: o[k] for k in ("full_name", "national_id", "birth_date", "gender", "orphan_type",
                                  "education_status", "health_status", "notes")},
        )
        created += 1
    imp.status = OrphanImport.CONFIRMED
    imp.summary = {**preview(parsed), "created": created, "skipped_existing": skipped, "families_created": created_fams}
    imp.save()
    log(user, "orphans_import", imp.file_name, imp.summary)
    return imp.summary


@transaction.atomic
def cancel(imp: OrphanImport, user=None):
    """يحذف أيتام هذه الرفعة غير المكفولين وأسرها الفارغة (المكفول يبقى ويُذكر)."""
    from .services import log

    kept = 0
    for o in imp.orphans.all():
        if o.sponsorships.exists():
            kept += 1
        else:
            o.delete()
    OrphanFamily.objects.filter(import_batch=imp, orphans__isnull=True).delete()
    imp.status = OrphanImport.CANCELLED
    imp.save()
    log(user, "orphans_import_cancel", imp.file_name, f"باقٍ لوجود كفالة: {kept}")
    return kept
