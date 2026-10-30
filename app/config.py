"""All settings come from environment variables (see .env.example)."""
import os


def _get(name, default=""):
    # empty values fall back to the default (Docker panels often pass VAR="")
    v = (os.environ.get(name) or "").strip()
    return v or default


def _int(name, default):
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _bool(name, default=False):
    v = _get(name, "true" if default else "false").lower()
    return v in ("1", "true", "yes", "on")


# --- Store -----------------------------------------------------------------
STORE_NAME = _get("STORE_NAME", "our store")
SITE_URL = _get("SITE_URL").rstrip("/")                 # https://yourshop.com
SITEMAP_URL = _get("SITEMAP_URL") or (SITE_URL + "/sitemap.xml" if SITE_URL else "")
# Regex: only crawl URLs matching INCLUDE (empty = all), skip URLs matching EXCLUDE
SITEMAP_INCLUDE = _get("SITEMAP_INCLUDE", "")
SITEMAP_EXCLUDE = _get(
    "SITEMAP_EXCLUDE",
    r"/(cart|checkout|my-account|wp-login|wp-admin|feed|author|tag)(/|$)|\?add-to-cart=",
)
PRODUCT_URL_PATTERN = _get("PRODUCT_URL_PATTERN", r"/product/")
# How product links are shown to customers. {site} = SITE_URL, {slug} = product slug.
# Headless/Next.js shops: keep the default so links point to the storefront, not WooCommerce.
PRODUCT_LINK_TEMPLATE = _get("PRODUCT_LINK_TEMPLATE", "{site}/product/{slug}")
MAX_PAGES = _int("MAX_PAGES", 3000)
CRAWL_CONCURRENCY = _int("CRAWL_CONCURRENCY", 4)
SYNC_INTERVAL_HOURS = _int("SYNC_INTERVAL_HOURS", 24)   # 0 = never auto re-sync

# --- WooCommerce -----------------------------------------------------------
WOO_URL = (_get("WOO_URL") or SITE_URL).rstrip("/")
WOO_KEY = _get("WOO_CONSUMER_KEY")
WOO_SECRET = _get("WOO_CONSUMER_SECRET")
CURRENCY_SYMBOL = _get("CURRENCY_SYMBOL", "")         # empty = read from WooCommerce
MAX_PRODUCT_CARDS = _int("MAX_PRODUCT_CARDS", 4)

# --- Chatwoot --------------------------------------------------------------
CHATWOOT_URL = _get("CHATWOOT_URL").rstrip("/")         # https://chat.yourshop.com
CHATWOOT_BOT_TOKEN = _get("CHATWOOT_BOT_TOKEN")
WEBHOOK_TOKEN = _get("WEBHOOK_TOKEN")                    # secret in the webhook URL
ADMIN_TOKEN = _get("ADMIN_TOKEN")
ONLY_PENDING = _bool("BOT_ONLY_PENDING", True)           # stay quiet once a human took over

# --- AI providers (tried in this order; first that works wins) -------------
LLM_PROVIDERS = [p.strip() for p in _get("LLM_PROVIDERS", "gemini,groq,openrouter").split(",") if p.strip()]
GEMINI_API_KEY = _get("GEMINI_API_KEY")
GEMINI_MODEL = _get("GEMINI_MODEL", "gemini-3.6-flash")
GROQ_API_KEY = _get("GROQ_API_KEY")
GROQ_MODEL = _get("GROQ_MODEL", "llama-3.3-70b-versatile")
OPENROUTER_API_KEY = _get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = _get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
OLLAMA_URL = _get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = _get("OLLAMA_MODEL", "qwen2.5:3b")
LLM_TIMEOUT = _int("LLM_TIMEOUT", 45)

# --- Bot behaviour ---------------------------------------------------------
BOT_LANGUAGE_HINT = _get("BOT_LANGUAGE_HINT", "Reply in the same language the customer writes in.")
HANDOFF_MESSAGE = _get("HANDOFF_MESSAGE", "Sure! I'm connecting you with our team. Someone will reply here shortly.")
EXTRA_INSTRUCTIONS = _get("EXTRA_INSTRUCTIONS", "")      # e.g. "We ship only within India."
HISTORY_TURNS = _int("HISTORY_TURNS", 6)

DATA_DIR = _get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "knowledge.db")
