# Hülyo Flight Monitor

Watches [hulyo.co.il/flights](https://www.hulyo.co.il/flights) and pushes a
Telegram alert when flights to a **watched destination** (default: Amsterdam
and Nice) become available — with dates, times, prices, and a **direct deal
link**. No login required; it reads the site's public catalog JSON.

Runs as a **GitHub Actions cron job** every 5 minutes, so monitoring is
independent of any single machine being on. See `hulyo_monitor.py` for the
catalog-parsing logic (destinations / catalogItems / productOverrides /
segmentMap — documented in the module docstring).

Text **"list"** (or **"status"** / **"current"**) to the bot at any time and
it replies with the current active offering for every watched destination,
even if nothing is new — picked up on the next 5-minute cycle. This bot is
**dedicated to Hulyo** (not the shared `@Boaz_mor_monitor_bot` used by the
other monitors) specifically so its `getUpdates` polling can't race with
the passport monitor's interactive reply flow, which reads from that same
shared inbox.

## Configuration

`config.json` holds non-secret settings (`watch_destinations`,
`destination_names`, poll/price filters, catalog URLs). The Telegram bot
token and chat ID are **not** stored here — they come from the
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` repo secrets, injected as env vars
by the workflow (`.github/workflows/monitor.yml`).

To watch more destinations, add their IATA codes to `watch_destinations`
and a friendly name to `destination_names`.

## How the schedule works

- `.github/workflows/monitor.yml` triggers `python hulyo_monitor.py` (single
  cycle) every 5 minutes via `cron: "*/5 * * * *"` — GitHub's practical
  floor for scheduled workflows.
- Dedup state (`seen_deals.json`) is committed back to the repo by the
  workflow itself whenever it changes, so state persists across runs even
  though each run is a fresh checkout.
- `concurrency: hulyo-monitor` (queue, not cancel) prevents overlapping runs
  from racing on `seen_deals.json` if a run is ever slow.
- Public repo → unlimited free Actions minutes. (A private repo's free tier
  is 2,000 min/month, which 5-minute polling exhausts in about a week.)

## Running locally

```bash
pip install -r requirements.txt
python hulyo_monitor.py          # one cycle (env vars or config.json for creds)
python hulyo_monitor.py --loop   # continuous, like the old local deployment
```

## Files

| File | Purpose |
|---|---|
| `hulyo_monitor.py` | The whole monitor (fetch, resolve, dedup, Telegram, list-command) |
| `config.json` | Watched destinations, filters, catalog URLs (no secrets) |
| `seen_deals.json` | New-deal dedup state, auto-updated by the workflow |
| `telegram_offset.json` | Last-read Telegram update ID, auto-updated by the workflow |
| `.github/workflows/monitor.yml` | The 5-minute cron schedule |
