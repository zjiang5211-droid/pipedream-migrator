# Pipedream Alternative 2027: Where to Move Your Workflows

Pipedream Workflows shuts down on **March 31, 2027**, and all workflow data — including your connected accounts' OAuth tokens — is **permanently deleted on April 30, 2027** ([Pipedream's announcement](https://pipedream.com/docs/workflows)). Pipedream Connect is not affected; this is about Workflows.

So where do the workflows go? Here's an honest look at the realistic alternatives, and how to pick one before the deletion date.

## What you're actually choosing between

Every alternative falls into one of two camps: **self-hosted** (you run it, software is free) or **cloud** (someone runs it, you pay per usage). Pipedream was serverless — you never thought about servers. Moving means deciding whether you want to think about them now.

## The shortlist

**n8n** — the default answer for ex-Pipedream users. Open source, self-hostable for free, with a paid Cloud option. The Code node runs JavaScript and Python, which covers most Pipedream code steps. Its node-based model (trigger → steps) is the closest mental match to Pipedream's shape, and its workflow JSON is an open format you can generate programmatically. If your team writes code, start here.

**Make** — the visual-builder alternative. Less code-centric than n8n, stronger at drag-and-drop scenario building across thousands of apps. Good if your workflows were mostly prebuilt actions rather than code steps. Pricing is per-operation, so estimate your volume before committing.

**Activepieces** — open source like n8n, with a gentler learning curve and a growing piece (integration) library. Worth evaluating if n8n feels heavyweight for your needs.

**FlowRunner** — a newer entrant explicitly courting Pipedream migrants, with a free tier and migration guides aimed at this exact shutdown. Newer means less battle-tested; the migration assistance is the draw.

There are others (Windmill for the code-heavy, Temporal for the infrastructure-minded, or just cron + scripts for the truly simple). But for most Pipedream workloads, the decision is n8n vs. Make, with Activepieces as the third look.

## How to pick

Ask these four questions in order:

1. **How much of your logic is code?** Mostly code steps → n8n or Windmill. Mostly prebuilt actions → Make or Activepieces.
2. **Do you want a server?** Self-hosting n8n on a small VPS is cheap but it's yours to keep alive. If "server" is a word you never want to hear, compare cloud pricings against your Pipedream bill.
3. **What does your volume cost?** Pipedream billed compute credits; n8n Cloud and Make bill executions/operations. Your Pipedream bill is the baseline — model the alternatives against it before you migrate, not after.
4. **Which apps do you depend on?** Check that your must-have integrations exist (and are mature) on the shortlist before you commit. An automation platform without your CRM's trigger is a non-starter.

## The migration itself

Whichever platform you pick, the work is the same:

1. **Export now.** Pipedream's export tool downloads each project's workflow structure as JSON, one project at a time. Do it before April 30, 2027 — after that there's nothing to export.
2. **Inventory.** Triggers, integrations, timers, code steps. Scope the work before you start it.
3. **Reconnect everything.** Credentials are redacted in the export; every account gets reconnected on the new platform.
4. **Rebuild timers and state.** Schedules don't transfer; data-store state doesn't export.
5. **Move the risky workflows first.** Approvals, delays, branching — validate the platform fit before you've moved 40 workflows.

## Skip the manual inventory

Our free analyzer takes your Pipedream export ZIP and produces the inventory (trigger types, integrations, timers, code-step share), per-workflow n8n JSON ready to import, and a migration checklist generated from your actual workflows. If n8n is your destination, it does the mechanical translation for you — the $29 one-time pack unlocks full downloads.
