# Pipedream Workflows Is Shutting Down: How to Migrate to n8n

Pipedream announced that **Workflows and String will shut down on March 31, 2027**. If your automations run on Pipedream Workflows, they have an end date — and a deletion date after it.

This guide covers what's happening, what Pipedream's export tool actually gives you, and how to move your workflows to n8n without rebuilding blind.

## What Pipedream announced

From [Pipedream's official announcement](https://pipedream.com/docs/workflows):

> "Workflows and String will shut down on **March 31, 2027**."

> "All Workflows data will be **permanently deleted by April 30, 2027**, including the credentials and OAuth tokens for your connected accounts."

The key points:

- **Workflows are in maintenance mode now.** Existing workflows keep running through March 31, 2027, but Pipedream is no longer accepting signups or subscriptions and won't ship new features for Workflows.
- **The deletion is permanent and includes credentials.** On April 30, 2027, everything goes — workflow definitions, and the OAuth tokens for your connected accounts.
- **Pipedream Connect is not affected.** If you use Connect, MCP Servers, or the API Proxy, nothing changes. This shutdown is about Workflows (and String) only.
- **Billing:** subscriptions stay active until March 31, 2027, when Pipedream automatically cancels them and issues a prorated refund. Monthly subscribers can cancel sooner via self-service; annual subscribers should contact support.

Six months sounds like a lot of time. It isn't, if you have dozens of workflows and every connected account needs to be re-authenticated somewhere new.

## What the export tool gives you (and what it doesn't)

Pipedream built an export tool for the transition. In their words, it lets you **"download the structure of your workflows as JSON, one project at a time"** — each file describes its triggers, steps, code, configured props, and control flow.

What the export **includes**:

- Workflow structure: triggers, steps in order, inline code, configured properties
- Your code steps — the logic you actually wrote, which is the hardest part to reconstruct from memory

What the export **excludes** (this is the part that bites):

- **Credentials and OAuth tokens.** Anything credential-like is redacted. Every connected account must be reconnected wherever you land.
- **Environment variables.** Copy them out separately.
- **Data stores and their contents.** If a workflow keeps state across runs, that state doesn't come with the export.
- **Event history.** Past runs stay behind.
- **Workflows you never deployed.** The export skips them.

Treat the export as a **specification for a rebuild**, not an import file. No destination platform imports Pipedream JSON directly — something has to translate it.

## The migration checklist that matters

Before April 30, 2027, work through this list per project:

1. **Export every project now** — even ones you plan to retire. The files cost nothing to keep, and April is not the month to discover you needed one.
2. **Inventory what you have.** Count the workflows, note the trigger types (webhook, schedule, app events), and list every integrated app. You can't plan a migration you haven't scoped.
3. **Reconnect every account.** The export redacts all credentials, so plan a session per app: Slack, Gmail, GitHub, databases, all of them.
4. **Rebuild every timer.** Schedules don't transfer — recreate them on the new platform and double-check timezones.
5. **Migrate state manually.** If any workflow uses checkpoint state or a data store, copy that state out before April 30 and re-seed it on the new platform.
6. **Copy environment variables** out of the Pipedream dashboard.
7. **Move the risky workflows first** — the ones with approval steps, delays, or complex branching. They're the ones that tell you whether the new platform fits.

## Why n8n

n8n is the most common landing spot for Pipedream refugees, for straightforward reasons: it's open source and can be self-hosted for free, its Code node runs both JavaScript and Python, and its workflow format (nodes + connections) maps naturally onto Pipedream's trigger-then-steps shape.

The mapping is mostly mechanical:

| Pipedream | n8n |
|---|---|
| HTTP / webhook trigger | Webhook node |
| Schedule trigger | Schedule Trigger node |
| Node.js code step | Code node (JavaScript), original code carried over |
| Python code step | Code node (Python via Pyodide) — test it, the runtime differs |
| Prebuilt action (Slack, Gmail, …) | Matching n8n node + reconnected credentials |
| If/else, filter | IF node |
| `$.flow.delay` | Wait node |

What doesn't map 1:1: app event sources (pick the matching n8n trigger per app), npm-package-dependent code (n8n's Python runs in Pyodide without pip; heavy Node dependencies may need rethinking), and anything relying on Pipedream-specific helpers like `$.respond()` or `$.flow.suspend`.

## Don't rebuild blind

The export is a JSON spec. Reading 40 workflows' worth of JSON and hand-building the n8n equivalents is where the weeks go.

Our free analyzer does the mechanical part: upload the export ZIP, get a full inventory (triggers, integrations, timers, code-step share), per-workflow n8n JSON you can import, and the checklist above generated from your actual workflows — which accounts to reconnect, which timers to rebuild, which steps carry state that doesn't export.

The inventory, checklist, and n8n previews are free. The $29 one-time pack unlocks full n8n JSON downloads for every workflow.
