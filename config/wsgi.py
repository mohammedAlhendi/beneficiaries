import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'your_project_name.settings') # استبدل your_project_name باسم مشروعك الفعلي

application = get_wsgi_application()

# --- كود إنشاء المشرف تلقائياً ---
try:
    from django.contrib.auth.models import User
    if not User.objects.filter(username='admin').exists():
        User.objects.create_superuser('admin', 'admin@example.com', 'Admin12345')
        print("Superuser 'admin' created successfully automatically!")
except Exception as e:
    print(f"Skipping superuser creation: {e}")
