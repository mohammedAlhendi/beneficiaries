from django.core.management.base import BaseCommand

from registry.sample import make_sample_set


class Command(BaseCommand):
    help = "ينشئ ملفات إكسل تجريبية (ببيانات وهمية) في المجلد المحدد لتجربة الرفع."

    def add_arguments(self, parser):
        parser.add_argument("folder", nargs="?", default="sample_data")

    def handle(self, *args, **opts):
        paths, _ = make_sample_set(opts["folder"])
        for p in paths:
            self.stdout.write(f"تم إنشاء: {p}")
        self.stdout.write("ملاحظة: ملف «نقدي» بلا تاريخ ولا نوع، فحدّدهما عند الرفع (مثلاً: نقدية + تاريخ).")
