"""
Configuracoes do projeto ZayaRun.

Le configuracao sensivel de variaveis de ambiente (arquivo .env no local,
config vars no Heroku). Mantenha segredos fora do controle de versao.
"""
import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Carrega .env em desenvolvimento local (no-op se o arquivo nao existir).
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.environ.get("SECRET_KEY", "dev-inseguro-troque-em-producao")
DEBUG = env_bool("DEBUG", True)

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if h.strip()
]
# Aceita os dominios das hospedagens suportadas (Heroku e Render).
ALLOWED_HOSTS.append(".herokuapp.com")
ALLOWED_HOSTS.append(".onrender.com")
ALLOWED_HOSTS.append(".up.railway.app")
ALLOWED_HOSTS.append(".railway.app")
# Render e Railway expoem o host externo nestas variaveis.
_render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if _render_host:
    ALLOWED_HOSTS.append(_render_host)
_railway_host = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
if _railway_host:
    ALLOWED_HOSTS.append(_railway_host)

CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip()
]
CSRF_TRUSTED_ORIGINS.append("https://*.herokuapp.com")
CSRF_TRUSTED_ORIGINS.append("https://*.onrender.com")
CSRF_TRUSTED_ORIGINS.append("https://*.up.railway.app")
CSRF_TRUSTED_ORIGINS.append("https://*.railway.app")
if _railway_host:
    CSRF_TRUSTED_ORIGINS.append(f"https://{_railway_host}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "runner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "zayarun.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "zayarun.wsgi.application"

# Banco: usa DATABASE_URL se existir (Heroku/Postgres), senao sqlite local.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        ssl_require=env_bool("DB_SSL_REQUIRE", False),
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "dashboard"

# Producao: reforca seguranca quando DEBUG=False.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# --- Insight do dia ---
# Provider: "auto" (Claude se ANTHROPIC_API_KEY existir, senao regras),
# "rules" (sempre regras) ou "claude" (sempre Claude, fallback regras).
INSIGHT_PROVIDER = os.environ.get("INSIGHT_PROVIDER", "auto")
INSIGHT_MODEL = os.environ.get("INSIGHT_MODEL", "claude-opus-4-8")
# A chave da Claude e lida do ambiente pelo SDK: ANTHROPIC_API_KEY

# --- IA (copiloto / prescricao / feedback) ---
# AI_PROVIDER: "auto" (usa a chave que existir, priorizando as gratuitas
# Groq -> Gemini), "groq" (GRATIS, sem cartao), "gemini" (Google AI Studio),
# "claude" ou "rules" (sem IA). Em "auto", basta uma chave gratuita p/ ligar.
AI_PROVIDER = os.environ.get("AI_PROVIDER", os.environ.get("INSIGHT_PROVIDER", "auto"))
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
# Chaves do ambiente: GROQ_API_KEY (gratis, sem cartao), GEMINI_API_KEY e/ou
# ANTHROPIC_API_KEY.

# --- Integracao Strava ---
STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")
STRAVA_REDIRECT_URI = os.environ.get(
    "STRAVA_REDIRECT_URI", "http://127.0.0.1:8000/strava/callback/"
)
