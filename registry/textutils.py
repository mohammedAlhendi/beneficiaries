"""دوال تنظيف النصوص العربية والأرقام والتواريخ (مستقلة عن Django لسهولة الاختبار)."""
import datetime as dt
import math
import numbers
import re

import pandas as pd

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_INVISIBLE = re.compile("[​-‏‪-‮⁦-⁩﻿]")
_TASHKEEL = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭ]")

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%y", "%d-%m-%y")


def is_blank(value) -> bool:
    if value is None or value is pd.NaT:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and not _INVISIBLE.sub("", value).replace("\xa0", " ").strip():
        return True
    return False


def clean_text(value) -> str:
    """نص نظيف: بلا محارف غير مرئية ولا مسافات زائدة."""
    if is_blank(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = _INVISIBLE.sub("", str(value)).replace("\xa0", " ")
    return " ".join(s.split())


def normalize_arabic(value) -> str:
    """صيغة موحدة للبحث والمطابقة: توحيد الهمزات والياء والتاء المربوطة وإزالة التشكيل."""
    s = clean_text(value).translate(_AR_DIGITS)
    s = _TASHKEEL.sub("", s).replace("ـ", "")
    s = re.sub("[أإآٱ]", "ا", s)
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return s.lower()


def clean_id(value) -> str:
    """رقم الهوية كنص: أرقام لاتينية فقط بلا فواصل أو '.0' الناتجة عن إكسل."""
    if is_blank(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = clean_text(value).translate(_AR_DIGITS)
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    return re.sub(r"[\s\-_/]", "", s)


def is_valid_id(national_id: str, lengths=(9,)) -> bool:
    return national_id.isdigit() and len(national_id) in lengths


def parse_date(value):
    """يعيد date، أو None إذا كانت الخانة فارغة، أو يرفع ValueError إن كانت غير صالحة."""
    if is_blank(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        # رقم تسلسلي لتاريخ إكسل
        if 20000 < float(value) < 80000:
            return dt.date(1899, 12, 30) + dt.timedelta(days=int(value))
        raise ValueError(f"تاريخ غير صالح: {value}")
    s = clean_text(value).translate(_AR_DIGITS).split(" ")[0]
    if re.fullmatch(r"\d{5}", s) and 20000 < int(s) < 80000:
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(s))
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"تاريخ غير صالح: {value}")
