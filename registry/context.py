from .roles import ADMIN, MANAGER, ORPHAN_ROLES, has_role


def roles(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    return {
        "can_edit": has_role(user, MANAGER, ADMIN),
        "is_admin": has_role(user, ADMIN),
        "can_orphans": has_role(user, *ORPHAN_ROLES),
    }
