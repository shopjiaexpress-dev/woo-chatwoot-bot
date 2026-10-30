"""Turn a Google Doc or a PDF into a draft guide (title + steps with screenshots).

The draft is returned to the admin page for review; nothing is saved until the admin clicks Save.
"""
import base64
import io
import re
import zipfile

import httpx

MAX_STEPS = 40
MAX_DOWNLOAD = 40 * 1024 * 1024
STEP_LABEL = re.compile(r"^(step|stage)\s*\d+\s*[:.)\-–]?\s*$", re.I)
STEP_PREFIX = re.compile(r"^((step|stage)\s*\d+\s*[:.)\-–]?|\d{1,2}\s*[.)]|[•\-–*])\s*", re.I)


class ImportError_(ValueError):
    pass


def _img_data_url(raw):
    """Normalise an image to a reasonably small PNG/JPEG data URL (max 1400px wide)."""
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    im.load()
    if im.width < 40 or im.height < 40:  # icons, bullets, spacers
        return None
    fmt = "PNG"
    if im.width > 1400:
        im = im.resize((1400, round(im.height * 1400 / im.width)))
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, "white")
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, fmt, optimize=True)
    if out.tell() > 900_000:  # big photo-like screenshot -> JPEG
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
        fmt = "JPEG"
    return "data:image/%s;base64,%s" % (fmt.lower(), base64.b64encode(out.getvalue()).decode())


class _Builder:
    """Collects text paragraphs and images in reading order and groups them into steps."""

    def __init__(self):
        self.title = None
        self.steps = []

    def text(self, t, heading=False):
        t = re.sub(r"\s+", " ", t or "").strip()
        if not t:
            return
        if self.title is None and heading and len(t) <= 120:
            self.title = t
            return
        if STEP_LABEL.match(t):  # "Step 2" on its own line: next paragraph is its text
            self.steps.append({"text": "", "image_data": None})
            return
        t = STEP_PREFIX.sub("", t)[:1000]
        if self.steps and not self.steps[-1]["text"]:
            self.steps[-1]["text"] = t
        else:
            self.steps.append({"text": t, "image_data": None})

    def image(self, raw):
        try:
            data = _img_data_url(raw)
        except Exception:
            return
        if not data:
            return
        if self.steps and not self.steps[-1]["image_data"]:
            self.steps[-1]["image_data"] = data
        else:
            self.steps.append({"text": "", "image_data": data})

    def result(self, fallback_title):
        steps = [s for s in self.steps if s["text"] or s["image_data"]][:MAX_STEPS]
        if not steps:
            raise ImportError_("No text or screenshots found in that document.")
        return {"title": self.title or fallback_title, "keywords": "", "link": "", "steps": steps,
                "note": ("Imported %d steps. Check them, add the questions customers might ask, then Save."
                         % len(steps)) + (" (Only the first %d steps were kept.)" % MAX_STEPS
                                         if len(self.steps) > MAX_STEPS else "")}


# ---------------------------------------------------------------------------- Google Docs
def _google_doc_id(url):
    m = re.search(r"/document/(?:u/\d+/)?d/([a-zA-Z0-9_-]{20,})", url or "")
    if not m:
        raise ImportError_("That doesn't look like a Google Docs link (docs.google.com/document/d/…).")
    return m.group(1)


async def from_google_doc(url):
    from bs4 import BeautifulSoup
    doc_id = _google_doc_id(url)
    export = "https://docs.google.com/document/d/%s/export?format=zip" % doc_id
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(export)
    if r.status_code in (401, 403, 404) or "zip" not in r.headers.get("content-type", ""):
        raise ImportError_("Couldn't open the Google Doc. In Google Docs click Share → General access → "
                           "\"Anyone with the link\" (Viewer), then try again.")
    if len(r.content) > MAX_DOWNLOAD:
        raise ImportError_("That document is too large (max 40 MB).")
    return parse_google_zip(r.content)


def parse_google_zip(content):
    from bs4 import BeautifulSoup
    z = zipfile.ZipFile(io.BytesIO(content))
    html_name = next((n for n in z.namelist() if n.lower().endswith(".html")), None)
    if not html_name:
        raise ImportError_("The Google Doc export was empty.")
    soup = BeautifulSoup(z.read(html_name), "lxml")
    b = _Builder()
    doc_title = (soup.title.get_text(strip=True) if soup.title else "") or "Imported guide"
    for el in soup.body.find_all(["p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"]) if soup.body else []:
        if el.name != "table" and el.find_parent("table"):
            continue  # handled together with its table
        if el.name == "table":  # e.g. a "step | screenshot" table: one step per row
            for row in el.find_all("tr"):
                b.text(" ".join(td.get_text(" ", strip=True) for td in row.find_all(["td", "th"])))
                for img in row.find_all("img"):
                    if img.get("src") in z.namelist():
                        b.image(z.read(img["src"]))
            continue
        cls = " ".join(el.get("class") or [])
        heading = el.name in ("h1", "h2") or "title" in cls
        b.text(el.get_text(" ", strip=True), heading=heading)
        for img in el.find_all("img"):
            src = img.get("src") or ""
            if src in z.namelist():
                b.image(z.read(src))
    return b.result(doc_title)


# ---------------------------------------------------------------------------- PDF
def from_pdf(data_url, filename="guide.pdf"):
    import fitz  # PyMuPDF
    m = re.match(r"^data:[\w/+.-]*;base64,(.+)$", data_url or "", re.S)
    raw = base64.b64decode(m.group(1) if m else (data_url or ""))
    if len(raw) > MAX_DOWNLOAD:
        raise ImportError_("That PDF is too large (max 40 MB).")
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception:
        raise ImportError_("That file couldn't be read as a PDF.")
    b = _Builder()
    meta_title = (doc.metadata or {}).get("title") or ""
    sizes = []
    for page in doc:
        for blk in page.get_text("dict").get("blocks", []):
            for line in blk.get("lines", []):
                for sp in line.get("spans", []):
                    if sp.get("text", "").strip():
                        sizes.append(sp.get("size", 0))
    body_size = sorted(sizes)[len(sizes) // 2] if sizes else 0

    for page in doc:
        blocks = page.get_text("dict", sort=True).get("blocks", [])
        page_has_content = False
        for blk in blocks:
            if blk.get("type") == 1:  # image block
                img = blk.get("image")
                if img:
                    b.image(img)
                    page_has_content = True
                continue
            spans = [sp for ln in blk.get("lines", []) for sp in ln.get("spans", [])]
            text = " ".join(" ".join(sp.get("text", "") for sp in ln.get("spans", [])) for ln in blk.get("lines", []))
            if not text.strip():
                continue
            size = max((sp.get("size", 0) for sp in spans), default=0)
            b.text(text, heading=body_size and size >= body_size * 1.35)
            page_has_content = True
        if not page_has_content:  # scanned page: use the whole page as a screenshot
            pix = page.get_pixmap(dpi=110)
            b.image(pix.tobytes("png"))
    title = meta_title.strip() or re.sub(r"\.pdf$", "", filename or "", flags=re.I).replace("_", " ").strip()
    return b.result(title or "Imported guide")
