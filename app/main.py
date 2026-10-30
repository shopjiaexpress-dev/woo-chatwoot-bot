"""FastAPI app: Chatwoot agent-bot webhook + admin endpoints + scheduled sitemap sync."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from . import chatwoot, config
from .bot import Bot, cards_as_text, storefront_links
from .guides import Guides
from .kb import KnowledgeBase
from .pages import KeyPages
from .woo import Woo

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

os.makedirs(config.DATA_DIR, exist_ok=True)
kb = KnowledgeBase(config.DB_PATH)
woo = Woo()
guides = Guides(config.DB_PATH, os.path.join(config.DATA_DIR, "guides"))
keypages = KeyPages(config.DB_PATH)
bot = Bot(kb, woo, guides, keypages)


async def _scheduler():
    await asyncio.sleep(5)
    if kb.stats()["pages"] == 0:
        await kb.sync()
    while config.SYNC_INTERVAL_HOURS > 0:
        await asyncio.sleep(config.SYNC_INTERVAL_HOURS * 3600)
        try:
            await kb.sync()
            await keypages.refresh_all()
        except Exception:
            log.exception("scheduled sync failed")


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_scheduler())
    yield
    task.cancel()


app = FastAPI(title="WooCommerce Chatwoot AI Bot", lifespan=lifespan)


def _check(token, expected, name):
    if not expected:
        raise HTTPException(500, "%s is not set in .env" % name)
    if token != expected:
        raise HTTPException(401, "bad token")


@app.get("/health")
async def health():
    return {"ok": True, "version": config.APP_VERSION, "kb": kb.stats(), "woocommerce": woo.enabled, "providers": config.LLM_PROVIDERS}


# ------------------------------------------------------------------ Chatwoot webhook
async def _process(event):
    try:
        conv = event.get("conversation") or {}
        account_id = (event.get("account") or {}).get("id")
        conv_id = conv.get("id")
        if not account_id or not conv_id:
            return
        sender = event.get("sender") or {}
        key = "%s:%s" % (account_id, conv_id)
        reply, handoff, cards, guide = await bot.answer(key, event.get("content") or "", sender.get("email"))
        channel = conv.get("channel") or ""
        if cards and channel and channel != "Channel::WebWidget":
            reply = (reply + "\n\n" + cards_as_text(cards)).strip()  # e.g. WhatsApp: no cards there
            cards = []
        if reply:
            await chatwoot.send_message(account_id, conv_id, reply)
        if cards:
            r = await chatwoot.send_cards(account_id, conv_id, cards)
            if r.status_code >= 400:  # older Chatwoot without cards -> plain text
                await chatwoot.send_message(account_id, conv_id, cards_as_text(cards))
        if guide:
            await _send_guide(account_id, conv_id, guide)
        if handoff:
            await chatwoot.handoff(account_id, conv_id)
    except Exception:
        log.exception("failed to process webhook")


def _step_text(i, step, total):
    text = storefront_links(step.get("text") or "")
    return ("**Step %d.** %s" % (i, text)).strip() if total > 1 else text


async def _send_guide(account_id, conv_id, guide):
    """One message per step, each with its screenshot, in order."""
    steps = guide["steps"]
    for i, st in enumerate(steps, 1):
        text = _step_text(i, st, len(steps))
        path = guides.image_path(st.get("image"))
        if path and os.path.exists(path):
            r = await chatwoot.send_attachment(account_id, conv_id, text, path)
            if r.status_code < 400:
                continue
        if text:
            await chatwoot.send_message(account_id, conv_id, text)
    if guide.get("link"):
        await chatwoot.send_message(account_id, conv_id, "\U0001F449 [%s](%s)" % (guide["title"], storefront_links(guide["link"])))


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks, token: str = ""):
    _check(token, config.WEBHOOK_TOKEN, "WEBHOOK_TOKEN")
    event = await request.json()
    if event.get("event") != "message_created":
        return {"ignored": "event"}
    if event.get("message_type") != "incoming" or event.get("private"):
        return {"ignored": "not incoming"}
    status = (event.get("conversation") or {}).get("status")
    if config.ONLY_PENDING and status and status != "pending":
        return {"ignored": "human handling"}
    background.add_task(_process, event)  # reply asynchronously so Chatwoot isn't kept waiting
    return {"ok": True}


# ------------------------------------------------------------------ admin
@app.post("/admin/sync")
async def admin_sync(background: BackgroundTasks, token: str = "", full: bool = False):
    _check(token, config.ADMIN_TOKEN, "ADMIN_TOKEN")
    async def run():
        await kb.sync(full)
        await keypages.refresh_all()
    background.add_task(run)
    return {"started": True, "hint": "watch progress at /admin/stats"}


@app.get("/admin/stats")
async def admin_stats(token: str = ""):
    _check(token, config.ADMIN_TOKEN, "ADMIN_TOKEN")
    return kb.stats()


@app.get("/admin/search")
async def admin_search(q: str, token: str = ""):
    _check(token, config.ADMIN_TOKEN, "ADMIN_TOKEN")
    return [{**h, "text": h["text"][:300]} for h in kb.search(q, 5)]


@app.post("/admin/ask")
async def admin_ask(request: Request, token: str = ""):
    """Test the bot without Chatwoot: {"message": "...", "session": "test1", "email": "optional"}"""
    _check(token, config.ADMIN_TOKEN, "ADMIN_TOKEN")
    body = await request.json()
    reply, handoff, cards, guide = await bot.answer("test:" + body.get("session", "default"), body.get("message", ""),
                                                    body.get("email"))
    g = None
    if guide:
        g = {"title": guide["title"], "link": storefront_links(guide.get("link") or ""),
             "steps": [{"text": _step_text(i, st, len(guide["steps"])),
                        "image_url": "/guide-img/" + st["image"] if st.get("image") else ""}
                       for i, st in enumerate(guide["steps"], 1)]}
    return {"reply": reply, "handoff": handoff, "cards": cards, "guide": g}


# ------------------------------------------------------------------ guides
@app.get("/admin/guides")
async def guides_list(token: str = ""):
    _check(token, config.ADMIN_TOKEN, "ADMIN_TOKEN")
    return guides.all()


@app.post("/admin/guides")
async def guides_save(request: Request, token: str = ""):
    _check(token, config.ADMIN_TOKEN, "ADMIN_TOKEN")
    try:
        gid = await asyncio.to_thread(guides.save, await request.json())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return guides.get(gid)


@app.post("/admin/guides/import")
async def guides_import(request: Request, token: str = ""):
    """Make a draft guide from {"google_url": ...} or {"pdf_data": "data:application/pdf;base64,...", "filename": ...}.
    The draft is shown in the editor for review; it is not saved here."""
    _check(token, config.ADMIN_TOKEN, "ADMIN_TOKEN")
    from . import importer
    body = await request.json()
    try:
        if body.get("google_url"):
            return await importer.from_google_doc(body["google_url"])
        if body.get("pdf_data"):
            return await asyncio.to_thread(importer.from_pdf, body["pdf_data"], body.get("filename") or "")
    except importer.ImportError_ as e:
        raise HTTPException(400, str(e))
    except Exception:
        log.exception("guide import failed")
        raise HTTPException(400, "Sorry, that document couldn't be imported.")
    raise HTTPException(400, "Paste a Google Docs link or choose a PDF file.")


# ------------------------------------------------------------------ key pages
@app.get("/admin/pages")
async def pages_list(token: str = ""):
    _check(token, config.ADMIN_TOKEN, "ADMIN_TOKEN")
    return keypages.all()


@app.post("/admin/pages")
async def pages_save(request: Request, token: str = ""):
    _check(token, config.ADMIN_TOKEN, "ADMIN_TOKEN")
    try:
        pid = await keypages.save(await request.json())
    except ValueError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError:
        raise HTTPException(400, "Couldn't reach that page. Check the address and try again.")
    p = keypages.get(pid)
    p["preview"] = p.pop("text")[:400]
    return p


@app.post("/admin/pages/refresh")
async def pages_refresh(token: str = ""):
    _check(token, config.ADMIN_TOKEN, "ADMIN_TOKEN")
    await keypages.refresh_all()
    return keypages.all()


@app.delete("/admin/pages/{pid}")
async def pages_delete(pid: int, token: str = ""):
    _check(token, config.ADMIN_TOKEN, "ADMIN_TOKEN")
    return {"deleted": keypages.delete(pid)}


@app.delete("/admin/guides/{gid}")
async def guides_delete(gid: int, token: str = ""):
    _check(token, config.ADMIN_TOKEN, "ADMIN_TOKEN")
    return {"deleted": guides.delete(gid)}


@app.get("/guide-img/{name}")
async def guide_image(name: str):
    path = guides.image_path(name)
    if not path or not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_HTML


with open(os.path.join(os.path.dirname(__file__), "admin.html"), encoding="utf-8") as _f:
    ADMIN_HTML = _f.read().replace("{{SITE_URL}}", config.SITE_URL.replace("'", "").replace('"', ""))
