import datetime as dt
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from registry import services
from registry.importer import auto_map, clean_rows, load_table, table_columns
from registry.models import Project, Upload


class Command(BaseCommand):
    help = (
        "استيراد ملف إكسل كبير من الطرفية (مناسب لقائمة أساسية بمئات آلاف الأسماء، لا يتقيد بمهلة المتصفح).\n"
        "مثال: python manage.py import_file الاسر.xlsx --project 'السجل الأساسي' --kind registry"
    )

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--project", required=True, help="اسم المشروع (يُنشأ إن لم يوجد)")
        parser.add_argument("--kind", choices=["registry", "distribution"], default="registry")
        parser.add_argument("--aid-type", default="", help="نوع المساعدة لكل الصفوف (لكشوف التوزيع)")
        parser.add_argument("--date", default="", help="تاريخ التوزيع YYYY-MM-DD (لكشوف التوزيع)")
        parser.add_argument("--skip-unregistered", action="store_true", help="تخطي الهويات غير المسجلة")
        parser.add_argument("--update-names", action="store_true", help="تحديث الأسماء المسجلة عند اختلافها")
        parser.add_argument("--user", default="", help="اسم المستخدم لتسجيل العملية في سجل التدقيق")
        parser.add_argument("--dry-run", action="store_true", help="تنقية وعرض الملخص فقط دون حفظ")

    def handle(self, *args, **o):
        path = Path(o["path"])
        if not path.exists():
            raise CommandError(f"الملف غير موجود: {path}")
        date = dt.date.fromisoformat(o["date"]) if o["date"] else None
        user = None
        if o["user"]:
            user = get_user_model().objects.filter(username=o["user"]).first()
            if not user:
                raise CommandError(f"المستخدم غير موجود: {o['user']}")

        self.stdout.write("قراءة الملف وتنقيته ...")
        table, notes = load_table(path)
        for n in notes:
            self.stdout.write(n)
        mapping = auto_map(table_columns(table))
        self.stdout.write(f"المطابقة: { {k: v for k, v in mapping.items() if v} }")
        if not (mapping["national_id"] and mapping["name"]):
            raise CommandError("لم يُعثر على عمودي الاسم ورقم الهوية. راجع أسماء الأعمدة في الملف.")

        cleaned = clean_rows(table, mapping, o["aid_type"], date, id_lengths=settings.NATIONAL_ID_LENGTHS, kind=o["kind"], strict_quad=settings.STRICT_WIFE_QUAD_NAME)
        self.stdout.write(
            f"الصفوف: {cleaned['total']} | صالحة: {len(cleaned['valid'])} | "
            f"مكررة: {len(cleaned['duplicates'])} | مرفوضة: {len(cleaned['rejected'])}"
        )
        for r in cleaned["rejected"][:10]:
            self.stdout.write(f"  مرفوض (صف {r['row']}): {r['reason']}")
        if o["dry_run"] or not cleaned["valid"]:
            self.stdout.write("لم يُحفظ شيء." if o["dry_run"] else "لا توجد صفوف صالحة.")
            return

        project, _ = Project.objects.get_or_create(name=o["project"])
        upload = Upload(project=project, kind=o["kind"], file_name=path.name, mapping=mapping, uploaded_by=user)
        if o["aid_type"] and o["kind"] == "distribution":
            upload.default_aid_type = services.get_or_create_aid_type(o["aid_type"])
        upload.default_date = date
        with path.open("rb") as fh:
            upload.file.save(path.name, File(fh), save=False)
        upload.save()
        services.store_raw_rows(upload, table)
        summary = services.commit_upload(
            upload, cleaned, user, update_names=o["update_names"], skip_unregistered=o["skip_unregistered"]
        )
        self.stdout.write(self.style.SUCCESS(f"تم الاستيراد: {summary}"))
