"""يُشغَّل أثناء البناء على Vercel: ترحيل قاعدة البيانات وإنشاء/تحديث حساب المشرف.

قاعدة البيانات قد تحتوي على بعض الجداول مسبقاً (أُنشئت خارج سجل الترحيلات)،
لذلك عند فشل ترحيل بسبب جدول موجود ننشئ الجداول الناقصة فقط ونسجّل الترحيل كمُطبَّق.
"""
import os

import django

ALREADY_EXISTS = ("already exists", "duplicate")


def _create_missing_tables(executor, migration):
    from django.db import connection
    from django.db.migrations.operations import CreateModel

    state = executor.loader.project_state((migration.app_label, migration.name), at_end=True)
    existing = set(connection.introspection.table_names())
    with connection.schema_editor() as editor:
        for op in migration.operations:
            if not isinstance(op, CreateModel):
                continue
            model = state.apps.get_model(migration.app_label, op.name)
            if model._meta.db_table not in existing:
                editor.create_model(model)
                existing.add(model._meta.db_table)
                continue
            for field in model._meta.local_many_to_many:
                through = field.remote_field.through
                if through._meta.auto_created and through._meta.db_table not in existing:
                    editor.create_model(through)
                    existing.add(through._meta.db_table)


def migrate():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    for _ in range(200):
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if not plan:
            print("Migrations up to date.")
            return
        migration, _ = plan[0]
        label = f"{migration.app_label}.{migration.name}"
        try:
            executor.migrate([(migration.app_label, migration.name)])
            print(f"Applied {label}")
        except Exception as exc:
            if not any(s in str(exc).lower() for s in ALREADY_EXISTS):
                raise
            _create_missing_tables(executor, migration)
            executor.recorder.record_applied(migration.app_label, migration.name)
            print(f"Synced existing schema for {label} ({exc.__class__.__name__})")
    raise RuntimeError("Too many migration steps")


def ensure_admin():
    username = os.environ.get("DJANGO_SUPERUSER_USERNAME")
    password = os.environ.get("DJANGO_SUPERUSER_PASSWORD")
    if not (username and password):
        print("DJANGO_SUPERUSER_USERNAME/PASSWORD not set; skipping admin setup.")
        return

    from django.contrib.auth import get_user_model

    User = get_user_model()
    user, created = User.objects.get_or_create(
        username=username,
        defaults={"email": os.environ.get("DJANGO_SUPERUSER_EMAIL", "")},
    )
    user.is_staff = True
    user.is_superuser = True
    user.is_active = True
    user.set_password(password)
    user.save()
    print(f"Admin user '{username}' {'created' if created else 'updated'}.")


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    migrate()
    ensure_admin()


if __name__ == "__main__":
    main()
