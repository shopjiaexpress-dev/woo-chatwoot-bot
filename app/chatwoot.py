"""Minimal Chatwoot API client used by the agent bot."""
import logging

import httpx

from . import config

log = logging.getLogger("chatwoot")


async def _post(path, payload):
    url = "%s/api/v1/accounts%s" % (config.CHATWOOT_URL, path)
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, json=payload, headers={"api_access_token": config.CHATWOOT_BOT_TOKEN})
        if r.status_code >= 400:
            log.warning("POST %s -> %s %s", path, r.status_code, r.text[:200])
        return r


async def send_message(account_id, conversation_id, content):
    return await _post("/%s/conversations/%s/messages" % (account_id, conversation_id),
                       {"content": content, "message_type": "outgoing", "private": False})


async def handoff(account_id, conversation_id):
    """Pending -> open: the conversation shows up for human agents and the bot goes quiet."""
    return await _post("/%s/conversations/%s/toggle_status" % (account_id, conversation_id), {"status": "open"})


async def send_cards(account_id, conversation_id, items):
    """Product cards (image, title, price, button) - rendered by the Chatwoot website widget."""
    return await _post("/%s/conversations/%s/messages" % (account_id, conversation_id),
                       {"content": "Products", "message_type": "outgoing", "private": False,
                        "content_type": "cards", "content_attributes": {"items": items}})


async def send_attachment(account_id, conversation_id, content, file_path):
    """Message with an image (e.g. a guide screenshot) uploaded to Chatwoot."""
    import mimetypes
    import os
    url = "%s/api/v1/accounts/%s/conversations/%s/messages" % (config.CHATWOOT_URL, account_id, conversation_id)
    mime = mimetypes.guess_type(file_path)[0] or "image/png"
    async with httpx.AsyncClient(timeout=60) as c:
        with open(file_path, "rb") as f:
            r = await c.post(url, headers={"api_access_token": config.CHATWOOT_BOT_TOKEN},
                             data={"content": content, "message_type": "outgoing", "private": "false"},
                             files={"attachments[]": (os.path.basename(file_path), f, mime)})
    if r.status_code >= 400:
        log.warning("attachment upload -> %s %s", r.status_code, r.text[:200])
    return r
