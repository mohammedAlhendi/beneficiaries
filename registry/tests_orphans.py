import datetime as dt
import tempfile

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook

from . import orphan_import, orphan_services as osv
from .models import AuditLog, Orphan, OrphanFamily, OrphanImport, Sponsor, Sponsorship
from .roles import ADMIN, ENTRY, SPONSOR

TMP = tempfile.mkdtemp()
TODAY = dt.date.today()
HEAD = ["م.", "اسم اليتيم", "رقم هوية اليتيم", "تاريخ ميلاد اليتيم", "الجنس", "اسم اليتيم 2", "رقم هوية اليتيم 2",
        "تاريخ ميلاد اليتيم 2", "الجنس 2", "المحافظة", "المنطقة", "اسم الوصي", "رقم هوية الام", "صفة الوصي",
        "اسم الوصي المختلف عن الام", "رقم هوية الوصي", "رقم جوال الواتس"]


def make_file(path):
    wb = Workbook()
    ws = wb.active
    ws.append(["كشف الأيتام"])
    ws.append(HEAD)
    ws.append([1, "كرمل إبراهيم علي", 438990137, dt.datetime(2018, 6, 4), "أنثى", "شام إبراهيم علي", 436870018, dt.datetime(2016, 1, 31), "أنثى",
               "دير البلح", "المعسكر", "فاطمة محمد", 400178935, "الأم", None, None, "970597584124"])
    ws.append([2, "قمر يزيد", 442819843, "17/02/2022", "أنثى", None, None, None, None,
               "خانيونس", "المواصي", "نصرة فتحي", 400269676, "الجد", "عوني سليمان", 960790657, 972598573125])
    ws.append([3, "يتيم بلا هوية", None, dt.datetime(2020, 1, 1), "ذكر", "يتيم هوية قصيرة", 12345, "تاريخ خطأ", "ذكر",
               "رفح", None, "ام ثالثة", 400000003, "الأم", None, None, 970590000000])
    ws.append([4, "كرمل إبراهيم علي", 438990137, dt.datetime(2018, 6, 4), "أنثى", "أخ جديد", 441111111, dt.datetime(2019, 5, 5), "ذكر",
               "دير البلح", "المعسكر", "فاطمة محمد", 400178935, "الأم", None, None, "970597584124"])   # تكرار + نفس الأم
    ws.append([5, None, 449999999, dt.datetime(2019, 1, 1), "ذكر", None, None, None, None, "رفح", "x", "ام", 400000005, "الأم", None, None, "1"])
    wb.save(path)
    return path


