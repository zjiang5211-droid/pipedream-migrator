# Pipedream → n8n Migration Analyzer

Pipedream Workflows shuts down **March 31, 2027**; all data permanently deleted **April 30, 2027**.
This tool analyzes Pipedream export ZIPs and generates n8n-ready migration packs.

## What it does

Upload the ZIP from Pipedream's export tool (Project Settings → Export Workflows), get:

1. **Full inventory** — trigger types, integrated APIs, timers, code-step share
2. **n8n JSON per workflow** — import-ready (schedule → Schedule Trigger, webhook → Webhook,
   code steps keep original code flagged for review, actions → matching n8n nodes)
3. **Migration checklist** — OAuth reconnects, timer rebuilds, $checkpoint/state warnings, env vars
4. **Cost comparison** — your Pipedream bill vs n8n self-hosted / Cloud

Free: inventory + checklist + n8n previews. **$29 one-time** unlocks full n8n JSON downloads + bulk ZIP.

## Run locally

```bash
pip install -r requirements.txt
python3 samples/make_sample.py   # build the demo export
python3 tests/test_chain.py       # 61 checks, full upload→parse→download chain
python3 app.py                   # dev server on :5000
```

## Deploy (Render free tier)

Docker-based, same pattern as PubMigrator. `render.yaml` is included;
or create manually: Web Service → Docker → `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120 app:app`.

Env vars: `PDM_UNLOCK_KEY` (the $29 unlock key; Stripe Payment Link wiring comes later).

## Boundaries (by design)

- Never touches OAuth tokens (export redacts them; we never ask)
- No automatic migration execution (generates config + checklist only)
- Does not cover Pipedream Connect (officially unaffected by the shutdown)

Not affiliated with Pipedream or n8n.
