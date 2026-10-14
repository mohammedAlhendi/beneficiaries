"""مولّد ملفات إكسل تجريبية ببيانات وهمية بالكامل (للتجربة والاختبار)."""
import datetime as dt
import random
from pathlib import Path

from openpyxl import Workbook

FIRST = ["محمد", "أحمد", "محمود", "خالد", "يوسف", "إبراهيم", "عمر", "سامي", "ناصر", "علي", "فاطمة", "مريم", "آمنة", "سعاد", "هدى"]
FATHER = ["عبد الله", "حسن", "سعيد", "مصطفى", "جمال", "رياض", "فؤاد", "طارق"]
FAMILY = ["الهندي", "أبو سمرة", "النجار", "الخطيب", "العطار", "شعت", "المصري", "بكر"]
SOCIAL = ["أرملة تعيل 4 أفراد", "أسرة من 7 أفراد", "معيل وحيد", "مطلقة", "أسرة نازحة", "ذوو إعاقة في الأسرة", ""]


def fake_people(n, seed=1):
    rnd = random.Random(seed)
    ids = rnd.sample(range(100000000, 999999999), n)
    people = [
        {
            "id": str(i),
            "name": f"{rnd.choice(FIRST)} {rnd.choice(FATHER)} {rnd.choice(FAMILY)}",
            "social": rnd.choice(SOCIAL),
        }
        for i in ids
    ]
    wr = random.Random(seed + 1000)                      # مولّد منفصل كي لا تتغير الأسماء والهويات الأصلية
    wife_ids = wr.sample(range(100000000, 999999999), n)
    for p, wid in zip(people, wife_ids):
        p["wife_id"] = str(wid) if str(wid) != p["id"] else str(wid + 1)
        p["wife"] = f"{wr.choice(FIRST[10:])} {wr.choice(FATHER)} {wr.choice(FATHER)} {wr.choice(FAMILY)}"
        p["size"] = wr.randint(2, 11)
    return people


def write_distribution_file(path, people, date=None, aid_type=None, with_dirty_rows=False, with_family=False):
    """ملف كشف بسيط. إن مُرّر date/aid_type يُكتبان كأعمدة داخل الملف؛ وإلا يُترَكان للمستخدم (خارج الملف)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "كشف"
    ws.append(["كشف التوزيع"])           # سطر عنوان قبل الترويسة
    headers = ["م", "اسم المستفيد", "رقم الهوية"]
    if date:
        headers.append("تاريخ الاستلام")
    if aid_type:
        headers.append("نوع المساعدة")
    headers.append("الحالة الاجتماعية")
    if with_family:
        headers += ["اسم الزوجة رباعي", "رقم هوية الزوجة", "عدد أفراد الأسرة", "ملاحظات"]
    ws.append(headers)
    for i, p in enumerate(people, start=1):
        row = [i, p["name"], int(p["id"])]
        if date:
            row.append(date)
        if aid_type:
            row.append(aid_type)
        row.append(p["social"])
        if with_family:
            row += [p["wife"], int(p["wife_id"]), p["size"], p.get("notes", "")]
        ws.append(row)
    if with_dirty_rows and people:
        first = people[0]
        dirty = [None, "", first["id"][:5]]                 # بلا اسم + هوية قصيرة
        ws.append(dirty + [None] * (len(headers) - 3))
        ws.append([None, first["name"], first["id"]] + ([date] if date else []) + ([aid_type] if aid_type else []) + [first["social"]])  # تكرار
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def make_sample_set(folder, today=None):
    """ينشئ ثلاثة ملفات: نقدي (التاريخ والنوع خارج الملف)، وخضار (داخل الملف)، وملف به صفوف رديئة."""
    folder = Path(folder)
    today = today or dt.date.today()
    people = fake_people(60)
    d1 = today - dt.timedelta(days=90)
    d2 = today - dt.timedelta(days=30)
    paths = [
        write_distribution_file(folder / "نقدي-سابق.xlsx", people[:25]),
        write_distribution_file(folder / "طرود-خضار.xlsx", people[10:50], date=d2, aid_type="طرد خضار"),
        write_distribution_file(folder / "نقدي-مع-أخطاء.xlsx", people[40:60], with_dirty_rows=True),
    ]
    return paths, {"nqdi_date": d1, "veg_date": d2}