@override_settings(MEDIA_ROOT=TMP)
class OrphanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for r in (ADMIN, ENTRY, SPONSOR):
            Group.objects.get_or_create(name=r)
        cls.admin = User.objects.create_user("a", password="x")
        cls.admin.groups.add(Group.objects.get(name=ADMIN))
        cls.sp = User.objects.create_user("s", password="x")
        cls.sp.groups.add(Group.objects.get(name=SPONSOR))
        cls.entry = User.objects.create_user("e", password="x")
        cls.entry.groups.add(Group.objects.get(name=ENTRY))

    def setUp(self):
        self.path = make_file(f"{tempfile.mkdtemp()}/orph.xlsx")
        self.client.force_login(self.sp)

    def do_import(self):
        with open(self.path, "rb") as f:
            r = self.client.post(reverse("orphan_import_new"), {"file": SimpleUploadedFile("orph.xlsx", f.read())})
        self.assertEqual(r.status_code, 302)
        imp = OrphanImport.objects.latest("id")
        self.assertEqual(self.client.get(reverse("orphan_import_preview", args=[imp.token])).status_code, 200)
        self.client.post(reverse("orphan_import_confirm", args=[imp.token]))
        imp.refresh_from_db()
        return imp

    def test_parse(self):
        p = orphan_import.parse_file(self.path)
        st = orphan_import.preview(p)
        self.assertEqual(st["rows"], 5)
        self.assertEqual(st["orphans"], 6)          # كرمل، شام، قمر، بلا هوية، هوية قصيرة، أخ جديد (المكرر مستبعد)
        self.assertEqual(len(p["duplicates"]), 1)    # كرمل المكرر
        self.assertEqual(len(p["rejected"]), 1)      # الصف 5 بلا اسم
        self.assertEqual(st["no_id"], 2)             # بلا هوية + هوية قصيرة
        o = {x["full_name"]: x for x in p["orphans"]}
        self.assertEqual(o["قمر يزيد"]["birth_date"], dt.date(2022, 2, 17))
        self.assertIsNone(o["يتيم هوية قصيرة"]["birth_date"])        # تاريخ خطأ
        self.assertIn("هوية غير صالحة", o["يتيم هوية قصيرة"]["notes"])
        fams = list(p["families"].values())
        mom = [f for f in fams if f["mother_national_id"] == "400178935"]
        self.assertEqual(len(mom), 1)                                # الأسرة المكررة ضُمّت
        qa = [f for f in fams if f["mother_national_id"] == "400269676"][0]
        self.assertEqual((qa["guardian_name"], qa["guardian_national_id"], qa["guardian_role"]), ("عوني سليمان", "960790657", "الجد"))
        self.assertEqual(mom[0]["guardian_name"], "فاطمة محمد")      # الوصي = الأم عند غياب الوصي المختلف

    def test_import_flow_and_roles(self):
        imp = self.do_import()
        self.assertEqual(imp.status, OrphanImport.CONFIRMED)
        self.assertEqual(Orphan.objects.count(), 6)
        self.assertEqual(OrphanFamily.objects.count(), 3)
        self.assertEqual(Orphan.objects.filter(family__mother_national_id="400178935").count(), 3)
        # إعادة الرفع لا تكرر أصحاب الهويات
        imp2 = self.do_import()
        self.assertEqual(Orphan.objects.exclude(national_id="").count(), 4)
        self.assertEqual(imp2.summary["skipped_existing"], 4)
        # الأدوار: مدخل البيانات العام لا يرى القسم
        self.client.force_login(self.entry)
        self.assertEqual(self.client.get(reverse("orphan_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("orphan_detail", args=[Orphan.objects.first().pk])).status_code, 403)

    def test_search_filters_and_export(self):
        self.do_import()
        g = lambda **kw: self.client.get(reverse("orphan_list"), kw).context["page"].paginator.count
        self.assertEqual(g(q="كرمل"), 1)
        self.assertEqual(g(q="438990137"), 1)
        self.assertEqual(g(q="فاطمة"), 3)                # بحث باسم الوصي/الأم
        self.assertEqual(g(governorate="دير البلح"), 3)
        self.assertEqual(g(gender="ذكر"), 3)
        self.assertEqual(g(age_from=0, age_to=5), g(age_to=5))
        self.assertGreaterEqual(g(age_from=8, age_to=12), 1)
        self.assertEqual(g(sponsored="no"), 6)
        r = self.client.get(reverse("orphan_export"), {"sponsored": "no"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action="export_orphans").exists())
        o = Orphan.objects.get(national_id="438990137")
        self.assertEqual(self.client.get(reverse("orphan_detail", args=[o.pk])).status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action="orphan_view").exists())

    def test_sponsorship_payments_overdue_stats(self):
        self.do_import()
        o = Orphan.objects.get(national_id="438990137")
        s = Sponsor.objects.create(name="فاعل خير", country="تركيا")
        self.client.post(reverse("sponsor_create"), {"name": "كفيل ثان", "organization": "جمعية"})
        self.assertEqual(Sponsor.objects.count(), 2)
        old = (TODAY - dt.timedelta(days=100)).isoformat()
        r = self.client.post(reverse("sponsorship_create_for_orphan", args=[o.pk]), {
            "sponsor": s.pk, "amount": "30", "currency": "USD", "frequency": "monthly", "start_date": old})
        self.assertEqual(r.status_code, 302)
        sp = Sponsorship.objects.get()
        # كفالة نشطة ثانية لنفس اليتيم ممنوعة
        s2 = Sponsor.objects.exclude(pk=s.pk).get()
        r = self.client.post(reverse("sponsorship_create_for_orphan", args=[o.pk]), {
            "sponsor": s2.pk, "amount": "30", "currency": "USD", "frequency": "monthly", "start_date": old})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Sponsorship.objects.count(), 1)
        # من صفحة الكفيل بهوية اليتيم
        o2 = Orphan.objects.get(national_id="436870018")
        r = self.client.post(reverse("sponsorship_create_for_sponsor", args=[s.pk]), {
            "sponsor": s.pk, "orphan_id": o2.national_id, "amount": "25", "currency": "USD", "frequency": "monthly",
            "start_date": TODAY.isoformat()})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Sponsorship.objects.count(), 2)
        # دفعة قديمة → متأخرة؛ ثم دفعة حديثة → غير متأخرة
        self.client.post(reverse("payment_add", args=[sp.pk]), {"paid_on": old, "amount": "30"})
        self.assertEqual([x["s"].pk for x in osv.overdue_sponsorships()], [sp.pk])
        self.client.post(reverse("payment_add", args=[sp.pk]), {"paid_on": TODAY.isoformat(), "amount": "30"})
        self.assertEqual(osv.overdue_sponsorships(), [])
        for name in ("sponsor_detail",):
            self.assertEqual(self.client.get(reverse(name, args=[s.pk])).status_code, 200)
        st = self.client.get(reverse("orphan_stats")).context["s"]
        self.assertEqual((st["sponsored"], st["unsponsored"]), (2, 4))
        self.assertEqual(self.client.get(reverse("orphan_list"), {"sponsored": "yes"}).context["page"].paginator.count, 2)
        self.assertEqual(self.client.get(reverse("orphan_list"), {"sponsor": s.pk}).context["page"].paginator.count, 2)
        self.assertEqual(self.client.get(reverse("sponsor_export")).status_code, 200)
        # تغيير الحالة ثم كفالة جديدة
        self.client.post(reverse("sponsorship_status", args=[sp.pk]), {"status": "ended"})
        sp.refresh_from_db()
        self.assertEqual(sp.status, "ended")
        r = self.client.post(reverse("sponsorship_create_for_orphan", args=[o.pk]), {
            "sponsor": s2.pk, "amount": "30", "currency": "USD", "frequency": "monthly", "start_date": TODAY.isoformat()})
        self.assertEqual(r.status_code, 302)

    def test_create_edit_orphan_and_cancel_import(self):
        imp = self.do_import()
        r = self.client.post(reverse("orphan_create"), {
            "full_name": "يتيم يدوي", "national_id": "455555555", "birth_date": "2015-03-03", "gender": "ذكر",
            "orphan_type": "father", "governorate": "غزة", "mother_national_id": "400178935", "mother_name": "فاطمة محمد"})
        self.assertEqual(r.status_code, 302)
        manual = Orphan.objects.get(national_id="455555555")
        self.assertEqual(OrphanFamily.objects.count(), 3)                 # رُبط بالأسرة الموجودة
        self.assertEqual(manual.family.orphans.count(), 4)
        r = self.client.post(reverse("orphan_create"), {"full_name": "مكرر", "national_id": "455555555"})
        self.assertEqual(r.status_code, 200)                              # هوية مكررة
        r = self.client.post(reverse("orphan_edit", args=[manual.pk]), {
            "full_name": "يتيم يدوي معدل", "national_id": "455555555", "governorate": "غزة", "mother_national_id": "400178935",
            "phone": "970599000000"})
        self.assertEqual(r.status_code, 302)
        manual.refresh_from_db()
        self.assertEqual(manual.full_name, "يتيم يدوي معدل")
        # مكفول يبقى عند التراجع
        o = Orphan.objects.get(national_id="438990137")
        s = Sponsor.objects.create(name="ك")
        osv.create_sponsorship(s, o, start_date=TODAY)
        self.client.force_login(self.admin)
        self.client.post(reverse("orphan_import_cancel", args=[imp.token]))
        self.assertEqual(list(Orphan.objects.filter(import_batch=imp)), [o])
        self.assertTrue(Orphan.objects.filter(pk=manual.pk).exists())
        self.assertEqual(self.client.post(reverse("orphan_delete", args=[manual.pk])).status_code, 302)
        manual.refresh_from_db()
        self.assertFalse(manual.is_active)


class MaskFilterTests(TestCase):
    def test_mask(self):
        from .templatetags.registry_extras import mask
        self.assertEqual(mask("438990137", "3,2"), "438••••37")
        self.assertEqual(mask("12", "3,2"), "12")
        self.assertEqual(mask("", "3,2"), "")
