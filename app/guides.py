"""Step-by-step guides with screenshots (e.g. "How to cancel an order"), managed from /admin.

Stored in SQLite next to the knowledge base; screenshots are saved in /data/guides.
"""
import base64
import json
import os
import re
import sqlite3
import threading
import time
import uuid

from .kb import _tokens

IMG_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp", "image/gif": "gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SAFE_NAME = re.compile(r"^[a-f0-9]{32}\.(png|jpg|webp|gif)$")


class Guides:
    def __init__(self, db_path, img_dir):
        self.path = db_path
        self.img_dir = img_dir
        self.lock = threading.Lock()
        os.makedirs(img_dir, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS guides(id INTEGER PRIMARY KEY, title TEXT, keywords TEXT,"
                      " link TEXT, steps TEXT, updated REAL)")
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS guides_fts USING fts5("
                      "title, keywords, body, tokenize='porter unicode61 remove_diacritics 2')")

    def _conn(self):
        return sqlite3.connect(self.path, timeout=30)

    @staticmethod
    def _row(r):
        return {"id": r[0], "title": r[1], "keywords": r[2], "link": r[3], "steps": json.loads(r[4] or "[]")}

    def image_path(self, name):
        return os.path.join(self.img_dir, name) if name and SAFE_NAME.match(name) else None

    def _save_image(self, data_url):
        m = re.match(r"^data:([\w/+.-]+);base64,(.+)$", data_url or "", re.S)
        if not m or m.group(1).lower() not in IMG_TYPES:
            raise ValueError("Screenshots must be PNG, JPG, WEBP or GIF images")
        raw = base64.b64decode(m.group(2))
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("Each screenshot must be smaller than 5 MB")
        name = "%s.%s" % (uuid.uuid4().hex, IMG_TYPES[m.group(1).lower()])
        with open(os.path.join(self.img_dir, name), "wb") as f:
            f.write(raw)
        return name

    # ------------------------------------------------------------------ CRUD
    def all(self):
        with self.lock, self._conn() as c:
            return [self._row(r) for r in c.execute("SELECT * FROM guides ORDER BY title").fetchall()]

    def get(self, gid):
        with self.lock, self._conn() as c:
            r = c.execute("SELECT * FROM guides WHERE id = ?", (gid,)).fetchone()
        return self._row(r) if r else None

    def save(self, data):
        title = (data.get("title") or "").strip()
        if not title:
            raise ValueError("Please give the guide a title")
        steps = []
        for s in data.get("steps") or []:
            text = (s.get("text") or "").strip()
            image = s.get("image") if self.image_path(s.get("image")) else None
            if s.get("image_data"):
                image = self._save_image(s["image_data"])
            if text or image:
                steps.append({"text": text, "image": image})
        if not steps:
            raise ValueError("Add at least one step")
        keywords = (data.get("keywords") or "").strip()
        link = (data.get("link") or "").strip()
        body = " ".join(s["text"] for s in steps)
        gid = data.get("id")
        old = self.get(gid) if gid else None
        with self.lock, self._conn() as c:
            if old:
                c.execute("UPDATE guides SET title=?, keywords=?, link=?, steps=?, updated=? WHERE id=?",
                          (title, keywords, link, json.dumps(steps), time.time(), gid))
                c.execute("DELETE FROM guides_fts WHERE rowid = ?", (gid,))
            else:
                gid = c.execute("INSERT INTO guides(title, keywords, link, steps, updated) VALUES (?,?,?,?,?)",
                                (title, keywords, link, json.dumps(steps), time.time())).lastrowid
            c.execute("INSERT INTO guides_fts(rowid, title, keywords, body) VALUES (?,?,?,?)",
                      (gid, title, keywords, body))
        if old:  # remove screenshots that are no longer used
            keep = {s["image"] for s in steps}
            for s in old["steps"]:
                if s.get("image") and s["image"] not in keep:
                    self._unlink(s["image"])
        return gid

    def delete(self, gid):
        g = self.get(gid)
        if not g:
            return False
        with self.lock, self._conn() as c:
            c.execute("DELETE FROM guides WHERE id = ?", (gid,))
            c.execute("DELETE FROM guides_fts WHERE rowid = ?", (gid,))
        for s in g["steps"]:
            self._unlink(s.get("image"))
        return True

    def _unlink(self, name):
        p = self.image_path(name)
        if p and os.path.exists(p):
            os.remove(p)

    # ------------------------------------------------------------------ search
    def search(self, text, k=2):
        toks = _tokens(text)[:20]
        if not toks:
            return []
        query = " OR ".join('"%s"' % t.replace('"', "") for t in toks)
        with self.lock, self._conn() as c:
            try:
                rows = c.execute("SELECT rowid, bm25(guides_fts, 6.0, 4.0, 1.0) AS s FROM guides_fts "
                                 "WHERE guides_fts MATCH ? ORDER BY s LIMIT ?", (query, k)).fetchall()
            except sqlite3.OperationalError:
                return []
        out = []
        for gid, score in rows:
            g = self.get(gid)
            if g:
                g["score"] = score
                out.append(g)
        return out

    def strong_match(self, text):
        """Best guide when most of the guide's title/keyword words appear in the question (used without AI)."""
        words = set(_tokens(text))
        for g in self.search(text, 3):
            key = set(_tokens(g["title"] + " " + g["keywords"]))
            if key and len(words & key) >= min(2, len(key)):
                return g
        return None
