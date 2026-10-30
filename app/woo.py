"""WooCommerce REST API client (read-only: products + orders)."""
import logging
import re
from datetime import datetime
from html import unescape

import httpx

from . import config

log = logging.getLogger("woo")


def _strip(html, limit=300):
    text = unescape(re.sub(r"<[^>]+>", " ", html or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


class Woo:
    def __init__(self):
        self.enabled = bool(config.WOO_URL and config.WOO_KEY and config.WOO_SECRET)
        self.base = config.WOO_URL + "/wp-json/wc/v3"
        self.symbol = config.CURRENCY_SYMBOL or None

    async def _get(self, path, params=None):
        # Keys as query params: works over HTTPS even on hosts that strip the Authorization header
        p = dict(params or {})
        p.update(consumer_key=config.WOO_KEY, consumer_secret=config.WOO_SECRET)
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(self.base + path, params=p)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()

    async def currency(self):
        """Store currency symbol (e.g. ₹), read once from WooCommerce."""
        if self.symbol is None:
            self.symbol = ""
            try:
                data = await self._get("/data/currencies/current")
                self.symbol = unescape((data or {}).get("symbol") or (data or {}).get("code") or "")
            except Exception as e:
                log.warning("currency lookup failed: %s", e)
        return self.symbol

    def money(self, amount):
        if amount in (None, ""):
            return ""
        try:
            f = float(amount)
            amount = str(int(f)) if f.is_integer() else "%.2f" % f
        except ValueError:
            pass
        return "%s%s" % (self.symbol or "", amount)

    def _product_summary(self, p):
        status = p.get("stock_status", "")
        stock = {"instock": "In stock", "outofstock": "Out of stock", "onbackorder": "On backorder"}.get(status, status)
        if status == "instock" and p.get("manage_stock") and p.get("stock_quantity") is not None \
                and p["stock_quantity"] <= 5:
            stock = "Only %s left" % p["stock_quantity"]
        price = self.money(p.get("price"))
        if p.get("on_sale") and p.get("regular_price"):
            price = "%s (was %s)" % (self.money(p.get("sale_price") or p.get("price")), self.money(p["regular_price"]))
        images = p.get("images") or []
        return {
            "name": unescape(p.get("name", "")),
            "slug": p.get("slug", ""),
            "price": price,
            "stock": stock,
            "image": images[0].get("src", "") if images else "",
            "url": (config.PRODUCT_LINK_TEMPLATE.format(site=config.SITE_URL, slug=p["slug"])
                    if config.SITE_URL and p.get("slug") else p.get("permalink", "")),
            "summary": _strip(p.get("short_description") or p.get("description")),
        }

    async def product_by_slug(self, slug):
        if not self.enabled:
            return None
        try:
            await self.currency()
            data = await self._get("/products", {"slug": slug, "status": "publish"})
            return self._product_summary(data[0]) if data else None
        except Exception as e:
            log.warning("product %s: %s", slug, e)
            return None

    async def search_products(self, query, limit=4):
        if not self.enabled or not query:
            return []
        try:
            await self.currency()
            data = await self._get("/products", {"search": query, "per_page": limit, "status": "publish"})
            return [self._product_summary(p) for p in data or []]
        except Exception as e:
            log.warning("product search: %s", e)
            return []

    async def find_order(self, ref, email):
        """Find an order by what the customer typed: '1008', '#1008', 'JES1008', 'JES-1008'.

        Works with plain WooCommerce IDs and with custom order numbers
        (Sequential Order Numbers and similar plugins). Only returns an order whose
        billing email matches.
        """
        ref = ref.strip().lstrip("#").upper()
        digits = re.sub(r"\D", "", ref)
        email = email.strip().lower()

        def norm(x):
            return re.sub(r"[^A-Z0-9]", "", str(x or "").upper())

        def is_match(o):
            if (o.get("billing", {}).get("email") or "").strip().lower() != email:
                return False
            num, oid = norm(o.get("number")), str(o.get("id"))
            return norm(ref) in (num, oid) or (ref.isdigit() and re.sub(r"\D", "", num) == ref)

        # 1) the digits are often the real order ID (JES1008 -> 1008)
        if digits:
            o = await self._get("/orders/%s" % int(digits))
            if o and is_match(o):
                return o
        # 2) otherwise look through this customer's orders (WooCommerce search matches billing email)
        for page in (1, 2):
            orders = await self._get("/orders", {"search": email, "per_page": 50, "page": page,
                                                 "orderby": "date", "order": "desc"}) or []
            for o in orders:
                if is_match(o):
                    return o
            if len(orders) < 50:
                break
        return None

    async def order_status(self, order_ref, email):
        """Returns (summary_dict or None, error_code). Only returns data when the email matches."""
        if not self.enabled:
            return None, "disabled"
        try:
            o = await self.find_order(str(order_ref), email)
        except Exception as e:
            log.warning("order %s: %s", order_ref, e)
            return None, "error"
        if not o:
            return None, "not_found"  # also when the email doesn't match: don't reveal the order exists

        tracking = []
        for m in o.get("meta_data", []):
            key, val = m.get("key", ""), m.get("value")
            if key == "_wc_shipment_tracking_items" and isinstance(val, list):  # WooCommerce Shipment Tracking
                for t in val:
                    tracking.append("%s %s %s" % (t.get("tracking_provider") or t.get("custom_tracking_provider", ""),
                                                  t.get("tracking_number", ""), t.get("custom_tracking_link", "")))
            elif any(k in key.lower() for k in ("tracking_number", "tracking_url", "awb", "_aftership_tracking")) and val:
                tracking.append("%s: %s" % (key.strip("_"), val))
        await self.currency()
        date = (o.get("date_created") or "")[:10]
        try:
            date = datetime.strptime(date, "%Y-%m-%d").strftime("%d %b %Y")
        except ValueError:
            pass
        return {
            "order": o.get("number") or o.get("id"),
            "status": o.get("status"),
            "date": date,
            "total": self.money(o.get("total")) if self.symbol else "%s %s" % (o.get("total"), o.get("currency")),
            "payment": o.get("payment_method_title"),
            "items": ["%s \u00d7 %s" % (unescape(i.get("name", "")), i.get("quantity")) for i in o.get("line_items", [])],
            "shipping": [s.get("method_title") for s in o.get("shipping_lines", [])],
            "tracking": [t.strip() for t in tracking if t.strip()],
        }, "ok"
