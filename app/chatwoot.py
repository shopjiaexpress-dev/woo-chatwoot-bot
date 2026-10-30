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
