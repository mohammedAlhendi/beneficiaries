"""يحوّل أسماء الأدوار القديمة إلى الجديدة، ويُبقي المستخدمين القدامى قادرين على العمل:
كل مستخدم غير مدير نظام يُسند إلى كل المشاريع القائمة (يمكن للمدير تضييق ذلك لاحقاً)،
وتُنسب المستفيدون القدامى إلى المشروع الذي رُفعوا فيه."""
from django.db import migrations

RENAMES = {"مسؤول": "مدير النظام", "مدخل بيانات": "مدير الدائرة", "قراءة فقط": "موظف"}


def forward(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for old, new in RENAMES.items():
        old_g = Group.objects.filter(name=old).first()
        if not old_g:
            continue
        new_g = Group.objects.filter(name=new).first()
        if new_g:
            for u in old_g.user_set.all():
                new_g.user_set.add(u)
            old_g.delete()
        else:
            old_g.name = new
            old_g.save()

    Project = apps.get_model("registry", "Project")
    Beneficiary = apps.get_model("registry", "Beneficiary")
    admin_g = Group.objects.filter(name="مدير النظام").first()
    admin_ids = set(admin_g.user_set.values_list("pk", flat=True)) if admin_g else set()
    members = Group.objects.filter(name__in=["مدير الدائرة", "موظف", "مسؤول كفالة"])
    users = {}
    for g in members:
        for u in g.user_set.all():
            users[u.pk] = u
    projects = list(Project.objects.all())
    for pk, u in users.items():
        if pk in admin_ids or u.is_superuser:
            continue
        for p in projects:
            p.members.add(u)

    for b in Beneficiary.objects.filter(home_project__isnull=True, created_by_upload__isnull=False).select_related("created_by_upload"):
        b.home_project_id = b.created_by_upload.project_id
        b.save(update_fields=["home_project"])


class Migration(migrations.Migration):
    dependencies = [
        ("registry", "0005_roles_scope_wife_fields"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
