from django.contrib import admin
from django.urls import include, path
from django.contrib.auth.models import User
from django.http import HttpResponse

admin.site.site_header = "إدارة نظام المستفيدين"
admin.site.site_title = "نظام المستفيدين"

# دالة مؤقتة لإنشاء حساب المشرف سحابياً
def create_temp_superuser(request):
    if not User.objects.filter(username="admin").exists():
        User.objects.create_superuser("admin", "admin@example.com", "Admin12345")
        return HttpResponse("تم إنشاء حساب المشرف بنجاح! اسم المستخدم: admin | كلمة المرور: Admin12345")
    return HttpResponse("حساب المشرف موجود مسبقاً، يمكنك تسجيل الدخول.")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("create-admin-now/", create_temp_superuser), # الرابط المؤقت (احذفه لاحقاً)
    path("", include("registry.urls")),
]
