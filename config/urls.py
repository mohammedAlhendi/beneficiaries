from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "إدارة نظام المستفيدين"
admin.site.site_title = "نظام المستفيدين"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("registry.urls")),
]
