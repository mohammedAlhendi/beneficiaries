import os
import sys
from django.core.wsgi import get_wsgi_application

# التأكد من أن مجلد المشروع الرئيسي موجود في مسار البايثون
path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if path not in sys.path:
    sys.path.append(path)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

try:
    application = get_wsgi_application()
except Exception as e:
    # سيساعد هذا في ظهور خطأ واضح في السجلات إذا واجهت البيئة مشكلة في التحميل
    print(f"WSGI application startup error: {e}", file=sys.stderr)
    raise
