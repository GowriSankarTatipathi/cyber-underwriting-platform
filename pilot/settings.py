import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() == "true"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY in .env. See README.md or run scripts/dev_db.py start.")
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,[::1]").split(",")
ROOT_URLCONF = "pilot.urls"
WSGI_APPLICATION = "pilot.wsgi.application"
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.messages", "django.contrib.staticfiles", "workspaces", "evidence", "underwriting", "ai_review",
    "allauth", "allauth.account", "allauth.mfa", "pilot",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "workspaces.middleware.WorkspaceSecurityMiddleware",
]
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": os.getenv("DB_NAME", "cyber_underwriting"),
    "USER": os.getenv("DB_USER", "cyber"),
    "PASSWORD": os.getenv("DB_PASSWORD", ""),
    "HOST": os.getenv("DB_HOST", "127.0.0.1"),
    "PORT": os.getenv("DB_PORT", "5432"),
    "CONN_MAX_AGE": 60,
}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "workspace_security_cache"}}
AUTH_USER_MODEL = "workspaces.User"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend", "allauth.account.auth_backends.AuthenticationBackend"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_ADAPTER = "workspaces.auth.AccountAdapter"
ACCOUNT_REAUTHENTICATION_REQUIRED = True
ACCOUNT_SESSION_REMEMBER = False
ACCOUNT_EMAIL_NOTIFICATIONS = True
MFA_ADAPTER = "workspaces.auth.MFAAdapter"
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
MFA_TOTP_ISSUER = "Cyber Underwriting"
MFA_TRUST_ENABLED = False
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/accounts/login/"
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.filebased.EmailBackend")
EMAIL_FILE_PATH = BASE_DIR / ".local" / "mail"
EMAIL_HOST = os.getenv("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "true").lower() == "true"
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "pilot@localhost")
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
PRIVATE_FILE_ROOT = BASE_DIR / ".local" / "private"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
# Preserve the origin on same-site form posts for Django's CSRF checks,
# while withholding referrer URLs from other sites.
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Evidence is never exposed by a public MEDIA_URL.
EVIDENCE_STORAGE_BACKEND = os.getenv("EVIDENCE_STORAGE_BACKEND", "local")
EVIDENCE_LOCAL_ROOT = BASE_DIR / ".local" / "evidence"
EVIDENCE_MAX_BYTES = 10 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
EVIDENCE_S3_BUCKET = os.getenv("EVIDENCE_S3_BUCKET", "")
EVIDENCE_S3_REGION = os.getenv("AWS_REGION", "us-east-1")
EVIDENCE_S3_ENDPOINT_URL = os.getenv("EVIDENCE_S3_ENDPOINT_URL") or None
EVIDENCE_S3_KMS_KEY_ID = os.getenv("EVIDENCE_S3_KMS_KEY_ID", "")
EVIDENCE_SCAN_MODE = os.getenv("EVIDENCE_SCAN_MODE", "basic" if DEBUG else "external")
EVIDENCE_SCAN_COMMAND = os.getenv("EVIDENCE_SCAN_COMMAND", "[]")
DATA_UPLOAD_MAX_NUMBER_FILES = 1
FILE_UPLOAD_HANDLERS = [
    "evidence.upload_handlers.SizeLimitedUploadHandler",
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]

# Queue payloads contain job IDs only. Private results and status stay in PostgreSQL.
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", f"redis+socket://{BASE_DIR / '.local/run/redis.sock'}?virtual_host=0")
CELERY_BROKER_CONNECTION_TIMEOUT = 2
CELERY_BROKER_TRANSPORT_OPTIONS = {"socket_connect_timeout": 2, "socket_timeout": 2, "visibility_timeout": 600}
CELERY_TASK_DEFAULT_QUEUE = "evidence_ai"
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_SEND_SENT_EVENT = False
CELERY_WORKER_SEND_TASK_EVENTS = False
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_SOFT_TIME_LIMIT = 240
CELERY_TASK_TIME_LIMIT = 270
CELERY_TASK_PUBLISH_RETRY = False
AI_REVIEW_PROVIDER = os.getenv("AI_REVIEW_PROVIDER", "demo" if DEBUG else "off")
AI_ALLOW_EXTERNAL = os.getenv("AI_ALLOW_EXTERNAL", "false").lower() == "true"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "")
AI_MAX_PAGES = 25
AI_MAX_TEXT_CHARS = 100000
AI_PREPARATION_TIMEOUT = 90
AI_PROVIDER_TIMEOUT = 60
AI_LEASE_SECONDS = 300
AI_MAX_ATTEMPTS = 3
AI_TESSERACT_COMMAND = os.getenv("AI_TESSERACT_COMMAND", "tesseract")
