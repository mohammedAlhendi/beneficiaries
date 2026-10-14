import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("registry", "0004_orphans_sponsors"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="project", name="created_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="+", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="project", name="members",
            field=models.ManyToManyField(blank=True, related_name="assigned_projects", to=settings.AUTH_USER_MODEL,
                                         verbose_name="المستخدمون المسندون"),
        ),
        migrations.AddField(
            model_name="upload", name="report",
            field=models.JSONField(blank=True, default=dict, verbose_name="تقرير دمج الهويات"),
        ),
        migrations.AddField(
            model_name="beneficiary", name="wife_name",
            field=models.CharField(blank=True, max_length=300, verbose_name="اسم الزوجة رباعي"),
        ),
        migrations.AddField(
            model_name="beneficiary", name="wife_name_search",
            field=models.CharField(blank=True, editable=False, max_length=300),
        ),
        migrations.AddField(
            model_name="beneficiary", name="wife_national_id",
            field=models.CharField(blank=True, db_index=True, max_length=20, verbose_name="رقم هوية الزوجة"),
        ),
        migrations.AddField(
            model_name="beneficiary", name="family_size",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="عدد أفراد الأسرة"),
        ),
        migrations.AddField(
            model_name="beneficiary", name="manager_notes",
            field=models.TextField(blank=True, verbose_name="ملاحظات المدير"),
        ),
        migrations.AddField(
            model_name="beneficiary", name="home_project",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="home_beneficiaries", to="registry.project",
                                    verbose_name="المشروع الذي سُجّل فيه"),
        ),
        migrations.CreateModel(
            name="UploadRow",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sheet", models.CharField(blank=True, max_length=100)),
                ("row_number", models.PositiveIntegerField(blank=True, null=True)),
                ("data", models.JSONField(default=dict)),
                ("upload", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="raw_rows",
                                             to="registry.upload")),
            ],
            options={"verbose_name": "صف خام", "verbose_name_plural": "الصفوف الخام", "ordering": ["id"]},
        ),
    ]
