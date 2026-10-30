# WooCommerce AI Bot for Chatwoot (free, self-hosted)

A small bot (about 60–150 MB RAM) that connects to your existing Chatwoot as an **Agent Bot** and:

- **Learns your website automatically from `sitemap.xml`.** It reads pages, products and policies, and re-syncs every 24 hours. Only pages that changed are fetched again.
- **Answers with live WooCommerce data.** Prices, sale prices and stock come from your store at the moment the customer asks.
- **Looks up orders.** The customer gives an order number and email, and the bot replies with status, items, total and tracking (supports the WooCommerce Shipment Tracking plugin).
- **Hands the chat to a human.** This happens when the customer asks for one, wants a refund, is upset, or the bot doesn't know the answer.
- **Uses only free AI.** The bot tries Gemini first, then Groq, then OpenRouter, with optional local Ollama. If every provider is down or out of limits, it still replies with the best-matching pages from your website.

```
Customer ─► Chatwoot widget ─► webhook ─► this bot ─┬─► search index of your sitemap pages (SQLite)
                         ▲                          ├─► WooCommerce REST API (live products/orders)
                         └──── reply / handoff ◄────┴─► free AI (Gemini → Groq → OpenRouter → Ollama)
```

---

## Step 1: Get the free keys (about 10 min)

1. **WooCommerce API key:** in WordPress, go to **WooCommerce → Settings → Advanced → REST API → Add key**. Set Permissions to **Read**, then copy the *Consumer key* (`ck_…`) and *Consumer secret* (`cs_…`).
2. **Gemini (main AI, free):** go to https://aistudio.google.com/apikey and click **Create API key**.
3. **Groq (backup AI, free, optional):** go to https://console.groq.com/keys and create a key.
4. **OpenRouter (second backup, optional):** go to https://openrouter.ai/keys and create a key.

> Get at least one AI key; two is better. Free-tier limits are set by each provider and can change.

## Step 2: Install the bot on your VPS

You need Docker installed. It's already there if you installed Chatwoot with Docker. If not, run `curl -fsSL https://get.docker.com | sh`.

```bash
cd /opt
git clone https://github.com/YOUR_USERNAME/woo-chatwoot-bot.git
# private repo? use: git clone https://YOUR_USERNAME:YOUR_GITHUB_TOKEN@github.com/YOUR_USERNAME/woo-chatwoot-bot.git
cd woo-chatwoot-bot
cp .env.example .env
nano .env            # fill in your values (see comments inside)
openssl rand -hex 24 # run twice → paste as WEBHOOK_TOKEN and ADMIN_TOKEN
docker compose up -d --build
docker compose logs -f   # watch the first sitemap sync
```

Check it's working: open `http://YOUR_SERVER_IP:8100/health`. You should see `"pages": <number>`.

## Step 3: Give the bot an HTTPS address (recommended)

Add a subdomain such as `bot.yourshop.com` pointing to your server. Then add this to Nginx, which your Chatwoot install probably already uses:

```nginx
server {
    server_name bot.yourshop.com;
    location / { proxy_pass http://127.0.0.1:8100; proxy_set_header Host $host; }
}
```

Then run `sudo certbot --nginx -d bot.yourshop.com`. In `docker-compose.yml`, change the port line to `"127.0.0.1:8100:8000"` so the bot is only reachable through Nginx, and run `docker compose up -d`.

## Step 4: Connect it to Chatwoot

1. In Chatwoot, go to **Settings → Bots → Add Bot**.
   - Name: e.g. *Shop Assistant*
   - Webhook URL: `https://bot.yourshop.com/webhook?token=YOUR_WEBHOOK_TOKEN`
2. Open the bot you created and copy its **access token**. Put it in `.env` as `CHATWOOT_BOT_TOKEN`, then run `docker compose up -d`.
   *If your Chatwoot version doesn't show a bot token, the bot can be created through the Super Admin console (`/super_admin` → Agent Bots), which shows the token.*
3. Go to **Settings → Inboxes → (your website inbox) → Bot Configuration**, select your bot, and click **Save**.

