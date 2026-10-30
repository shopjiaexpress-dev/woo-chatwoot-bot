"""Conversation logic: order lookup, human handoff, and answers from site content."""
import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from . import config, llm

log = logging.getLogger("bot")

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
ORDER_INTENT_RE = re.compile(
    r"(\btrack(ing)?\b|\bstatus of|where('?s| is) my|my order|my parcel|my package|order (status|number|no|id)|order\s*#|\border\b.*\d{3,})",
    re.I)
ORDER_NUM_RE = re.compile(r"(?:#|order\s*(?:no\.?|number|id)?\s*[:#]?\s*)(\d{2,12})\b", re.I)
BARE_NUM_RE = re.compile(r"\b(\d{2,12})\b")
HANDOFF_RE = re.compile(
    r"\b(human|real person|live (agent|chat|person)|talk to (someone|a person|support|agent|an agent)|"
    r"speak (to|with) (someone|a person|an agent|support)|customer (care|service)|representative|connect me)\b",
    re.I)
HANDOFF_MARK = "[HANDOFF]"


class Bot:
    def __init__(self, kb, woo):
        self.kb = kb
        self.woo = woo
        self.history = defaultdict(lambda: deque(maxlen=config.HISTORY_TURNS * 2))
        self.order_state = {}  # conv_key -> {"order":..., "email":..., "ts":...}

    # ---------------------------------------------------------------- helpers
    def _remember(self, key, role, content):
        self.history[key].append({"role": role, "content": content[:2000]})

    async def _order_flow(self, key, text, contact_email):
        state = self.order_state.get(key)
        if state and time.time() - state["ts"] > 1800:
            state = None
        num = ORDER_NUM_RE.search(text) or (BARE_NUM_RE.search(text) if state else None)
        email = EMAIL_RE.search(text)
        if not state and not ORDER_INTENT_RE.search(text):
            return None
        if state and not num and not email and not ORDER_INTENT_RE.search(text):
            self.order_state.pop(key, None)  # customer changed topic
            return None

        state = state or {"order": None, "email": contact_email}
        if num:
            state["order"] = num.group(1)
        if email:
            state["email"] = email.group(0)
        state["ts"] = time.time()

        if not self.woo.enabled:
            self.order_state.pop(key, None)
            return None
        if not state["order"]:
            self.order_state[key] = state
            return "I can check that for you. What is your order number?"
        if not state["email"]:
            self.order_state[key] = state
            return "Thanks! Please also share the email address used for order #%s, so I can verify it's yours." % state["order"]

        self.order_state.pop(key, None)
        info, err = await self.woo.order_status(state["order"], state["email"])
        if err == "not_found":
            return ("I couldn't find order #%s with that email address. Please double-check both, "
                    "or type \"agent\" to talk to our team." % state["order"])
        if not info:
            return "Sorry, I can't reach our order system right now. Type \"agent\" and our team will help you."
        lines = [
            "**Order #%s**" % info["order"],
            "Status: **%s**" % str(info["status"]).replace("-", " ").title(),
            "Placed on: %s" % info["date"],
            "Total: %s" % info["total"],
        ]
        if info["items"]:
            lines.append("Items: " + ", ".join(info["items"]))
        if info["shipping"]:
            lines.append("Shipping: " + ", ".join(s for s in info["shipping"] if s))
        if info["tracking"]:
            lines.append("Tracking: " + "; ".join(info["tracking"]))
        return "\n".join(lines)

    async def _live_products(self, hits):
        pat = re.compile(config.PRODUCT_URL_PATTERN) if config.PRODUCT_URL_PATTERN else None
        slugs = []
        for h in hits:
            if pat and pat.search(h["url"]):
                slug = urlparse(h["url"]).path.rstrip("/").rsplit("/", 1)[-1]
                if slug and slug not in slugs:
                    slugs.append(slug)
        results = await asyncio.gather(*(self.woo.product_by_slug(s) for s in slugs[:3]))
        return [r for r in results if r]

    def _system_prompt(self, context):
        return f"""You are the friendly customer-support assistant for {config.STORE_NAME} ({config.SITE_URL}).
Answer the customer's question using ONLY the information in the CONTEXT below (taken from our website and live store data).

Rules:
- Be concise: 1-4 short sentences or a short bullet list. Use markdown links like [Product name](url) when helpful.
- Never invent prices, stock, policies, delivery times, discounts or contact details. Live store data overrides website text.
- If the context does not contain the answer, say you're not sure and offer to connect them with our team.
- For order tracking, ask for their order number and email (our system will look it up).
- If the customer is angry, wants a refund/return/cancellation processed, or asks for a human, or you cannot help,
  reply briefly and end your message with {HANDOFF_MARK}
- {config.BOT_LANGUAGE_HINT}
{config.EXTRA_INSTRUCTIONS}

CONTEXT:
{context}
"""

    # ---------------------------------------------------------------- main
    async def answer(self, key, text, contact_email=None):
        """Returns (reply_text, handoff: bool)."""
        text = (text or "").strip()
        if not text:
            return None, False

        if HANDOFF_RE.search(text):
            self._remember(key, "user", text)
            return config.HANDOFF_MESSAGE, True

        order_reply = await self._order_flow(key, text, contact_email)
        if order_reply:
            self._remember(key, "user", text)
            self._remember(key, "assistant", order_reply)
            return order_reply, False

        # Short follow-ups ("and in blue?") need the previous question for search
        query = text
        prev_user = [m["content"] for m in self.history[key] if m["role"] == "user"]
        if len(text.split()) < 6 and prev_user:
            query = prev_user[-1] + " " + text

        hits = await asyncio.to_thread(self.kb.search, query, 5)
        products = await self._live_products(hits)

        parts = []
        for p in products:
            parts.append("[LIVE STORE DATA] %s | price: %s | stock: %s | %s\n%s"
                         % (p["name"], p["price"], p["stock"], p["url"], p["summary"]))
        for h in hits:
            parts.append("[PAGE] %s | %s\n%s" % (h["title"], h["url"], h["text"]))
        context = "\n\n---\n\n".join(parts) or "(no matching content found on the website)"

        messages = list(self.history[key]) + [{"role": "user", "content": text}]
        reply, provider = await llm.chat(self._system_prompt(context), messages)

        handoff = False
        if reply:
            if HANDOFF_MARK in reply:
                handoff = True
                reply = reply.replace(HANDOFF_MARK, "").strip() or config.HANDOFF_MESSAGE
            log.info("answered via %s", provider)
        else:
            # All AI providers unavailable -> search-only answer
            if hits:
                seen, links = set(), []
                for h in hits:
                    if h["url"] not in seen:
                        seen.add(h["url"])
                        links.append("- [%s](%s)" % (h["title"], h["url"]))
                reply = ("Here's what I found on our website that may help:\n" + "\n".join(links[:3]) +
                         "\n\nType \"agent\" if you'd like to talk to our team.")
            else:
                reply, handoff = config.HANDOFF_MESSAGE, True

        self._remember(key, "user", text)
        self._remember(key, "assistant", reply)
        return reply, handoff
