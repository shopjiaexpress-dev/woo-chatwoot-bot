"""Knowledge base: crawls sitemap.xml and indexes page text in SQLite FTS5 (BM25 search).

No embedding model needed, so it runs in ~50 MB RAM.
"""
import asyncio
import gzip
import json
import logging
import re
import sqlite3
import threading
import time
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup

from . import config

log = logging.getLogger("kb")

UA = "Mozilla/5.0 (compatible; WooChatwootBot/1.0; +knowledge-sync)"

STOPWORDS = set("""
a an the and or but if then so to of in on at by for with from up about into over after before is are was were be been being
am do does did have has had i me my we our you your he she it its they them their this that these those what which who whom
how when where why can could would should will shall may might must not no yes please hi hello hey thanks thank ok okay
any some all just also very more most much many there here than too get got want need know tell show give let like
""".split())


def _tokens(text):
    words = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    return [w for w in words if len(w) > 1 and w not in STOPWORDS]


class KnowledgeBase:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.syncing = False
        self.last_sync = None
        with self._conn() as c:
            c.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5("
                "url UNINDEXED, title, body, tokenize='porter unicode61 remove_diacritics 2')"
            )
            c.execute("CREATE TABLE IF NOT EXISTS pages(url TEXT PRIMARY KEY, lastmod TEXT, title TEXT, fetched_at REAL)")

    def _conn(self):
        return sqlite3.connect(self.path, timeout=30)

    # ------------------------------------------------------------------ search
    def search(self, text, k=5):
        toks = _tokens(text)[:20]
        if not toks:
            return []
        query = " OR ".join('"%s"' % t.replace('"', "") for t in toks)
        with self.lock, self._conn() as c:
            try:
                rows = c.execute(
                    "SELECT url, title, body, bm25(chunks, 0.0, 4.0, 1.0) AS s FROM chunks "
                    "WHERE chunks MATCH ? ORDER BY s LIMIT ?",
                    (query, k * 3),
                ).fetchall()
            except sqlite3.OperationalError as e:
                log.warning("search failed: %s", e)
                return []
        # keep max 2 chunks per page so one page doesn't crowd out everything
        out, per_url = [], {}
        for url, title, body, score in rows:
            if per_url.get(url, 0) >= 2:
                continue
            per_url[url] = per_url.get(url, 0) + 1
            out.append({"url": url, "title": title, "text": body, "score": score})
            if len(out) >= k:
                break
        return out

    def stats(self):
        with self.lock, self._conn() as c:
            pages = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            chunks = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"pages": pages, "chunks": chunks, "syncing": self.syncing, "last_sync": self.last_sync}

    # ------------------------------------------------------------------ storage
    def _known(self):
        with self.lock, self._conn() as c:
            return dict(c.execute("SELECT url, lastmod FROM pages").fetchall())

    def _store(self, url, lastmod, title, chunks):
        with self.lock, self._conn() as c:
            c.execute("DELETE FROM chunks WHERE url = ?", (url,))
            c.executemany("INSERT INTO chunks(url, title, body) VALUES (?,?,?)", [(url, title, ch) for ch in chunks])
            c.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?,?)", (url, lastmod or "", title, time.time()))

    def _remove(self, urls):
        with self.lock, self._conn() as c:
            for u in urls:
                c.execute("DELETE FROM chunks WHERE url = ?", (u,))
                c.execute("DELETE FROM pages WHERE url = ?", (u,))

    # ------------------------------------------------------------------ crawling
    async def _sitemap_urls(self, client, url, depth=0, seen=None):
        seen = seen if seen is not None else set()
        if depth > 3 or url in seen:
            return []
        seen.add(url)
        try:
            r = await client.get(url)
            r.raise_for_status()
            data = r.content
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            root = ET.fromstring(data)
        except Exception as e:
            log.warning("sitemap %s failed: %s", url, e)
            return []

        def tag(el):
            return el.tag.split("}")[-1]

        results = []
        if tag(root) == "sitemapindex":
            for sm in root:
                loc = next((x.text.strip() for x in sm if tag(x) == "loc" and x.text), None)
                if loc:
                    results += await self._sitemap_urls(client, loc, depth + 1, seen)
        else:
            for u in root:
                loc = next((x.text.strip() for x in u if tag(x) == "loc" and x.text), None)
                lastmod = next((x.text.strip() for x in u if tag(x) == "lastmod" and x.text), "")
                if loc:
                    results.append((loc, lastmod))
        return results

    @staticmethod
    def extract(html, url):
        """Return (title, text) from a page, keeping main content and product data."""
        soup = BeautifulSoup(html, "lxml")
        title = (soup.title.get_text(strip=True) if soup.title else "") or url
        extra = []

        desc = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        if desc and desc.get("content"):
            extra.append(desc["content"].strip())

        # Product structured data (price, stock, sku) that WooCommerce/Yoast/RankMath print as JSON-LD
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(s.string or "")
            except Exception:
                continue
            items = data.get("@graph", [data]) if isinstance(data, dict) else data
            for it in items if isinstance(items, list) else []:
                if not isinstance(it, dict):
                    continue
                t = it.get("@type")
                if t == "Product" or (isinstance(t, list) and "Product" in t):
                    offers = it.get("offers") or {}
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get("price") or offers.get("lowPrice")
                    if not price and isinstance(offers.get("priceSpecification"), list) and offers["priceSpecification"]:
                        price = offers["priceSpecification"][0].get("price")
                    cur = offers.get("priceCurrency", "")
                    avail = str(offers.get("availability", "")).rsplit("/", 1)[-1]
                    line = "Product: %s." % it.get("name", "")
                    if price:
                        line += " Price: %s %s." % (price, cur)
                    if avail:
                        line += " Availability: %s." % avail
                    if it.get("sku"):
                        line += " SKU: %s." % it["sku"]
                    extra.append(line)

        for t in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "form", "iframe", "aside", "button"]):
            t.decompose()
        for sel in [".site-header", ".site-footer", "#masthead", "#colophon", ".menu", ".breadcrumb",
                    ".woocommerce-breadcrumb", ".related", ".upsells", ".cookie", "#cookie-notice"]:
            for t in soup.select(sel):
                t.decompose()
        main = (soup.find("main") or soup.find(attrs={"role": "main"}) or soup.find("article")
                or soup.find(id="content") or soup.body or soup)
        lines, last = [], None
        for line in main.get_text("\n", strip=True).split("\n"):
            line = re.sub(r"\s+", " ", line).strip()
            if line and line != last:
                lines.append(line)
            last = line
        text = "\n".join(extra + lines)
        return title, text

    @staticmethod
    def chunk(text, size=900, overlap=150):
        paras = [p for p in text.split("\n") if p.strip()]
        chunks, cur = [], ""
        for p in paras:
            if len(cur) + len(p) + 1 > size and cur:
                chunks.append(cur.strip())
                cur = cur[-overlap:] + "\n" + p
            else:
                cur += "\n" + p
            while len(cur) > size * 2:  # very long paragraph
                chunks.append(cur[:size].strip())
                cur = cur[size - overlap:]
        if cur.strip():
            chunks.append(cur.strip())
        return chunks

    async def sync(self, full=False):
        """Crawl the sitemap. Only re-fetches pages whose <lastmod> changed (unless full=True)."""
        if self.syncing:
            return {"status": "already running"}
        if not config.SITEMAP_URL:
            return {"status": "SITEMAP_URL not set"}
        self.syncing = True
        started = time.time()
        fetched = skipped = failed = 0
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as client:
                urls = await self._sitemap_urls(client, config.SITEMAP_URL)
                inc = re.compile(config.SITEMAP_INCLUDE) if config.SITEMAP_INCLUDE else None
                exc = re.compile(config.SITEMAP_EXCLUDE) if config.SITEMAP_EXCLUDE else None
                dedup = {}
                for u, lm in urls:
                    if inc and not inc.search(u):
                        continue
                    if exc and exc.search(u):
                        continue
                    dedup[u] = lm
                urls = list(dedup.items())[: config.MAX_PAGES]
                log.info("sitemap: %d urls to consider", len(urls))

                known = self._known()
                gone = set(known) - set(dict(urls))
                if gone and urls:  # don't wipe the index if the sitemap failed to load
                    self._remove(gone)

                sem = asyncio.Semaphore(config.CRAWL_CONCURRENCY)

                async def work(u, lm):
                    nonlocal fetched, skipped, failed
                    if not full and u in known and lm and known[u] == lm:
                        skipped += 1
                        return
                    async with sem:
                        try:
                            r = await client.get(u)
                            r.raise_for_status()
                            if "html" not in r.headers.get("content-type", "html"):
                                return
                            title, text = self.extract(r.text, u)
                            chunks = self.chunk(text)
                            if chunks:
                                await asyncio.to_thread(self._store, u, lm, title, chunks)
                            fetched += 1
                        except Exception as e:
                            failed += 1
                            log.warning("fetch %s failed: %s", u, e)
                        await asyncio.sleep(0.2)  # be gentle with the shop server

                await asyncio.gather(*(work(u, lm) for u, lm in urls))
                result = {"status": "ok", "urls": len(urls), "fetched": fetched, "unchanged": skipped,
                          "failed": failed, "removed": len(gone) if urls else 0,
                          "seconds": round(time.time() - started, 1)}
                self.last_sync = time.strftime("%Y-%m-%d %H:%M:%S")
                log.info("sync done: %s", result)
                return result
        finally:
            self.syncing = False
