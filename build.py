"""يُشغَّل أثناء البناء على Vercel: ترحيل قاعدة البيانات وإنشاء/تحديث حساب المشرف."""
import os

import django
from django.core.management import call_command


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    call_command("migrate", interactive=False)

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


if __name__ == "__main__":
    main()
