"""Conversation logic: order lookup, human handoff, and answers from site content."""
import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from . import config, llm
from .kb import _tokens

log = logging.getLogger("bot")

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Order references: 1008, #1008, JES1008, #JES-1008, ORD/1008
_REF = r"[A-Za-z]{0,6}[-/]?\d{2,12}"
ORDER_INTENT_RE = re.compile(
    r"(\btrack(ing)?\b|\bstatus of|where('?s| is) my|my order|my parcel|my package|order (status|number|no|id)|"
    r"order\s*#|\border\b.*\d{3,}|\bdeliver(ed|y status)\b.*\border\b|#\s*" + _REF + r"\b)",
    re.I)
ORDER_NUM_RE = re.compile(r"(?:#\s*|order\s*(?:no\.?|number|id)?\s*[:#]?\s*)(" + _REF + r")\b", re.I)
BARE_NUM_RE = re.compile(r"(?<![\w@.])(" + _REF + r")\b")
HANDOFF_RE = re.compile(
    r"\b(human|real person|live (agent|chat|person)|talk to (someone|a person|support|agent|an agent)|"
    r"speak (to|with) (someone|a person|an agent|support)|customer (care|service)|representative|connect me)\b",
    re.I)
HANDOFF_MARK = "[HANDOFF]"
HOW_TO_RE = re.compile(r"\b(how|steps?|way to|guide|process|procedure|can i|cancel|cancell?ation|return|exchange|refund)\b", re.I)
GUIDE_LINE_RE = re.compile(r"^[ \t*_]*GUIDE[ \t*_]*:\s*#?(\d+).*$", re.I | re.M)
PRODUCTS_LINE_RE = re.compile(r"^[ \t*_]*PRODUCTS?[ \t*_]*:(.*)$", re.I | re.M)


def storefront_links(text):
    """Replace any WooCommerce-backend link (e.g. web.yoursite.com) with the storefront SITE_URL."""
    woo, site = config.WOO_URL.rstrip("/"), config.SITE_URL.rstrip("/")
    if not text or not woo or not site or woo == site:
        return text
    for scheme in ("https://", "http://"):
        host = woo.split("://", 1)[-1]
        text = text.replace(scheme + host, site)
    # storefront URLs have no trailing slash on product pages: /product/slug/ -> /product/slug
    return re.sub(r"(%s/product/[^\s)\]/]+)/(?=[\s)\]]|$)" % re.escape(site), r"\1", text)


ORDER_STATUS = {
    "pending": "\u23f3 Awaiting payment",
    "processing": "\U0001F4E6 Processing \u2013 we're preparing your order",
    "on-hold": "\u23f8\ufe0f On hold",
    "completed": "\u2705 Completed",
    "shipped": "\U0001F69A Shipped",
    "delivered": "\u2705 Delivered",
    "cancelled": "\u274c Cancelled",
    "refunded": "\u21a9\ufe0f Refunded",
    "failed": "\u26a0\ufe0f Payment failed",
}


def format_order(info):
    status = ORDER_STATUS.get(info["status"], str(info["status"]).replace("-", " ").title())
    lines = ["**Order #%s**" % info["order"], "", "**Status:** %s" % status]
    if info.get("date"):
        lines.append("**Ordered on:** %s" % info["date"])
    if info.get("items"):
        lines.append("**Items:** " + ", ".join(info["items"]))
    lines.append("**Total:** %s" % info["total"])
    if info.get("shipping"):
        lines.append("**Shipping:** " + ", ".join(s for s in info["shipping"] if s))
    if info.get("tracking"):
        lines.append("**Tracking:** " + "; ".join(info["tracking"]))
    lines += ["", "Need help with this order? Type \"agent\" to chat with our team."]
    return "\n".join(lines)


GENERIC_WORDS = set("""product products item items buy buying price prices cost show see list available stock sell
sale cheap best good new all something anything thing things looking find search want need some more other""".split())


def product_keywords(text, n=2):
    """Main words of a shopping question, e.g. 'show me baby products' -> ['baby']."""
    words = []
    for w in _tokens(text):
        if w in GENERIC_WORDS or w.isdigit():
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]  # bottles -> bottle (WooCommerce search is literal)
        if w not in words:
            words.append(w)
    return sorted(words, key=len, reverse=True)[:n]


