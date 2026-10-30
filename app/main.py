"""FastAPI app: Chatwoot agent-bot webhook + admin endpoints + scheduled sitemap sync."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import chatwoot, config
from .bot import Bot
from .kb import KnowledgeBase
from .woo import Woo

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

os.makedirs(config.DATA_DIR, exist_ok=True)
kb = KnowledgeBase(config.DB_PATH)
woo = Woo()
bot = Bot(kb, woo)


async def _scheduler():
    await asyncio.sleep(5)
    if kb.stats()["pages"] == 0:
        await kb.sync()
    while config.SYNC_INTERVAL_HOURS > 0:
        await asyncio.sleep(config.SYNC_INTERVAL_HOURS * 3600)
        try:
            await kb.sync()
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
    return {"ok": True, "kb": kb.stats(), "woocommerce": woo.enabled, "providers": config.LLM_PROVIDERS}


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
        reply, handoff = await bot.answer(key, event.get("content") or "", sender.get("email"))
        if reply:
            await chatwoot.send_message(account_id, conv_id, reply)
        if handoff:
            await chatwoot.handoff(account_id, conv_id)
    except Exception:
        log.exception("failed to process webhook")


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
    background.add_task(kb.sync, full)
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
    reply, handoff = await bot.answer("test:" + body.get("session", "default"), body.get("message", ""), body.get("email"))
    return {"reply": reply, "handoff": handoff}


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_HTML


ADMIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bot admin</title><style>
body{font-family:system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color:#222}
input,button,textarea{font:inherit;padding:8px;border:1px solid #bbb;border-radius:6px}
button{background:#1f6feb;color:#fff;border:0;cursor:pointer} .row{display:flex;gap:8px;margin:8px 0}
.row input{flex:1} pre{background:#f4f4f4;padding:12px;border-radius:6px;white-space:pre-wrap}
.msg{padding:8px 12px;border-radius:8px;margin:6px 0} .u{background:#e8f0fe} .b{background:#f1f1f1}
</style></head><body>
<h2>WooCommerce Chatwoot Bot — admin</h2>
<div class="row"><input id="t" type="password" placeholder="ADMIN_TOKEN"></div>
<div class="row"><button onclick="sync(false)">Sync sitemap (changed pages)</button>
<button onclick="sync(true)">Full re-sync</button><button onclick="stats()">Stats</button></div>
<pre id="out">Enter your ADMIN_TOKEN, then click Stats.</pre>
<h3>Test chat</h3><div id="chat"></div>
<div class="row"><input id="m" placeholder="Ask something a customer would ask…" onkeydown="if(event.key==='Enter')ask()">
<button onclick="ask()">Send</button></div>
<script>
const T=()=>encodeURIComponent(document.getElementById('t').value), out=document.getElementById('out');
async function stats(){out.textContent=JSON.stringify(await (await fetch('/admin/stats?token='+T())).json(),null,2)}
async function sync(f){out.textContent=JSON.stringify(await (await fetch('/admin/sync?full='+f+'&token='+T(),{method:'POST'})).json(),null,2)}
function add(c,t){const d=document.createElement('div');d.className='msg '+c;d.textContent=t;document.getElementById('chat').appendChild(d)}
async function ask(){const m=document.getElementById('m');const v=m.value.trim();if(!v)return;m.value='';add('u',v);
const r=await (await fetch('/admin/ask?token='+T(),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:v,session:'admin'})})).json();
add('b',(r.reply||r.detail||'(no reply)')+(r.handoff?'  → [handed to human]':''))}
</script></body></html>"""
