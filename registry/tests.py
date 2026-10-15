import datetime as dt
import tempfile

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from . import services
from .importer import auto_map, clean_rows, load_table, table_columns
from .models import AidType, Beneficiary, Benefit, Distribution, Project, Upload
from .importer import REGISTRY_FIELDS
from .roles import ADMIN, MANAGER, STAFF
from .sample import fake_people, write_distribution_file
from .textutils import clean_id, normalize_arabic, parse_date

TMP_MEDIA = tempfile.mkdtemp()
TODAY = dt.date.today()


class TextUtilsTests(TestCase):
    def test_normalize_arabic(self):
        self.assertEqual(normalize_arabic("أَحْمَد  إبراهيم"), normalize_arabic("احمد ابراهيم"))
        self.assertEqual(normalize_arabic("فاطمة"), normalize_arabic("فاطمه"))

    def test_clean_id(self):
        self.assertEqual(clean_id(401234567.0), "401234567")
        self.assertEqual(clean_id("٤٠١٢٣٤٥٦٧"), "401234567")
        self.assertEqual(clean_id(" 401 234 567 "), "401234567")
        self.assertEqual(clean_id("401234567.0"), "401234567")
        self.assertEqual(clean_id(None), "")

    def test_parse_date(self):
        self.assertEqual(parse_date("15/03/2026"), dt.date(2026, 3, 15))
        self.assertEqual(parse_date("2026-03-15"), dt.date(2026, 3, 15))
        self.assertEqual(parse_date(46096), dt.date(2026, 3, 15))
        self.assertIsNone(parse_date(""))
        with self.assertRaises(ValueError):
            parse_date("غير تاريخ")


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class ImporterTests(TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.people = fake_people(10, seed=7)

    def test_auto_map_and_clean(self):
        path = write_distribution_file(f"{self.dir}/a.xlsx", self.people, date=TODAY - dt.timedelta(days=5), aid_type="نقدية", with_dirty_rows=True)
        table, _ = load_table(path)
        mapping = auto_map(table_columns(table))
        self.assertTrue(all(mapping[f] for f in ("national_id", "name", "aid_type", "date", "social_status")))
        res = clean_rows(table, mapping, id_lengths=(9,))
        self.assertEqual(len(res["valid"]), 10)
        self.assertEqual(len(res["rejected"]), 1)
        self.assertEqual(len(res["duplicates"]), 1)

    def test_defaults_outside_file(self):
        path = write_distribution_file(f"{self.dir}/b.xlsx", self.people)
        table, _ = load_table(path)
        mapping = auto_map(table_columns(table))
        self.assertEqual(clean_rows(table, mapping)["valid"], [])  # لا نوع ولا تاريخ => كلها مرفوضة
        res = clean_rows(table, mapping, "طرد خضار", TODAY - dt.timedelta(days=2))
        self.assertEqual(len(res["valid"]), 10)
        self.assertEqual(res["valid"][0]["aid_type"], "طرد خضار")

    def test_future_date_rejected(self):
        path = write_distribution_file(f"{self.dir}/c.xlsx", self.people[:2], date=TODAY + dt.timedelta(days=3), aid_type="نقدية")
        table, _ = load_table(path)
        res = clean_rows(table, auto_map(table_columns(table)))
        self.assertEqual(len(res["rejected"]), 2)
        self.assertIn("المستقبل", res["rejected"][0]["reason"])


class UploadHelpers:
    """مساعدات مشتركة: المستخدمون (مدير النظام/مدير الدائرة/موظف) وإسنادهم للمشاريع ورفع الملفات."""

    @classmethod
    def make_users(cls):
        for r in (ADMIN, MANAGER, STAFF):
            Group.objects.get_or_create(name=r)
        cls.admin = User.objects.create_user("admin1", password="x-pass-123")
        cls.admin.groups.add(Group.objects.get(name=ADMIN))
        cls.entry = User.objects.create_user("entry1", password="x-pass-123")      # مدير دائرة
        cls.entry.groups.add(Group.objects.get(name=MANAGER))
        cls.viewer = User.objects.create_user("view1", password="x-pass-123")      # موظف
        cls.viewer.groups.add(Group.objects.get(name=STAFF))
        cls.nobody = User.objects.create_user("nobody", password="x-pass-123")

    def project(self, name):
        """مشروع يُسنَد إليه مدير الدائرة والموظف الافتراضيان."""
        p, _ = Project.objects.get_or_create(name=name)
        p.members.add(self.entry, self.viewer)
        return p

    def mk(self, person, **extra):
        return Beneficiary.objects.create(
            national_id=person["id"], full_name=person["name"], home_project=self.home, **extra)

    def registry_file(self, name, people):
        return write_distribution_file(f"{self.dir}/{name}", people, with_family=True)

    def upload(self, path, project="مشروع أ", aid="", date="", new_aid="", kind="distribution"):
        new_aid = new_aid or aid
        with open(path, "rb") as f:
            resp = self.client.post(reverse("upload_new"), {
                "kind": kind, "project": self.project(project).pk, "file": SimpleUploadedFile(path.split("/")[-1], f.read()),
                "new_aid_type": new_aid, "default_date": date,
            })
        self.assertEqual(resp.status_code, 302, getattr(resp, "context", None) and resp.context["form"].errors)
        upload = Upload.objects.latest("id")
        resp = self.client.get(reverse("upload_mapping", args=[upload.pk]))
        self.assertEqual(resp.status_code, 200)
        data = {f: upload.mapping[f] for f in upload.mapping}
        data.update({"new_aid_type": new_aid, "default_date": date})
        if kind == "registry":
            data = {k: v for k, v in data.items() if k in REGISTRY_FIELDS}
        if upload.default_aid_type:
            data["default_aid_type"] = upload.default_aid_type.pk
        self.client.post(reverse("upload_mapping", args=[upload.pk]), data)
        resp = self.client.get(reverse("upload_summary", args=[upload.pk]))
        self.assertEqual(resp.status_code, 200)
        upload.refresh_from_db()
        return upload, resp

    def confirm(self, upload):
        return self.client.post(reverse("upload_confirm", args=[upload.pk]))


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class FlowTests(UploadHelpers, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.make_users()

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.people = fake_people(30, seed=3)
        self.home = self.project("الرئيسي")
        self.client.force_login(self.entry)

    def test_full_flow_and_merge(self):
        d1 = (TODAY - dt.timedelta(days=60)).isoformat()
        d2 = TODAY - dt.timedelta(days=20)
        p1 = write_distribution_file(f"{self.dir}/cash.xlsx", self.people[:20])
        up1, resp = self.upload(p1, aid="نقدية", date=d1)
        self.assertEqual(resp.context["valid_count"], 20)
        self.assertEqual(resp.context["stats"]["new_beneficiaries"], 20)
        self.confirm(up1)
        up1.refresh_from_db()
        self.assertEqual(up1.status, Upload.CONFIRMED)
        self.assertEqual(Beneficiary.objects.count(), 20)
        self.assertEqual(Distribution.objects.get().names_count, 20)

        # ملف ثانٍ: النوع والتاريخ داخل الملف، ويتقاطع مع الأول في 10 أسماء
        p2 = write_distribution_file(f"{self.dir}/veg.xlsx", self.people[10:30], date=d2, aid_type="طرد خضار")
        up2, resp = self.upload(p2, project="مشروع ب")
        self.assertEqual(resp.context["stats"]["existing_beneficiaries"], 10)
        self.assertEqual(resp.context["stats"]["new_beneficiaries"], 10)
        self.confirm(up2)
        self.assertEqual(Beneficiary.objects.count(), 30)  # سجل واحد لكل شخص (قاعدة موحدة)
        self.assertEqual(Benefit.objects.count(), 40)

        # البحث بالهوية: من في الملفين له استفادتان
        shared = self.people[15]
        resp = self.client.get(reverse("search"), {"q": shared["id"]})
        row = resp.context["page"].object_list[0]
        self.assertEqual(row.total_benefits, 2)
        self.assertEqual(row.projects_count, 2)

        # البحث بالاسم بكتابة مختلفة للهمزة
        resp = self.client.get(reverse("search"), {"q": shared["name"].replace("أ", "ا").split()[0]})
        self.assertGreater(resp.context["page"].paginator.count, 0)

        # البحث عن كشف حسب النوع والتاريخ
        resp = self.client.get(reverse("distribution_list"), {"aid_type": AidType.objects.get(name="طرد خضار").pk, "date_from": d2.isoformat(), "date_to": d2.isoformat()})
        self.assertEqual(resp.context["summary"]["names"], 20)
        self.assertEqual(resp.context["summary"]["kashfs"], 1)

        # فلترة الشهر
        month = d2.strftime("%Y-%m")
        resp = self.client.get(reverse("search"), {"month_from": month, "month_to": month})
        self.assertEqual(resp.status_code, 200)

        # الكشف المجمع والتصدير
        resp = self.client.get(reverse("report"), {"aid_type": AidType.objects.get(name="نقدية").pk})
        self.assertEqual(resp.context["total"], 20)
        resp = self.client.get(reverse("report_export"), {"aid_type": AidType.objects.get(name="نقدية").pk})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheetml", resp["Content-Type"])
        dist = Distribution.objects.first()
        self.assertEqual(self.client.get(reverse("distribution_export", args=[dist.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("distribution_detail", args=[dist.pk])).status_code, 200)

    def test_reupload_is_idempotent(self):
        d = (TODAY - dt.timedelta(days=10)).isoformat()
        path = write_distribution_file(f"{self.dir}/x.xlsx", self.people[:10])
        up1, _ = self.upload(path, aid="نقدية", date=d)
        self.confirm(up1)
        up2, resp = self.upload(path, aid="نقدية", date=d)
        self.assertEqual(resp.context["stats"]["duplicates_in_db"], 10)
        self.confirm(up2)
        self.assertEqual(Benefit.objects.count(), 10)
        self.assertEqual(Distribution.objects.count(), 1)  # الكشف الفارغ لا يُنشأ

    def test_dirty_rows_isolated_and_undo(self):
        path = write_distribution_file(f"{self.dir}/d.xlsx", self.people[:8], date=TODAY - dt.timedelta(days=4), aid_type="نقدية", with_dirty_rows=True)
        up, resp = self.upload(path)
        self.assertEqual(len(resp.context["cleaned"]["rejected"]), 1)
        self.assertEqual(len(resp.context["cleaned"]["duplicates"]), 1)
        self.assertEqual(self.client.get(reverse("upload_rejected", args=[up.pk])).status_code, 200)
        self.confirm(up)
        self.assertEqual(Benefit.objects.count(), 8)
        # الدور «مدخل بيانات» لا يستطيع التراجع عن رفعة معتمدة
        self.assertEqual(self.client.post(reverse("upload_cancel", args=[up.pk])).status_code, 403)
        self.client.force_login(self.admin)
        self.client.post(reverse("upload_cancel", args=[up.pk]))
        self.assertEqual(Benefit.objects.count(), 0)
        self.assertEqual(Beneficiary.objects.count(), 0)
        self.assertEqual(Upload.objects.get(pk=up.pk).status, Upload.CANCELLED)

    def test_manual_entry_and_duplicates(self):
        project = self.project("يدوي")
        aid = AidType.objects.create(name="نقدية")
        resp = self.client.post(reverse("beneficiary_create"), {
            "full_name": "سامي النجار", "national_id": "401234567", "social_status": "معيل",
            "wife_name": "هدى أحمد محمد النجار", "wife_national_id": "801234567", "family_size": "5",
            "manager_notes": "حالة عاجلة",
            "project": project.pk, "benefit-aid_type": aid.pk,
            "benefit-receipt_date": (TODAY - dt.timedelta(days=1)).isoformat(),
        })
        self.assertEqual(resp.status_code, 302)
        b = Beneficiary.objects.get()
        self.assertEqual(b.benefits.count(), 1)
        self.assertEqual((b.wife_national_id, b.family_size, b.home_project_id), ("801234567", 5, project.pk))
        # هوية مكررة
        resp = self.client.post(reverse("beneficiary_create"), {"full_name": "آخر", "national_id": "401234567"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Beneficiary.objects.count(), 1)
        # هوية غير صالحة
        resp = self.client.post(reverse("beneficiary_create"), {"full_name": "آخر", "national_id": "123"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Beneficiary.objects.count(), 1)
        # إضافة نفس الاستفادة مرة ثانية لا تتكرر
        d = (TODAY - dt.timedelta(days=1)).isoformat()
        self.client.post(reverse("benefit_add", args=[b.pk]), {"project": project.pk, "aid_type": aid.pk, "receipt_date": d})
        self.assertEqual(b.benefits.count(), 1)

    def test_roles(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("search")).status_code, 200)
        self.assertEqual(self.client.get(reverse("upload_new")).status_code, 403)
        self.assertEqual(self.client.get(reverse("beneficiary_create")).status_code, 403)
        self.assertEqual(self.client.get(reverse("audit_list")).status_code, 403)
        for name in ("upload_list", "projects", "distribution_list", "stats", "user_list"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)     # الموظف: بحث واستعلام فقط
        self.client.force_login(self.nobody)
        self.assertEqual(self.client.get(reverse("search")).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("search")).status_code, 302)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("audit_list")).status_code, 200)

    def test_soft_delete_updates_counts(self):
        path = write_distribution_file(f"{self.dir}/s.xlsx", self.people[:5], date=TODAY - dt.timedelta(days=3), aid_type="نقدية")
        up, _ = self.upload(path)
        self.confirm(up)
        b = Beneficiary.objects.first()
        self.client.force_login(self.admin)
        self.client.post(reverse("beneficiary_delete", args=[b.pk]))
        self.assertEqual(Distribution.objects.get().names_count, 4)
        self.assertEqual(self.client.get(reverse("beneficiary_detail", args=[b.pk])).status_code, 404)


    # ---------------------------------------------------------------- القائمة الأساسية والرفع الجماعي
    def test_registry_upload_then_distribution_skip_unregistered(self):
        base = self.people[:20]
        up, resp = self.upload(self.registry_file("reg.xlsx", base), project="السجل", kind="registry")
        self.assertEqual(resp.context["valid_count"], 20)
        self.assertEqual(resp.context["stats"]["new_beneficiaries"], 20)
        self.confirm(up)
        self.assertEqual(Beneficiary.objects.count(), 20)
        self.assertEqual(Benefit.objects.count(), 0)          # لا استفادة من القائمة الأساسية
        self.assertEqual(Distribution.objects.count(), 0)

        # كشف توزيع: 10 مسجلون + 10 غير مسجلين
        d = (TODAY - dt.timedelta(days=6)).isoformat()
        path = write_distribution_file(f"{self.dir}/dist.xlsx", self.people[10:30])
        up2, resp = self.upload(path, project="نقدي", aid="نقدية", date=d)
        self.assertEqual(resp.context["stats"]["existing_beneficiaries"], 10)
        self.assertEqual(resp.context["stats"]["new_beneficiaries"], 10)
        self.assertEqual(len(resp.context["stats"]["unmatched_rows"]), 10)
        self.assertTrue(resp.context["registry_exists"])
        self.client.post(reverse("upload_confirm", args=[up2.pk]), {"skip_unregistered": "1"})
        self.assertEqual(Benefit.objects.count(), 10)
        self.assertEqual(Beneficiary.objects.count(), 20)      # لم يُضف غير المسجلين
        up2.refresh_from_db()
        self.assertEqual(up2.rows_rejected, 10)
        self.assertEqual(Distribution.objects.get().names_count, 10)

    def test_distribution_without_skip_adds_unregistered(self):
        d = (TODAY - dt.timedelta(days=6)).isoformat()
        up, _ = self.upload(self.registry_file("r.xlsx", self.people[:5]), project="س", kind="registry")
        self.confirm(up)
        up2, _ = self.upload(write_distribution_file(f"{self.dir}/d2.xlsx", self.people[3:8]), project="ن", aid="نقدية", date=d)
        self.confirm(up2)
        self.assertEqual(Beneficiary.objects.count(), 8)
        self.assertEqual(Benefit.objects.count(), 5)

    def test_registry_cancel_keeps_earlier_beneficiaries(self):
        up1, _ = self.upload(self.registry_file("a.xlsx", self.people[:6]), project="س", kind="registry")
        self.confirm(up1)
        up2, _ = self.upload(self.registry_file("b.xlsx", self.people[3:10]), project="س", kind="registry")
        self.confirm(up2)
        self.assertEqual(Beneficiary.objects.count(), 10)
        self.client.force_login(self.admin)
        self.client.post(reverse("upload_cancel", args=[up2.pk]))
        self.assertEqual(Beneficiary.objects.count(), 6)

    def test_status_without_and_export(self):
        d = (TODAY - dt.timedelta(days=6)).isoformat()
        up, _ = self.upload(self.registry_file("r.xlsx", self.people[:20]), project="س", kind="registry")
        self.confirm(up)
        up2, _ = self.upload(write_distribution_file(f"{self.dir}/c.xlsx", self.people[:5]), project="ن", aid="نقدية", date=d)
        self.confirm(up2)
        cash = AidType.objects.get(name="نقدية").pk
        resp = self.client.get(reverse("search"), {"status": "without", "aid_type": cash})
        self.assertEqual(resp.context["page"].paginator.count, 15)
        resp = self.client.get(reverse("search"), {"status": "with", "aid_type": cash})
        self.assertEqual(resp.context["page"].paginator.count, 5)
        resp = self.client.get(reverse("search"), {"status": "without"})
        self.assertEqual(resp.context["page"].paginator.count, 15)
        resp = self.client.get(reverse("search_export"), {"status": "without", "aid_type": cash})
        self.assertEqual(resp.status_code, 200)
        from io import BytesIO
        from openpyxl import load_workbook
        ws = load_workbook(BytesIO(b"".join(resp.streaming_content) if resp.streaming else resp.content)).active
        data_rows = [r for r in ws.iter_rows(values_only=True) if isinstance(r[0], int)]
        self.assertEqual(len(data_rows), 15)
        home = self.client.get(reverse("search"))
        self.assertEqual(home.context["stats"]["not_served"], 15)

    def test_batch_upload_and_confirm(self):
        d = (TODAY - dt.timedelta(days=6)).isoformat()
        p1 = write_distribution_file(f"{self.dir}/b1.xlsx", self.people[:6], date=d, aid_type="نقدية")
        p2 = write_distribution_file(f"{self.dir}/b2.xlsx", self.people[6:12], date=d, aid_type="طرد خضار")
        p3 = write_distribution_file(f"{self.dir}/b3.xlsx", self.people[12:15])  # بلا نوع ولا تاريخ => يحتاج مراجعة
        files = [SimpleUploadedFile(f"b{i}.xlsx", open(p, "rb").read()) for i, p in enumerate((p1, p2, p3), start=1)]
        resp = self.client.post(reverse("upload_new"), {"kind": "distribution", "project": self.project("دفعة").pk, "file": files})
        self.assertEqual(resp.status_code, 302)
        batch = Upload.objects.first().batch
        self.assertTrue(batch)
        self.assertEqual(Upload.objects.filter(batch=batch).count(), 3)
        page = self.client.get(reverse("upload_batch", args=[batch]))
        self.assertEqual(page.context["ready_count"], 2)
        self.client.post(reverse("upload_batch_confirm", args=[batch]), {})
        self.assertEqual(Benefit.objects.count(), 12)
        self.assertEqual(Upload.objects.filter(batch=batch, status=Upload.CONFIRMED).count(), 2)
        self.assertEqual(Upload.objects.filter(batch=batch, status=Upload.DRAFT).count(), 1)

    def test_pages_render(self):
        for name in ("upload_list", "upload_new", "projects", "beneficiary_create", "distribution_list", "report", "search"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_search_shows_aid_types(self):
        d1 = (TODAY - dt.timedelta(days=40)).isoformat()
        d2 = TODAY - dt.timedelta(days=10)
        p = self.people[:3]
        up1, _ = self.upload(write_distribution_file(f"{self.dir}/t1.xlsx", p), project="أ", aid="نقدية", date=d1)
        self.confirm(up1)
        up2, _ = self.upload(write_distribution_file(f"{self.dir}/t2.xlsx", p[:1], date=d2, aid_type="طرد خضار"), project="ب")
        self.confirm(up2)
        resp = self.client.get(reverse("search"), {"q": p[0]["id"]})
        b = resp.context["page"].object_list[0]
        self.assertEqual({t["name"] for t in b.aid_types}, {"نقدية", "طرد خضار"})
        self.assertContains(resp, "طرد خضار")
        self.assertContains(resp, "نقدية")
        resp = self.client.get(reverse("search"), {"q": p[2]["id"]})
        self.assertEqual([t["name"] for t in resp.context["page"].object_list[0].aid_types], ["نقدية"])

    def test_compare_file(self):
        d1 = (TODAY - dt.timedelta(days=60)).isoformat()
        d2 = TODAY - dt.timedelta(days=20)
        up1, _ = self.upload(write_distribution_file(f"{self.dir}/c1.xlsx", self.people[:10]), project="أ", aid="نقدية", date=d1)
        self.confirm(up1)
        up2, _ = self.upload(write_distribution_file(f"{self.dir}/c2.xlsx", self.people[:3], date=d2, aid_type="طرد خضار"), project="ب")
        self.confirm(up2)
        self.mk(self.people[10])
        before = (Beneficiary.objects.count(), Benefit.objects.count())

        # ملف المقارنة: 3 استلموا نقدية وطرداً، 2 نقدية فقط، 1 مسجل بلا استفادة، 2 غير مسجلين، + صف سيئ
        names = self.people[:5] + [self.people[10]] + self.people[20:22]
        path = write_distribution_file(f"{self.dir}/cmp.xlsx", names, with_dirty_rows=False)
        with open(path, "rb") as f:
            resp = self.client.post(reverse("compare_new"), {"file": SimpleUploadedFile("cmp.xlsx", f.read())})
        self.assertEqual(resp.status_code, 302)
        url = resp["Location"]
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("error", resp.context, resp.context.get("error"))
        c = resp.context["counts"]
        self.assertEqual((c["prior"], c["new"], c["unregistered"], c["review"], c["other"]), (5, 1, 2, 0, 0))
        # لا شيء يُحفظ
        self.assertEqual(before, (Beneficiary.objects.count(), Benefit.objects.count()))

        # فلتر النوع: طرد الخضار فقط → 3 سبق لهم، 2 استفادوا بنوع آخر
        resp = self.client.get(url, {"aid_type": AidType.objects.get(name="طرد خضار").pk})
        c = resp.context["counts"]
        self.assertEqual((c["prior"], c["other"], c["new"]), (3, 2, 1))
        # فلتر الفترة: آخر 30 يوماً فقط (طرد الخضار)
        resp = self.client.get(url, {"date_from": (TODAY - dt.timedelta(days=30)).isoformat()})
        self.assertEqual(resp.context["counts"]["prior"], 3)
        row = [r for r in resp.context["page"].object_list if r["national_id"] == self.people[0]["id"]][0]
        self.assertIn("طرد خضار", row["details"])
        self.assertEqual(row["total"], 2)
        self.assertEqual(row["similarity"], 100)

        # التصدير
        resp = self.client.get(url.replace("/compare/", "/compare/").split("?")[0] + "export/", {"show": "prior"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheetml", resp["Content-Type"])
        # رمز غير صالح
        self.assertEqual(self.client.get("/compare/../x/").status_code, 404)
        self.assertEqual(self.client.get(reverse("compare_result", args=["0" * 32])).status_code, 404)

    def test_compare_requires_login_and_file(self):
        resp = self.client.post(reverse("compare_new"), {})
        self.assertEqual(resp.status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("compare_new")).status_code, 302)
        self.client.force_login(self.nobody)
        self.assertEqual(self.client.get(reverse("compare_new")).status_code, 403)

    def test_manual_benefit_joins_project_kashf_and_shows_file_name(self):
        d1 = (TODAY - dt.timedelta(days=30))
        up, _ = self.upload(write_distribution_file(f"{self.dir}/proj.xlsx", self.people[:5]), project="مشروع س", aid="نقدية", date=d1.isoformat())
        self.confirm(up)
        outsider = self.mk(self.people[6])
        dist = Distribution.objects.get()
        # إدراج يدوي بنفس المشروع والنوع والتاريخ → يلتحق بنفس الكشف المرفوع
        project, aid = dist.project, dist.aid_type
        resp = self.client.post(reverse("benefit_add", args=[outsider.pk]), {
            "project": project.pk, "aid_type": aid.pk, "receipt_date": d1.isoformat(), "note": "قسيمة 12"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Distribution.objects.count(), 1)
        dist.refresh_from_db()
        self.assertEqual(dist.names_count, 6)
        resp = self.client.get(reverse("beneficiary_detail", args=[outsider.pk]))
        self.assertContains(resp, "إدخال يدوي")
        resp = self.client.get(reverse("beneficiary_detail", args=[Beneficiary.objects.get(national_id=self.people[0]["id"]).pk]))
        self.assertContains(resp, "proj.xlsx")           # اسم الملف بدل «ملف إكسل»
        self.assertNotContains(resp, "ملف إكسل<")
        # يظهر في تصدير المشروع
        resp = self.client.get(reverse("project_export", args=[project.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get(reverse("project_detail", args=[project.pk])).status_code, 200)

    def test_add_to_several_kashfs_and_add_person_by_id(self):
        d1, d2 = TODAY - dt.timedelta(days=50), TODAY - dt.timedelta(days=10)
        up1, _ = self.upload(write_distribution_file(f"{self.dir}/k1.xlsx", self.people[:3]), project="أ", aid="نقدية", date=d1.isoformat())
        self.confirm(up1)
        up2, _ = self.upload(write_distribution_file(f"{self.dir}/k2.xlsx", self.people[:3]), project="ب", aid="طرد", date=d2.isoformat())
        self.confirm(up2)
        p = self.mk(self.people[9])
        resp = self.client.post(reverse("benefit_add_kashfs", args=[p.pk]), {"distributions": [d.pk for d in Distribution.objects.all()]})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(p.benefits.count(), 2)
        self.assertEqual({d.names_count for d in Distribution.objects.all()}, {4})
        # إعادة نفس الإضافة لا تكرر
        self.client.post(reverse("benefit_add_kashfs", args=[p.pk]), {"distributions": [Distribution.objects.first().pk]})
        self.assertEqual(p.benefits.count(), 2)
        # إضافة برقم الهوية من صفحة الكشف
        q = self.mk(self.people[10])
        dist = Distribution.objects.first()
        self.client.post(reverse("distribution_add_person", args=[dist.pk]), {"national_id": q.national_id})
        self.assertTrue(q.benefits.filter(distribution=dist).exists())
        self.client.post(reverse("distribution_add_person", args=[dist.pk]), {"national_id": "999999999"})  # غير مسجل → لا خطأ
        self.assertFalse(Beneficiary.objects.filter(national_id="999999999").exists())

    def test_edit_delete_benefit_and_permissions(self):
        d1 = TODAY - dt.timedelta(days=20)
        up, _ = self.upload(write_distribution_file(f"{self.dir}/e.xlsx", self.people[:2]), project="أ", aid="نقدية", date=d1.isoformat())
        self.confirm(up)
        x = Benefit.objects.first()
        new_date = TODAY - dt.timedelta(days=5)
        resp = self.client.post(reverse("benefit_edit", args=[x.pk]), {
            "project": x.project_id, "aid_type": x.aid_type_id, "receipt_date": new_date.isoformat()})
        self.assertEqual(resp.status_code, 302)
        x.refresh_from_db()
        self.assertEqual(x.receipt_date, new_date)
        self.assertEqual(Distribution.objects.count(), 2)          # كشف جديد بالتاريخ الجديد
        # حذف: مدخل البيانات لا يحذف، المسؤول يحذف
        self.assertEqual(self.client.post(reverse("benefit_delete", args=[x.pk])).status_code, 403)
        self.client.force_login(self.admin)
        self.client.post(reverse("benefit_delete", args=[x.pk]))
        self.assertFalse(Benefit.objects.filter(pk=x.pk).exists())
        self.assertEqual(Distribution.objects.filter(upload__isnull=True).count(), 0)  # الكشف اليدوي الفارغ يزول

    def test_name_alias_recorded_and_promoted(self):
        d1 = TODAY - dt.timedelta(days=20)
        up, _ = self.upload(write_distribution_file(f"{self.dir}/n1.xlsx", self.people[:2]), project="أ", aid="نقدية", date=d1.isoformat())
        self.confirm(up)
        changed = [dict(self.people[0], name="محمد اسم مختلف تماما")] + [self.people[1]]
        up2, _ = self.upload(write_distribution_file(f"{self.dir}/n2.xlsx", changed), project="ب", aid="طرد", date=d1.isoformat())
        self.confirm(up2)
        b = Beneficiary.objects.get(national_id=self.people[0]["id"])
        self.assertEqual(b.full_name, self.people[0]["name"])        # الاسم المعتمد لم يتغير
        self.assertEqual(Beneficiary.objects.count(), 2)              # لا مكرر
        alias = b.aliases.get()
        self.assertEqual(alias.name, "محمد اسم مختلف تماما")
        self.assertContains(self.client.get(reverse("beneficiary_detail", args=[b.pk])), "أسماء بديلة")
        self.client.post(reverse("alias_promote", args=[alias.pk]))
        b.refresh_from_db()
        self.assertEqual(b.full_name, "محمد اسم مختلف تماما")
        self.assertEqual(b.aliases.get().name, self.people[0]["name"])

    def test_stats_and_export_all(self):
        d1 = TODAY - dt.timedelta(days=20)
        up, _ = self.upload(write_distribution_file(f"{self.dir}/s.xlsx", self.people[:4]), project="أ", aid="نقدية", date=d1.isoformat())
        self.confirm(up)
        resp = self.client.get(reverse("stats"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["s"]["served"], 4)
        self.assertEqual(self.client.get(reverse("export_all")).status_code, 403)   # للمسؤول فقط
        self.client.force_login(self.admin)
        r = self.client.get(reverse("export_all"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])

    def test_compare_id_only_and_name_only_files(self):
        from openpyxl import Workbook
        d1 = TODAY - dt.timedelta(days=30)
        up, _ = self.upload(write_distribution_file(f"{self.dir}/b1.xlsx", self.people[:4]), project="أ", aid="نقدية", date=d1.isoformat())
        self.confirm(up)
        up2, _ = self.upload(write_distribution_file(f"{self.dir}/b2.xlsx", self.people[:2]), project="ب", aid="طرد", date=(d1 + dt.timedelta(days=5)).isoformat())
        self.confirm(up2)

        def run(headers, rows):
            wb = Workbook(); ws = wb.active; ws.append(headers)
            for r in rows: ws.append(r)
            path = f"{self.dir}/q{len(headers)}{headers[0]}.xlsx"; wb.save(path)
            with open(path, "rb") as f:
                resp = self.client.post(reverse("compare_new"), {"file": SimpleUploadedFile("q.xlsx", f.read())})
            self.assertEqual(resp.status_code, 302)
            resp = self.client.get(resp["Location"])
            self.assertNotIn("error", resp.context, resp.context.get("error"))
            return resp.context["page"].object_list, resp.context["counts"]

        # هويات فقط
        rows, c = run(["رقم الهوية"], [[self.people[0]["id"]], [self.people[3]["id"]], ["999999999"]])
        by = {r["national_id"]: r for r in rows}
        self.assertEqual(by[self.people[0]["id"]]["matched_count"], 2)
        self.assertEqual(by[self.people[0]["id"]]["types_text"].count("×") + by[self.people[0]["id"]]["types_text"].count("،"), 1)
        self.assertEqual(by[self.people[3]["id"]]["matched_count"], 1)
        self.assertEqual(by["999999999"]["status"], "unregistered")
        # أسماء فقط
        rows, c = run(["الاسم"], [[self.people[0]["name"]], ["اسم غير موجود تماما"]])
        self.assertEqual(rows[0]["matched_count"], 2)
        self.assertEqual(rows[0]["national_id"], self.people[0]["id"])
        self.assertEqual(rows[1]["status"], "unregistered")
        self.assertEqual(c["mode"], "name")
        # الصفحة الرئيسية تعرض رفع ملف
        self.assertContains(self.client.get(reverse("search")), "ابحث بالملف")



@override_settings(MEDIA_ROOT=TMP_MEDIA)
class RolesScopeTests(UploadHelpers, TestCase):
    """الأدوار الثلاثة، نطاق المشاريع، الحقول الإلزامية، البحث بهوية الزوجة، ودمج الهويات."""

    @classmethod
    def setUpTestData(cls):
        cls.make_users()

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.people = fake_people(30, seed=5)
        self.home = self.project("الرئيسي")
        self.client.force_login(self.entry)

    FAMILY = {"wife_name": "هدى أحمد محمد النجار", "wife_national_id": "801234567", "family_size": "5"}

    # ------------------------------------------------------------ الأدوار
    def test_only_admin_creates_projects_and_users(self):
        self.assertEqual(self.client.post(reverse("projects"), {"name": "جديد"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 403)
        self.client.force_login(self.admin)
        self.client.post(reverse("projects"), {"name": "جديد"})
        self.assertTrue(Project.objects.filter(name="جديد").exists())
        resp = self.client.post(reverse("user_create"), {
            "username": "m2", "password": "Zx-9183-pass!", "role": MANAGER, "projects": [self.home.pk], "is_active": "on"})
        self.assertEqual(resp.status_code, 302)
        u = User.objects.get(username="m2")
        self.assertTrue(u.groups.filter(name=MANAGER).exists())
        self.assertEqual(list(u.assigned_projects.all()), [self.home])

    def test_manager_cannot_create_new_project_via_upload(self):
        path = write_distribution_file(f"{self.dir}/np.xlsx", self.people[:2], date=TODAY - dt.timedelta(days=2), aid_type="نقدية")
        with open(path, "rb") as f:
            resp = self.client.post(reverse("upload_new"), {
                "kind": "distribution", "new_project": "مشروع سري", "file": SimpleUploadedFile("np.xlsx", f.read())})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Project.objects.filter(name="مشروع سري").exists())

    def test_staff_read_only(self):
        b = self.mk(self.people[0], **{"wife_name": "هدى أحمد محمد النجار", "wife_national_id": "801234567", "family_size": 4})
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("beneficiary_detail", args=[b.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("beneficiary_edit", args=[b.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("beneficiary_delete", args=[b.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("beneficiary_create")).status_code, 403)
        self.assertEqual(self.client.get(reverse("upload_new")).status_code, 403)

    # ------------------------------------------------------------ نطاق المشاريع
    def test_scope_hides_other_projects(self):
        other = Project.objects.create(name="مشروع آخر")          # لا يُسنَد للمدير
        aid = AidType.objects.create(name="نقدية")
        mine, theirs = self.mk(self.people[0]), Beneficiary.objects.create(
            national_id=self.people[1]["id"], full_name=self.people[1]["name"], home_project=other)
        services.add_manual_benefit(theirs, other, aid, TODAY - dt.timedelta(days=3))
        resp = self.client.get(reverse("search"), {"q": theirs.national_id})
        self.assertEqual(resp.context["page"].paginator.count, 0)
        self.assertEqual(self.client.get(reverse("beneficiary_detail", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("beneficiary_detail", args=[mine.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("project_detail", args=[other.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("distribution_list")).context["summary"]["names"], 0)
        self.client.force_login(self.admin)                          # مدير النظام يرى كل شيء
        self.assertEqual(self.client.get(reverse("search"), {"q": theirs.national_id}).context["page"].paginator.count, 1)

    # ------------------------------------------------------------ الحقول الإلزامية
    def test_mandatory_fields(self):
        base = {"full_name": "سامي النجار", "national_id": "401234567", "project": self.home.pk, **self.FAMILY}
        for missing in ("wife_name", "wife_national_id", "family_size"):
            data = {k: v for k, v in base.items() if k != missing}
            self.assertEqual(self.client.post(reverse("beneficiary_create"), data).status_code, 200, missing)
        self.assertFalse(Beneficiary.objects.exists())
        short = dict(base, wife_name="هدى أحمد")                    # ليس رباعياً
        self.assertEqual(self.client.post(reverse("beneficiary_create"), short).status_code, 200)
        self.assertFalse(Beneficiary.objects.exists())
        self.assertEqual(self.client.post(reverse("beneficiary_create"), base).status_code, 302)
        self.assertEqual(Beneficiary.objects.get().wife_national_id, "801234567")

    def test_registry_import_is_strict(self):
        people = self.people[:6]
        path = self.registry_file("strict.xlsx", people)
        up, resp = self.upload(path, kind="registry")
        self.assertEqual(resp.context["valid_count"], 6)
        # ملف بلا أعمدة الأسرة: كل الصفوف ترفض
        bare = write_distribution_file(f"{self.dir}/bare.xlsx", people)
        with open(bare, "rb") as f:
            self.client.post(reverse("upload_new"), {
                "kind": "registry", "project": self.home.pk, "file": SimpleUploadedFile("bare.xlsx", f.read())})
        up2 = Upload.objects.latest("id")
        resp = self.client.get(reverse("upload_summary", args=[up2.pk]))
        self.assertEqual(resp.context["valid_count"], 0)
        self.assertEqual(self.client.post(reverse("upload_confirm", args=[up2.pk])).status_code, 302)
        self.assertEqual(Beneficiary.objects.count(), 0)

    # ------------------------------------------------------------ البحث بهوية الزوجة
    def test_search_by_wife_id(self):
        up, _ = self.upload(self.registry_file("w.xlsx", self.people[:5]), kind="registry")
        self.confirm(up)
        target = self.people[2]
        resp = self.client.get(reverse("search"), {"q": target["wife_id"]})
        rows = list(resp.context["page"].object_list)
        self.assertEqual([r.national_id for r in rows], [target["id"]])
        resp = self.client.get(reverse("search_suggest"), {"q": target["wife_id"]})
        self.assertEqual(resp.json()["results"][0]["via"], "هوية الزوجة")
        b = Beneficiary.objects.get(national_id=target["id"])
        self.assertEqual(b.family_size, target["size"])
        self.assertEqual(b.wife_name, target["wife"])

    def test_family_links(self):
        a = self.mk(self.people[0], wife_national_id=self.people[1]["id"], wife_name="س س س س")
        w = self.mk(self.people[1], wife_national_id=self.people[0]["id"], wife_name="ص ص ص ص")
        resp = self.client.get(reverse("beneficiary_detail", args=[a.pk]))
        self.assertEqual([o.pk for o, _ in resp.context["family"]], [w.pk])

    # ------------------------------------------------------------ الاستيراد: الصفوف الخام + دمج الهويات
    def test_raw_rows_kept_and_merge_report(self):
        d1, d2 = TODAY - dt.timedelta(days=40), TODAY - dt.timedelta(days=10)
        people = self.people[:5]
        p1 = write_distribution_file(f"{self.dir}/m1.xlsx", people, date=d1, aid_type="نقدية")
        up1, _ = self.upload(p1, project="دمج")
        self.assertEqual(up1.raw_rows.count(), 5)                   # كل الصفوف بكل أعمدتها
        self.assertIn("الحالة الاجتماعية", up1.raw_rows.first().data)
        self.confirm(up1)
        # الملف الثاني: هوية تتكرر داخل الملف نفسه (تاريخان مختلفان) + هويتان سبقتا في الملف الأول
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active
        ws.append(["رقم الهوية", "اسم المستفيد", "نوع المساعدة", "تاريخ الاستلام"])
        ws.append([people[0]["id"], people[0]["name"], "طرد", d2.isoformat()])
        ws.append([people[0]["id"], people[0]["name"], "طرد", (d2 + dt.timedelta(days=3)).isoformat()])
        ws.append([people[1]["id"], people[1]["name"], "طرد", d2.isoformat()])
        ws.append([self.people[20]["id"], self.people[20]["name"], "طرد", d2.isoformat()])
        p2 = f"{self.dir}/m2.xlsx"; wb.save(p2)
        up2, resp = self.upload(p2, project="دمج")
        self.assertEqual(resp.context["stats"]["repeated_in_file"], 1)
        self.assertEqual(resp.context["stats"]["repeated_across"], 2)
        self.confirm(up2)
        up2.refresh_from_db()
        by = {r["national_id"]: r for r in up2.report["rows"]}
        first = by[people[0]["id"]]
        self.assertEqual((first["in_file"], first["before"], first["added"], first["after"]), (2, 1, 2, 3))
        self.assertEqual(len(first["dates"]), 3)
        self.assertEqual(by[people[1]["id"]]["after"], 2)
        self.assertNotIn(self.people[20]["id"], by)                 # جديد وغير مكرر
        self.assertEqual(Benefit.objects.filter(beneficiary__national_id=people[0]["id"]).count(), 3)
        resp = self.client.get(reverse("upload_merge_report", args=[up2.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheetml", resp["Content-Type"])
