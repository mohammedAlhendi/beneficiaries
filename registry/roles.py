"""الأدوار: مدير النظام / مدير الدائرة / موظف (+ مسؤول كفالة لقسم الأيتام).

المستخدم الخارق (superuser) يملك كل الصلاحيات. أسماء الأدوار القديمة تُحوَّل تلقائياً بالترحيل 0006.

* مدير النظام: المستخدمون والصلاحيات والمشاريع وإعدادات النظام + كل ما يفعله مدير الدائرة.
* مدير الدائرة: إدارة المستفيدين (بحث/إضافة/تعديل/رفع ملفات) ضمن المشاريع المسندة إليه فقط.
* الموظف: بحث واستعلام فقط ضمن المشاريع المسندة إليه، دون إضافة أو تعديل أو حذف.
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

ADMIN = "مدير النظام"
MANAGER = "مدير الدائرة"
STAFF = "موظف"
SPONSOR = "مسؤول كفالة"        # يدير الأيتام والكفلاء (بيانات أطفال: وصول مقيّد)

# أسماء مستعارة للتوافق مع الكود والاختبارات السابقة
ENTRY = MANAGER
VIEW = STAFF

LEGACY_ROLE_NAMES = {"مسؤول": ADMIN, "مدخل بيانات": MANAGER, "قراءة فقط": STAFF}
ALL_ROLES = (ADMIN, MANAGER, STAFF, SPONSOR)
ORPHAN_ROLES = (ADMIN, SPONSOR)
EDIT_ROLES = (MANAGER, ADMIN)           # من يملك الإضافة والتعديل والرفع
READ_ROLES = ALL_ROLES                  # من يملك البحث والاستعلام
ROLE_CHOICES = [(ADMIN, ADMIN), (MANAGER, MANAGER), (STAFF, STAFF), (SPONSOR, SPONSOR)]


def has_role(user, *roles):
    if not user.is_authenticated:
        return False
    return user.is_superuser or user.groups.filter(name__in=roles).exists()


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(request, *args, **kwargs):
            if not has_role(request.user, *roles):
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


# ------------------------------------------------------------------ نطاق المشاريع
def is_global(user):
    """يرى كل المشاريع: المستخدم الخارق ومدير النظام."""
    return user.is_authenticated and (user.is_superuser or user.groups.filter(name=ADMIN).exists())


def project_scope(user):
    """None = كل المشاريع، وإلا قائمة معرّفات المشاريع المسندة للمستخدم (قد تكون فارغة = لا شيء)."""
    if is_global(user):
        return None
    if not user.is_authenticated:
        return []
    return list(user.assigned_projects.values_list("pk", flat=True))


def visible_projects(user):
    from .models import Project

    scope = project_scope(user)
    return Project.objects.all() if scope is None else Project.objects.filter(pk__in=scope)
