import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "نسخة احتياطية: قاعدة البيانات (SQLite) + ملفات الرفع، في مجلد backups/ كملف zip. (PostgreSQL: استخدم pg_dump)"

    def add_arguments(self, parser):
        parser.add_argument("--dir", default="backups", help="مجلد الحفظ (الافتراضي backups)")
        parser.add_argument("--no-media", action="store_true", help="بدون ملفات إكسل المرفوعة")

    def handle(self, *args, **o):
        db = settings.DATABASES["default"]
        if "sqlite" not in db["ENGINE"]:
            raise CommandError("القاعدة ليست SQLite. استخدم: pg_dump -Fc اسم_القاعدة > backup.dump")
        out = Path(o["dir"])
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        tmp = out / f"db-{stamp}.sqlite3"
        src = sqlite3.connect(str(db["NAME"]))
        dst = sqlite3.connect(str(tmp))
        with dst:
            src.backup(dst)  # نسخ آمن حتى لو كان الخادم يعمل
        src.close()
        dst.close()
        target = out / f"backup-{stamp}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp, "db.sqlite3")
            media = Path(settings.MEDIA_ROOT)
            if not o["no_media"] and media.exists():
                for p in media.rglob("*"):
                    if p.is_file() and "compare" not in p.parts:
                        z.write(p, f"media/{p.relative_to(media)}")
        tmp.unlink()
        self.stdout.write(self.style.SUCCESS(f"تم إنشاء النسخة: {target}  — انسخها إلى مكان خارج هذا الجهاز."))