That's it. New conversations now start as **Pending** and the bot answers them. When the bot hands over, the conversation becomes **Open** and appears for your agents, and the bot stops replying. To give a conversation back to the bot, an agent sets it to **Pending** again.

## Step 5: Test it

Open `https://bot.yourshop.com/admin` and enter your `ADMIN_TOKEN`. From there you can:

- see how many pages are indexed (**Stats**)
- re-sync your sitemap now, for example after adding products
- **chat with the bot** to test answers before customers see them

Then try the live chat widget on your website.

---

## Updating the bot later

```bash
cd /opt/woo-chatwoot-bot
git pull
docker compose up -d --build
```

Your `.env` and `data/` folder are never touched by `git pull`, because they're listed in `.gitignore`.

## Teaching the bot: step-by-step guides with screenshots

Use guides for things like "How to cancel an order" or "How to check order status".

1. Open `/admin` on the bot's address, enter your `ADMIN_TOKEN`, and go to the **Guides** tab.
2. Create a guide in one of three ways:
   - **+ New guide:** type each step and attach a screenshot.
   - **Import Google Doc:** paste the link. The doc must be shared as **"Anyone with the link"**.
   - **Import PDF:** choose the file.

   When you import, each paragraph becomes a step, and each picture becomes that step's screenshot. A page with no text, such as a scanned page, becomes a screenshot by itself.
3. Fill in **Customers might ask…** with words and phrases like `cancel order, cancel my order, stop my order`. Optionally add a link to show at the end. Then click **Save guide**.
4. Test it in the **Test chat** tab.

When a customer asks, the bot replies with one short sentence. It then sends each step as its own message with its screenshot, and finishes with the link if you added one. Screenshots are stored in the bot's `/data` volume, so they survive restarts and updates.

## Common questions

**Which sitemap URL do I use?**
- Yoast or Rank Math: `https://yourshop.com/sitemap_index.xml`
- WordPress without an SEO plugin: `https://yourshop.com/wp-sitemap.xml`

Sitemap indexes (sitemaps that list other sitemaps) and `.gz` sitemaps are handled automatically. Cart, checkout, my-account, tag and author pages are skipped by default. You can change this with `SITEMAP_EXCLUDE`.

**Pages are behind Cloudflare or a security plugin and the sync fails.**
Allow your VPS IP in Cloudflare or Wordfence. Requests use the user agent `WooChatwootBot`.

**How do I stop the bot from answering something?** Put rules in `EXTRA_INSTRUCTIONS`, for example:
`EXTRA_INSTRUCTIONS=Never promise delivery dates. We do not offer COD. Always reply in English or Hindi.`

**What if my order numbers aren't the WooCommerce order ID?**
Plugins like *Sequential Order Numbers* change the number customers see. The lookup uses the real order ID, so with such a plugin, customers may need to use the ID instead. Ask if you need support for this.

**How do I use Ollama instead (fully local, no API keys)?**
Run `docker compose --profile ollama up -d`, then `docker exec -it $(docker ps -qf name=ollama) ollama pull qwen2.5:3b`. Set `LLM_PROVIDERS=ollama` (or `gemini,groq,ollama` to use it as the last fallback). This needs about 4 GB of extra RAM.

**Privacy:** order details are shown only when the order number **and** billing email match. Addresses and phone numbers are never sent to the chat. The WooCommerce key only needs **Read** permission. Free AI tiers may use prompts to improve their models. Use Ollama if that matters to you.

**Updating content:** the bot re-syncs every `SYNC_INTERVAL_HOURS` (default 24). To sync immediately, click **Sync** in `/admin`. Prices and stock are always fetched live, so they're never out of date.

## Files

| File | What it does |
|---|---|
| `app/kb.py` | Sitemap crawler and full-text search index (SQLite FTS5, BM25) |
| `app/woo.py` | WooCommerce products and order lookup |
| `app/llm.py` | Free AI providers with automatic fallback |
| `app/bot.py` | Conversation logic: order flow, handoff, answers |
| `app/chatwoot.py` | Sends replies to Chatwoot and hands chats to humans |
| `app/main.py` | Webhook, admin page and scheduled sync |
