"""إعدادات نظام قاعدة بيانات المستفيدين."""
import os
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me-in-production")
DEBUG = os.environ.get("DEBUG", "1") == "1"
ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "registry",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "registry.context.roles",
                "registry.context.upload_limit",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# قاعدة البيانات: SQLite افتراضياً للتجربة، وPostgreSQL عند ضبط DB_NAME في البيئة.
# Vercel لا يدعم IPv6، لذلك نفضّل رابط Supabase pooler (IPv4) عند توفره.
_DATABASE_URL = os.environ.get("POSTGRES_URL_NON_POOLING") or os.environ.get("DATABASE_URL")
if _DATABASE_URL:
    from urllib.parse import unquote, urlparse

    _url = urlparse(_DATABASE_URL)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _url.path.lstrip("/") or "postgres",
            "USER": unquote(_url.username or ""),
            "PASSWORD": unquote(_url.password or ""),
            "HOST": _url.hostname,
            "PORT": _url.port or 5432,
            "OPTIONS": {"sslmode": "require"},
        }
    }
elif os.environ.get("DB_NAME"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["DB_NAME"],
            "USER": os.environ.get("DB_USER", "postgres"),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", "localhost"),
            "PORT": os.environ.get("DB_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ar"
TIME_ZONE = "Asia/Hebron"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = Path(tempfile.gettempdir()) / "media" if os.environ.get("VERCEL") else BASE_DIR / "media"

# على Vercel تُحفظ الملفات المرفوعة في Supabase Storage (القرص للقراءة فقط هناك)
if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
    STORAGES = {
        "default": {"BACKEND": "config.storage.SupabaseStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "search"
LOGOUT_REDIRECT_URL = "login"

# جلسة تنتهي بعد ساعتين من الخمول (NFR-04)
SESSION_COOKIE_AGE = 60 * 60 * 2
SESSION_SAVE_EVERY_REQUEST = True

if os.environ.get("HTTPS") == "1":
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True

# ---- إعدادات خاصة بالنظام ----
# أطوال أرقام الهوية المقبولة (الهوية الفلسطينية 9 أرقام). عدّلها حسب الهوية المعتمدة.
NATIONAL_ID_LENGTHS = tuple(
    int(x) for x in os.environ.get("NATIONAL_ID_LENGTHS", "9").split(",") if x
)
# يلزم أن يكون اسم الزوجة رباعياً (4 كلمات فأكثر) في الإدخال والاستيراد؛ اجعله False لتخفيف الشرط
STRICT_WIFE_QUAD_NAME = os.environ.get("STRICT_WIFE_QUAD_NAME", "1") == "1"
# Vercel يرفض أي طلب أكبر من 4.5 ميغابايت قبل وصوله إلى Django
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "4" if os.environ.get("VERCEL") else "25"))
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_MB * 1024 * 1024

# تفعيل قراءة النطاقات القادمة عبر بروكسي Vercel بشكل صحيح
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
