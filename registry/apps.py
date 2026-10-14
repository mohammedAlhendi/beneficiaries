from django.apps import AppConfig
from django.db.models.signals import post_migrate


def create_roles(sender, **kwargs):
    from django.contrib.auth.models import Group

    from .roles import ALL_ROLES

    for name in ALL_ROLES:
        Group.objects.get_or_create(name=name)


def log_login(sender, request, user, **kwargs):
    from .services import log

    log(user, "login")


class RegistryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "registry"
    verbose_name = "المستفيدون"

    def ready(self):
        from django.contrib.auth.signals import user_logged_in

        post_migrate.connect(create_roles, sender=self)
        user_logged_in.connect(log_login)
