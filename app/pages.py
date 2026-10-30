"""Key pages: official pages linked to a topic (refunds -> /refund-policy, shipping -> /shipping, ...).

When a customer's question matches a topic, the bot reads that page and answers from it.
Pages are fetched when saved and refreshed on every scheduled sitemap sync.
"""
import re
import sqlite3
import threading
import time

import httpx

from .kb import UA, KnowledgeBase, _tokens

MAX_TEXT = 8000


def _stem(w):
    for suf in ("ings", "ing", "ies", "es", "s", "ed"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


class KeyPages:
    def __init__(self, db_path):
        self.path = db_path
        self.lock = threading.Lock()
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS key_pages(id INTEGER PRIMARY KEY, topic TEXT, keywords TEXT,"
                      " url TEXT, title TEXT, text TEXT, fetched_at REAL, error TEXT)")

    def _conn(self):
        return sqlite3.connect(self.path, timeout=30)

    @staticmethod
    def _row(r, full=False):
        d = {"id": r[0], "topic": r[1], "keywords": r[2], "url": r[3], "title": r[4],
             "chars": len(r[5] or ""), "fetched_at": r[6], "error": r[7]}
        if full:
            d["text"] = r[5] or ""
        return d

    def all(self, full=False):
        with self.lock, self._conn() as c:
            return [self._row(r, full) for r in c.execute("SELECT * FROM key_pages ORDER BY topic").fetchall()]

    def get(self, pid, full=True):
        with self.lock, self._conn() as c:
            r = c.execute("SELECT * FROM key_pages WHERE id = ?", (pid,)).fetchone()
        return self._row(r, full) if r else None

    @staticmethod
    async def fetch(url):
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as c:
            r = await c.get(url)
        if r.status_code >= 400:
            raise ValueError("Couldn't open that page (HTTP %s). Check the address." % r.status_code)
        if "html" not in r.headers.get("content-type", "html"):
            raise ValueError("That address isn't a web page.")
        title, text = KnowledgeBase.extract(r.text, url)
        text = text.strip()
        if len(text) < 40:
            raise ValueError("That page has almost no text the bot can read.")
        return title, text[:MAX_TEXT]

    async def save(self, data):
        topic = (data.get("topic") or "").strip()
        url = (data.get("url") or "").strip()
        if not topic:
            raise ValueError("Please enter a topic, e.g. Refunds")
        if not re.match(r"^https?://", url):
            raise ValueError("Please enter the full page address, starting with https://")
        keywords = (data.get("keywords") or "").strip()
        title, text = await self.fetch(url)
        pid = data.get("id")
        with self.lock, self._conn() as c:
            if pid and c.execute("SELECT 1 FROM key_pages WHERE id = ?", (pid,)).fetchone():
                c.execute("UPDATE key_pages SET topic=?, keywords=?, url=?, title=?, text=?, fetched_at=?, error=NULL "
                          "WHERE id=?", (topic, keywords, url, title, text, time.time(), pid))
            else:
                pid = c.execute("INSERT INTO key_pages(topic, keywords, url, title, text, fetched_at) "
                                "VALUES (?,?,?,?,?,?)", (topic, keywords, url, title, text, time.time())).lastrowid
        return pid

    def delete(self, pid):
        with self.lock, self._conn() as c:
            return c.execute("DELETE FROM key_pages WHERE id = ?", (pid,)).rowcount > 0

    async def refresh_all(self):
        for p in self.all():
            try:
                title, text = await self.fetch(p["url"])
                with self.lock, self._conn() as c:
                    c.execute("UPDATE key_pages SET title=?, text=?, fetched_at=?, error=NULL WHERE id=?",
                              (title, text, time.time(), p["id"]))
            except Exception as e:  # keep the last good copy
                with self.lock, self._conn() as c:
                    c.execute("UPDATE key_pages SET error=? WHERE id=?", (str(e)[:200], p["id"]))

    def match(self, text, limit=2):
        """Key pages whose topic/keywords appear in the question, best first."""
        q = (text or "").lower()
        qwords = {_stem(w) for w in _tokens(q)}
        scored = []
        for p in self.all(full=True):
            phrases = [k.strip().lower() for k in re.split(r"[,\n]", p["keywords"] + "," + p["topic"]) if k.strip()]
            score = 0
            for ph in phrases:
                words = {_stem(w) for w in _tokens(ph)}
                if not words:
                    continue
                if " " in ph and ph in q:
                    score += 3  # whole phrase, e.g. "money back"
                elif words <= qwords:
                    score += 2
                elif words & qwords:
                    score += 1
            if score:
                scored.append((score, p))
        scored.sort(key=lambda x: -x[0])
        return [p for _, p in scored[:limit]]
