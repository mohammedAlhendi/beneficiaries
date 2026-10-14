from django.contrib import admin

from .models import AidType, AuditLog, Beneficiary, Benefit, Distribution, Project, Upload


@admin.register(AidType)
class AidTypeAdmin(admin.ModelAdmin):
    search_fields = ["name"]


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    search_fields = ["name"]
    filter_horizontal = ["members"]


@admin.register(Beneficiary)
class BeneficiaryAdmin(admin.ModelAdmin):
    list_display = ["full_name", "national_id", "is_active"]
    search_fields = ["full_name", "national_id"]
    list_filter = ["is_active"]


@admin.register(Distribution)
class DistributionAdmin(admin.ModelAdmin):
    list_display = ["aid_type", "distribution_date", "project", "names_count"]
    list_filter = ["aid_type", "project"]


@admin.register(Upload)
class UploadAdmin(admin.ModelAdmin):
    list_display = ["file_name", "project", "status", "uploaded_by", "uploaded_at"]
    list_filter = ["status", "project"]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "username", "action", "object_repr"]
    list_filter = ["action"]
    search_fields = ["username", "object_repr", "details"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(Benefit)
