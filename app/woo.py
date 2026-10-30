"""WooCommerce REST API client (read-only: products + orders)."""
import logging
import re
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

    @staticmethod
    def _product_summary(p):
        stock = p.get("stock_status", "")
        if p.get("manage_stock") and p.get("stock_quantity") is not None:
            stock += " (%s left)" % p["stock_quantity"]
        price = p.get("price") or ""
        if p.get("on_sale") and p.get("regular_price"):
            price = "%s (on sale, was %s)" % (p.get("sale_price") or price, p["regular_price"])
        return {
            "name": unescape(p.get("name", "")),
            "price": price,
            "stock": stock,
            "url": p.get("permalink", ""),
            "summary": _strip(p.get("short_description") or p.get("description")),
        }

    async def product_by_slug(self, slug):
        if not self.enabled:
            return None
        try:
            data = await self._get("/products", {"slug": slug, "status": "publish"})
            return self._product_summary(data[0]) if data else None
        except Exception as e:
            log.warning("product %s: %s", slug, e)
            return None

    async def search_products(self, query, limit=4):
        if not self.enabled or not query:
            return []
        try:
            data = await self._get("/products", {"search": query, "per_page": limit, "status": "publish"})
            return [self._product_summary(p) for p in data or []]
        except Exception as e:
            log.warning("product search: %s", e)
            return []

    async def order_status(self, order_id, email):
        """Returns (summary_dict or None, error_code). Only returns data when the email matches."""
        if not self.enabled:
            return None, "disabled"
        try:
            o = await self._get("/orders/%s" % int(order_id))
        except Exception as e:
            log.warning("order %s: %s", order_id, e)
            return None, "error"
        if not o:
            return None, "not_found"
        if (o.get("billing", {}).get("email") or "").strip().lower() != email.strip().lower():
            return None, "not_found"  # don't reveal that the order exists

        tracking = []
        for m in o.get("meta_data", []):
            key, val = m.get("key", ""), m.get("value")
            if key == "_wc_shipment_tracking_items" and isinstance(val, list):  # WooCommerce Shipment Tracking
                for t in val:
                    tracking.append("%s %s %s" % (t.get("tracking_provider") or t.get("custom_tracking_provider", ""),
                                                  t.get("tracking_number", ""), t.get("custom_tracking_link", "")))
            elif any(k in key.lower() for k in ("tracking_number", "tracking_url", "awb", "_aftership_tracking")) and val:
                tracking.append("%s: %s" % (key.strip("_"), val))
        return {
            "order": o.get("number") or o.get("id"),
            "status": o.get("status"),
            "date": (o.get("date_created") or "")[:10],
            "total": "%s %s" % (o.get("total"), o.get("currency")),
            "payment": o.get("payment_method_title"),
            "items": ["%s x%s" % (unescape(i.get("name", "")), i.get("quantity")) for i in o.get("line_items", [])],
            "shipping": [s.get("method_title") for s in o.get("shipping_lines", [])],
            "tracking": [t.strip() for t in tracking if t.strip()],
        }, "ok"