class Bot:
    def __init__(self, kb, woo, guides=None):
        self.kb = kb
        self.woo = woo
        self.guides = guides
        self.history = defaultdict(lambda: deque(maxlen=config.HISTORY_TURNS * 2))
        self.order_state = {}  # conv_key -> {"order":..., "email":..., "ts":...}

    # ---------------------------------------------------------------- helpers
    def _remember(self, key, role, content):
        self.history[key].append({"role": role, "content": content[:2000]})

    async def _order_flow(self, key, text, contact_email):
        state = self.order_state.get(key)
        if state and time.time() - state["ts"] > 1800:
            state = None
        email = EMAIL_RE.search(text)
        rest = EMAIL_RE.sub(" ", text)  # don't read digits inside the email as an order number
        num = ORDER_NUM_RE.search(rest) or (BARE_NUM_RE.search(rest) if state else None)
        if not state and email and not num:
            # Email sent as a follow-up ("what's the email on your order?") -> find the order number
            # in the customer's previous messages
            last_bot = next((m["content"] for m in reversed(self.history[key]) if m["role"] == "assistant"), "")
            if re.search(r"\border|email", last_bot, re.I):
                for m in reversed([m["content"] for m in self.history[key] if m["role"] == "user"][-3:]):
                    n = ORDER_NUM_RE.search(EMAIL_RE.sub(" ", m)) or BARE_NUM_RE.search(EMAIL_RE.sub(" ", m))
                    if n:
                        num, state = n, {"order": None, "email": None}
                        break
        intent = ORDER_INTENT_RE.search(text)
        if intent and not num:
            num = BARE_NUM_RE.search(rest)  # "track 1008"
        if not state and not intent:
            return None
        if state and not num and not email and not ORDER_INTENT_RE.search(text):
            self.order_state.pop(key, None)  # customer changed topic
            return None

        state = state or {"order": None, "email": contact_email, "from_contact": bool(contact_email)}
        if num:
            state["order"] = num.group(1)
        if email:
            state["email"] = email.group(0)
            state["from_contact"] = False
        state["ts"] = time.time()

        if not self.woo.enabled:
            self.order_state.pop(key, None)
            return None
        if not state["order"]:
            self.order_state[key] = state
            return "I can check that for you. What is your order number?"
        if not state["email"]:
            self.order_state[key] = state
            return ("Thanks! Please also share the email address used for order #%s, so I can verify it's yours."
                    % state["order"].lstrip("#").upper())

        self.order_state.pop(key, None)
        ref = state["order"].lstrip("#").upper()
        info, err = await self.woo.order_status(state["order"], state["email"])
        if err == "not_found" and state.get("from_contact"):
            # the email Chatwoot knows may differ from the one used at checkout -> ask for it
            state.update(email=None, from_contact=False, ts=time.time())
            self.order_state[key] = state
            return "Please share the email address used when placing order #%s, so I can verify it's yours." % ref
        if err == "not_found":
            return ("I couldn't find order #%s with that email address. Please double-check both, "
                    "or type \"agent\" to talk to our team." % ref)
        if not info:
            return "Sorry, I can't reach our order system right now. Type \"agent\" and our team will help you."
        return format_order(info)

    def _product_slugs(self, hits):
        pat = re.compile(config.PRODUCT_URL_PATTERN) if config.PRODUCT_URL_PATTERN else None
        slugs = []
        for h in hits:
            if pat and pat.search(h["url"]):
                slug = urlparse(h["url"]).path.rstrip("/").rsplit("/", 1)[-1]
                if slug and slug not in slugs:
                    slugs.append(slug)
        return slugs

    async def _live_products(self, slugs):
        results = await asyncio.gather(*(self.woo.product_by_slug(s) for s in slugs))
        return {s: r for s, r in zip(slugs, results) if r}

    def _system_prompt(self, context):
        return f"""You are the friendly customer-support assistant for {config.STORE_NAME} ({config.SITE_URL}).
Answer the customer's question using ONLY the information in the CONTEXT below (taken from our website and live store data).

Rules:
- Be short and warm: 1-3 sentences. No long lists.
- PRODUCTS: when you recommend or mention specific products, do NOT write their names as a list, links or prices
  in your text - they are shown to the customer automatically as product cards with image, price and a button.
  Just write a short sentence (e.g. "Here are some glass water bottles you might like:"), then add a LAST line
  exactly like:  PRODUCTS: slug-one, slug-two
  using as many matching slugs as fit (up to {config.MAX_PRODUCT_CARDS}), copied exactly from the CONTEXT, best match first.
  When the customer asks to see products or a category, show plenty of options, not just one or two.
  If the customer asks about one product's details, answer briefly in text and still add its PRODUCTS line.
- For non-product pages (policies, contact, about), you may use one short markdown link like [Shipping policy](url).
- Never invent prices, stock, policies, delivery times, discounts or contact details. Live store data overrides website text.
- If the context does not contain the answer, say you're not sure and offer to connect them with our team.
- For order tracking, ask for their order number and email (our system will look it up).
- GUIDES: if a [GUIDE] in the CONTEXT explains how to do what the customer asks (cancel an order, check order
  status, return an item, etc.), do NOT write the steps yourself - they are sent automatically with screenshots.
  Write ONE short friendly sentence (e.g. "Here's how you can cancel your order:"), then add a LAST line exactly like:
  GUIDE: <id>
  Prefer a matching guide over handing the chat to a human.
- If the customer is angry, wants a refund/return/cancellation processed and no GUIDE covers it, asks for a human,
  or you cannot help, reply briefly and end your message with {HANDOFF_MARK}
- {config.BOT_LANGUAGE_HINT}
{config.EXTRA_INSTRUCTIONS}

CONTEXT:
{context}
"""

    @staticmethod
    def _card(p):
        # Title + current price only. "Out of stock" is added only when it can't be bought.
        desc = p.get("card_price") or p.get("price") or ""
        if not p.get("in_stock", True):
            desc = (desc + " \u00b7 Out of stock").strip(" \u00b7")
        card = {"title": p["name"][:80], "description": desc,
                "actions": [{"type": "link", "text": "View product \u2192", "uri": p["url"]}]}
        if config.SHOW_PRODUCT_IMAGES and p.get("image"):
            card["media_url"] = p["image"]
        return card

    # ---------------------------------------------------------------- main
    async def answer(self, key, text, contact_email=None):
        """Returns (reply_text, handoff: bool, product_cards: list, guide: dict | None)."""
        text = (text or "").strip()
        if not text:
            return None, False, [], None

        if HANDOFF_RE.search(text):
            self._remember(key, "user", text)
            return config.HANDOFF_MESSAGE, True, [], None

        # "How do I cancel my order?" -> send the guide, not the order lookup
        how_to = HOW_TO_RE.search(text) and not ORDER_NUM_RE.search(EMAIL_RE.sub(" ", text))
        if how_to and self.guides and self.guides.strong_match(text):
            self.order_state.pop(key, None)
            order_reply = None
        else:
            order_reply = await self._order_flow(key, text, contact_email)
        if order_reply:
            self._remember(key, "user", text)
            self._remember(key, "assistant", order_reply)
            return order_reply, False, [], None

        # Short follow-ups ("and in blue?") need the previous question for search
        query = text
        prev_user = [m["content"] for m in self.history[key] if m["role"] == "user"]
        if len(text.split()) < 6 and prev_user:
            query = prev_user[-1] + " " + text

        hits = await asyncio.to_thread(self.kb.search, query, 14)
        slugs = self._product_slugs(hits)[:12]
        live = await self._live_products(slugs)
        # Also ask WooCommerce directly, so we can show more matching products than the website search found
        found = await asyncio.gather(*(self.woo.search_products(k, limit=12) for k in product_keywords(text)))
        for p in [p for group in found for p in group]:
            if p.get("slug") and p["slug"] not in live and len(live) < 16:
                live[p["slug"]] = p
                slugs.append(p["slug"])

        guide_hits = await asyncio.to_thread(self.guides.search, query, 2) if self.guides else []
        parts = []
        for g in guide_hits:
            parts.append("[GUIDE id=%s] %s\nSteps: %s" % (
                g["id"], g["title"], " | ".join("%d. %s" % (i + 1, st["text"]) for i, st in enumerate(g["steps"]))))
        for slug, p in live.items():
            parts.append("[PRODUCT slug=%s] %s | price: %s | stock: %s\n%s"
                         % (slug, p["name"], p["price"], p["stock"], p["summary"]))
        product_urls = set()
        pat = re.compile(config.PRODUCT_URL_PATTERN) if config.PRODUCT_URL_PATTERN else None
        for h in hits:
            if pat and pat.search(h["url"]):
                product_urls.add(h["url"])
                slug = urlparse(h["url"]).path.rstrip("/").rsplit("/", 1)[-1]
                parts.append("[PRODUCT PAGE slug=%s] %s\n%s" % (slug, h["title"], h["text"]))
            else:
                parts.append("[PAGE] %s | %s\n%s" % (h["title"], h["url"], h["text"]))
        context = "\n\n---\n\n".join(parts) or "(no matching content found on the website)"

        messages = list(self.history[key]) + [{"role": "user", "content": text}]
        reply, provider = await llm.chat(self._system_prompt(context), messages)

        handoff, chosen, guide = False, [], None
        if reply:
            g = GUIDE_LINE_RE.search(reply)
            if g:
                reply = GUIDE_LINE_RE.sub("", reply).strip()
                guide = next((x for x in guide_hits if str(x["id"]) == g.group(1)), None)
            if HANDOFF_MARK in reply:
                reply = reply.replace(HANDOFF_MARK, "").strip()
                if guide:
                    reply = reply or "Here's how:"
                else:
                    handoff = True
                    reply = reply or config.HANDOFF_MESSAGE
            m = PRODUCTS_LINE_RE.search(reply)
            if m:
                reply = PRODUCTS_LINE_RE.sub("", reply).strip()
                for s in re.split(r"[,\s]+", m.group(1)):
                    s = s.strip(" .`'\"[]/").rsplit("/", 1)[-1]
                    if s in slugs and s not in chosen:
                        chosen.append(s)
            log.info("answered via %s", provider)
        else:
            # All AI providers unavailable -> search-only answer
            guide = self.guides.strong_match(query) if self.guides else None
            chosen = [] if guide else slugs[:config.MAX_PRODUCT_CARDS]
            pages = []
            for h in hits:
                if h["url"] not in product_urls and h["url"] not in [u for _, u in pages]:
                    pages.append((h["title"], h["url"]))
            if guide:
                reply = "Here's how:"
            elif chosen and live:
                reply = "Here are some products that match what you're looking for:"
            elif pages:
                reply = ("Here's what I found on our website that may help:\n" +
                         "\n".join("- [%s](%s)" % p for p in pages[:3]))
            else:
                reply, handoff = config.HANDOFF_MESSAGE, True
            if not handoff:
                reply += "\n\nType \"agent\" if you'd like to talk to our team."

        chosen = sorted([s for s in chosen if s in live], key=lambda s: not live[s].get("in_stock", True))
        cards = [self._card(live[s]) for s in chosen[:config.MAX_PRODUCT_CARDS]]
        reply = storefront_links(reply) or ("Here you go:" if cards else config.HANDOFF_MESSAGE)
        self._remember(key, "user", text)
        memo = reply + ("\n(Showed products: %s)" % ", ".join(c["title"] for c in cards) if cards else "")
        if guide:
            memo += "\n(Sent the step-by-step guide: %s)" % guide["title"]
        self._remember(key, "assistant", memo)
        return reply, handoff, cards, guide


def cards_as_text(cards):
    """Plain-text version of product cards for channels that can't show cards (WhatsApp, email...)."""
    lines = []
    for c in cards:
        lines.append("*%s*%s\n%s" % (c["title"], (" - " + c["description"]) if c["description"] else "",
                                       c["actions"][0]["uri"]))
    return "\n\n".join(lines)
