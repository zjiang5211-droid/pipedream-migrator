"""Pipedream export analyzer: parse Pipedream shutdown export archives and
generate n8n migration packages.

Input: the ZIP produced by Pipedream's official export tool (one project at
a time): one JSON file per workflow (+ optional manifest.json), OR a single
workflow JSON file.

The export tool is new (built for the 2027-03-31 shutdown) and its exact
schema is not publicly documented, so parsing is defensive and heuristic:
we look for common keys (trigger/steps/name/id/code/props) and classify
best-effort. Anything we cannot classify is surfaced honestly as a manual
step instead of being silently dropped.

Output per project:
  - inventory: workflow count, trigger breakdown, integrations, timers,
    code-step ratio, redacted-credential count
  - per workflow: parsed steps + generated n8n workflow JSON (import-ready)
  - checklist: OAuth reconnects, timer rebuilds, state/env-var reminders
  - cost comparison vs n8n self-hosted / n8n Cloud
"""

import io
import json
import re
import uuid
import zipfile
from collections import Counter

REDACTED = "<redacted>"

# Pipedream app -> n8n node type for the most common integrations.
# We generate the node with minimal valid params + original props in `notes`
# for manual verification (n8n parameter schemas differ per version).
APP_TO_N8N = {
    "slack": "n8n-nodes-base.slack",
    "gmail": "n8n-nodes-base.gmail",
    "google_sheets": "n8n-nodes-base.googleSheets",
    "googlesheets": "n8n-nodes-base.googleSheets",
    "telegram": "n8n-nodes-base.telegram",
    "discord": "n8n-nodes-base.discord",
    "openai": "n8n-nodes-base.openAi",
    "anthropic": "n8n-nodes-base.anthropic",
    "notion": "n8n-nodes-base.notion",
    "airtable": "n8n-nodes-base.airtable",
    "github": "n8n-nodes-base.github",
    "gitlab": "n8n-nodes-base.gitlab",
    "http": "n8n-nodes-base.httpRequest",
    "webhook": "n8n-nodes-base.webhook",
    "email": "n8n-nodes-base.emailSend",
    "smtp": "n8n-nodes-base.emailSend",
    "twilio": "n8n-nodes-base.twilio",
    "stripe": "n8n-nodes-base.stripe",
    "shopify": "n8n-nodes-base.shopify",
    "salesforce": "n8n-nodes-base.salesforce",
    "hubspot": "n8n-nodes-base.hubspot",
    "jira": "n8n-nodes-base.jira",
    "linear": "n8n-nodes-base.linear",
    "postgres": "n8n-nodes-base.postgres",
    "mysql": "n8n-nodes-base.mySql",
    "mongodb": "n8n-nodes-base.mongoDb",
    "redis": "n8n-nodes-base.redis",
    "s3": "n8n-nodes-base.awsS3",
    "aws": "n8n-nodes-base.awsS3",
    "openweather": "n8n-nodes-base.openWeatherMap",
}

STATE_PATTERNS = re.compile(
    r"(\$\.flow|\$checkpoint|checkpoint|data_store|DataStore|this\.data\b)", re.IGNORECASE
)


def _as_list(v):
    return v if isinstance(v, list) else []


def _str(v):
    return v if isinstance(v, str) else ""


def _find_redacted(obj):
    """Count values equal to the export's redaction marker."""
    n = 0
    if isinstance(obj, dict):
        for v in obj.values():
            n += _find_redacted(v)
    elif isinstance(obj, list):
        for v in obj:
            n += _find_redacted(v)
    elif obj == REDACTED:
        n += 1
    return n


# ---------------------------------------------------------------- triggers

def classify_trigger(trig):
    """Return {kind, label, detail, raw} for a Pipedream trigger object."""
    if not isinstance(trig, dict):
        return {"kind": "manual", "label": "Manual / unknown trigger",
                "detail": "Trigger structure not recognized — start the n8n workflow manually.",
                "raw": trig}
    blob = json.dumps(trig).lower()
    t = _str(trig.get("type") or trig.get("kind")).lower()
    key = _str(trig.get("key") or trig.get("component_key") or trig.get("componentKey")).lower()
    interface = _str(trig.get("interface")).lower()
    app = _str(trig.get("app") or trig.get("app_name"))

    http_hit = ("http" in t or "webhook" in t or "webhook" in key
                or "$.interface.http" in blob or interface == "$.interface.http")
    timer_hit = ("timer" in t or "schedule" in t or "cron" in blob
                 or "$.interface.timer" in blob or interface == "$.interface.timer")

    if http_hit and not timer_hit:
        return {"kind": "webhook", "label": "HTTP / Webhook trigger",
                "detail": "Fires on incoming HTTP requests.",
                "raw": trig}
    if timer_hit:
        cfg = trig.get("config") or trig.get("props") or {}
        interval = _describe_interval(cfg, blob)
        return {"kind": "schedule", "label": "Schedule trigger",
                "detail": interval, "raw": trig, "config": cfg}
    if app or "source" in t or "event" in key:
        app_label = app or key.split("-")[0] if key else "app"
        return {"kind": "app_source", "label": f"App event source ({app_label})",
                "detail": f"Pipedream app event ({key or 'unknown event'}). n8n needs the matching app trigger — reconnect the account and pick the event.",
                "raw": trig, "app": app_label, "key": key}
    return {"kind": "manual", "label": "Manual / unknown trigger",
            "detail": "Trigger structure not recognized — start the n8n workflow manually.",
            "raw": trig}


def _describe_interval(cfg, blob):
    if isinstance(cfg, dict):
        for k in ("interval_seconds", "intervalSeconds", "interval"):
            if k in cfg:
                try:
                    s = int(cfg[k])
                    if s % 3600 == 0:
                        return f"Every {s // 3600} hour(s)"
                    if s % 60 == 0:
                        return f"Every {s // 60} minute(s)"
                    return f"Every {s} second(s)"
                except (ValueError, TypeError):
                    pass
        cron = cfg.get("cron") or cfg.get("cron_expression") or cfg.get("cronExpression")
        if cron:
            return f"Cron: {cron}"
    m = re.search(r"cron[^a-z0-9]{0,4}([0-9*/,\- ]{5,})", blob)
    if m:
        return f"Cron: {m.group(1).strip()}"
    return "On a schedule (interval not parsed — check the original workflow)"


# ------------------------------------------------------------------- steps

def classify_step(step, idx):
    """Return a normalized step dict."""
    if not isinstance(step, dict):
        return {"name": f"step_{idx}", "kind": "unknown",
                "label": "Unrecognized step", "notes": "Step was not a JSON object.",
                "redacted": 0, "raw": step}
    name = _str(step.get("name") or step.get("title") or step.get("id") or f"step_{idx}")
    blob = json.dumps(step)
    redacted = _find_redacted(step)
    code = step.get("code") or step.get("script") or ""
    lang = _str(step.get("language") or step.get("lang") or step.get("runtime")).lower()
    comp_key = _str(step.get("key") or step.get("component_key") or step.get("componentKey"))
    stype = _str(step.get("type")).lower()
    app = _str(step.get("app") or step.get("app_name"))

    # Code step
    if code or stype in ("code", "nodejs", "python", "bash", "go") or "definecomponent" in _str(code).lower():
        if not lang:
            lang = "python" if stype == "python" else "nodejs"
        return {"name": name, "kind": "code", "language": lang, "code": _str(code),
                "redacted": redacted, "raw": step,
                "label": f"Code step ({lang})"}

    # Condition / branch
    if stype in ("condition", "branch", "filter") or "if" == comp_key or "filter" in comp_key:
        return {"name": name, "kind": "condition", "redacted": redacted, "raw": step,
                "label": "Condition / branch"}

    # Delay
    if "delay" in comp_key or stype == "delay" or "$.flow.delay" in blob:
        return {"name": name, "kind": "delay", "redacted": redacted, "raw": step,
                "label": "Delay step"}

    # Prebuilt action
    if comp_key or stype == "action" or app:
        app_guess = app or (comp_key.split("-")[0] if "-" in comp_key else comp_key)
        return {"name": name, "kind": "action", "app": app_guess, "key": comp_key,
                "props": step.get("props") or step.get("configured_props")
                         or step.get("configuredProps") or {},
                "redacted": redacted, "raw": step,
                "label": f"Action: {comp_key or app_guess or name}"}

    return {"name": name, "kind": "unknown", "redacted": redacted, "raw": step,
            "label": f"Unclassified step ({name}) — needs manual rebuild"}


# --------------------------------------------------------------- workflows

def parse_workflow(obj, filename=""):
    """Parse one workflow JSON object into a normalized structure."""
    if not isinstance(obj, dict):
        return None
    name = _str(obj.get("name") or obj.get("title")
                or filename.rsplit(".", 1)[0] or "Unnamed workflow")
    wid = _str(obj.get("id") or obj.get("workflow_id") or uuid.uuid4().hex[:8])
    trig = classify_trigger(obj.get("trigger") or obj.get("interface") or {})
    steps = [classify_step(s, i) for i, s in enumerate(_as_list(obj.get("steps") or obj.get("components")))]
    return {"id": wid, "name": name, "filename": filename,
            "trigger": trig, "steps": steps,
            "redacted": _find_redacted(obj)}


def analyze_export(data: bytes, filename: str):
    """Parse an uploaded export (ZIP or single JSON). Returns project dict."""
    workflows = []
    skipped = []
    errors = []

    def add_workflow(obj, fname):
        try:
            wf = parse_workflow(obj, fname)
            if wf:
                workflows.append(wf)
            else:
                errors.append(f"{fname}: not a workflow object")
        except Exception as e:  # never let one bad file kill the analysis
            errors.append(f"{fname}: parse error ({e})")

    lname = filename.lower()
    if lname.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                # manifest.json lists skipped workflows / warnings
                if "manifest.json" in z.namelist():
                    try:
                        man = json.loads(z.read("manifest.json"))
                        skipped = _as_list(man.get("skipped") or man.get("skipped_workflows"))
                        if isinstance(skipped[0] if skipped else None, dict):
                            skipped = [s.get("name", "?") for s in skipped]
                    except Exception:
                        pass
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    fn = info.filename
                    if fn == "manifest.json":
                        continue
                    if not fn.lower().endswith(".json"):
                        continue
                    try:
                        obj = json.loads(z.read(fn))
                    except Exception as e:
                        errors.append(f"{fn}: invalid JSON ({e})")
                        continue
                    # A file may hold one workflow or a dict of workflows
                    if isinstance(obj, dict) and "workflows" in obj and isinstance(obj["workflows"], list):
                        for w in obj["workflows"]:
                            add_workflow(w, fn)
                    elif isinstance(obj, list):
                        for w in obj:
                            add_workflow(w, fn)
                    else:
                        add_workflow(obj, fn.split("/")[-1])
        except zipfile.BadZipFile:
            errors.append("File is not a valid ZIP archive.")
    else:
        try:
            obj = json.loads(data.decode("utf-8", errors="replace"))
        except Exception as e:
            errors.append(f"Invalid JSON: {e}")
            obj = None
        if obj is not None:
            if isinstance(obj, dict) and "workflows" in obj and isinstance(obj["workflows"], list):
                for w in obj["workflows"]:
                    add_workflow(w, filename)
            elif isinstance(obj, list):
                for w in obj:
                    add_workflow(w, filename)
            else:
                add_workflow(obj, filename)

    project = {
        "id": uuid.uuid4().hex[:12],
        "name": filename,
        "workflows": workflows,
        "skipped": skipped,
        "errors": errors,
    }
    project["inventory"] = build_inventory(workflows)
    project["checklist"] = build_checklist(project)
    return project


def build_inventory(workflows):
    triggers = Counter(w["trigger"]["kind"] for w in workflows)
    apps = Counter()
    code_steps = 0
    total_steps = 0
    redacted = 0
    timers = 0
    for w in workflows:
        if w["trigger"]["kind"] == "schedule":
            timers += 1
        redacted += w["redacted"]
        for s in w["steps"]:
            total_steps += 1
            if s["kind"] == "code":
                code_steps += 1
            if s.get("app"):
                apps[s["app"]] += 1
    return {
        "workflow_count": len(workflows),
        "trigger_breakdown": dict(triggers),
        "unique_apps": sorted(apps.keys()),
        "app_usage": dict(apps),
        "timer_count": timers,
        "total_steps": total_steps,
        "code_steps": code_steps,
        "code_ratio": round(code_steps / total_steps, 2) if total_steps else 0,
        "redacted_count": redacted,
    }


def build_checklist(project):
    """Migration checklist: list of {severity, title, detail}."""
    items = []
    inv = project["inventory"]

    # OAuth reconnects
    if inv["unique_apps"]:
        apps = ", ".join(inv["unique_apps"])
        items.append({
            "severity": "must",
            "title": f"Reconnect {len(inv['unique_apps'])} connected account(s) in n8n",
            "detail": f"The export redacts all credentials ({inv['redacted_count']} redacted values found). "
                      f"Apps used: {apps}. Every account must be reconnected on the n8n side — OAuth tokens are "
                      f"permanently deleted by Pipedream on April 30, 2027.",
        })
    else:
        items.append({
            "severity": "must",
            "title": "Check for connected accounts",
            "detail": "No app connections were detected in the export, but verify in the Pipedream dashboard — "
                      "any connected account must be reconnected in n8n.",
        })

    # Timer rebuilds
    for w in project["workflows"]:
        if w["trigger"]["kind"] == "schedule":
            items.append({
                "severity": "must",
                "title": f"Recreate schedule for “{w['name']}”",
                "detail": f"Pipedream schedule: {w['trigger']['detail']}. Rebuild it as an n8n Schedule Trigger "
                          f"and double-check the timezone — n8n defaults to the server timezone.",
            })

    # State / checkpoint
    state_hits = []
    for w in project["workflows"]:
        for s in w["steps"]:
            if s["kind"] == "code" and STATE_PATTERNS.search(s.get("code", "")):
                state_hits.append(f"{w['name']} → {s['name']}")
                break
    if state_hits:
        items.append({
            "severity": "must",
            "title": "$checkpoint / data-store state does not export",
            "detail": f"Found state usage in: {'; '.join(state_hits)}. Pipedream's export excludes data stores "
                      f"and checkpoint state. Copy the state out manually before April 30, 2027 and re-seed it "
                      f"in n8n (e.g. via a static data store or your own database).",
        })

    # Env vars — always excluded
    items.append({
        "severity": "must",
        "title": "Copy environment variables manually",
        "detail": "The export excludes environment variables. Copy them from the Pipedream dashboard "
                  "into n8n's variables / credentials before April 30, 2027.",
    })

    # Non-JS code steps
    nonjs = [(w["name"], s["name"], s["language"])
             for w in project["workflows"] for s in w["steps"]
             if s["kind"] == "code" and s.get("language") not in ("nodejs", "node", "javascript", "js")]
    if nonjs:
        items.append({
            "severity": "should",
            "title": f"Review {len(nonjs)} non-JavaScript code step(s)",
            "detail": "n8n's Code node runs JavaScript and Python (Pyodide). "
                      + "; ".join(f"{w} → {s} ({l})" for w, s, l in nonjs[:8])
                      + ". Bash/Go steps need rewriting.",
        })

    # Unmapped steps
    unmapped = [(w["name"], s["name"])
                for w in project["workflows"] for s in w["steps"]
                if s["kind"] == "unknown"]
    if unmapped:
        items.append({
            "severity": "should",
            "title": f"Manually rebuild {len(unmapped)} unrecognized step(s)",
            "detail": "These steps could not be classified from the export and are generated as annotated "
                      "placeholders: " + "; ".join(f"{w} → {s}" for w, s in unmapped[:8]),
        })

    # App event sources need trigger re-pick
    app_trigs = [(w["name"], w["trigger"].get("app", "?"))
                 for w in project["workflows"]
                 if w["trigger"]["kind"] == "app_source"]
    if app_trigs:
        items.append({
            "severity": "should",
            "title": f"Re-pick {len(app_trigs)} app event trigger(s) in n8n",
            "detail": "Pipedream app event sources map to n8n app triggers — pick the matching event per app: "
                      + "; ".join(f"{w} ({a})" for w, a in app_trigs[:8]),
        })

    # Skipped workflows from manifest
    if project["skipped"]:
        items.append({
            "severity": "should",
            "title": f"{len(project['skipped'])} workflow(s) were skipped by the export",
            "detail": "The export skips workflows that were never deployed: "
                      + ", ".join(project["skipped"][:10])
                      + ". Decide whether each one is still needed.",
        })

    return items


# ------------------------------------------------------- n8n generation

def _node(name, ntype, type_version, params, pos, notes=None):
    node = {"parameters": params, "id": uuid.uuid4().hex,
            "name": name, "type": ntype, "typeVersion": type_version,
            "position": pos}
    if notes:
        node["notes"] = notes
    return node


def _trigger_node(wf, pos):
    t = wf["trigger"]
    kind = t["kind"]
    if kind == "webhook":
        return _node(
            "Webhook", "n8n-nodes-base.webhook", 2,
            {"httpMethod": "POST", "path": _slug(wf["name"]),
             "responseMode": "onReceived",
             "options": {}},
            pos,
            notes="Migrated from Pipedream HTTP/Webhook trigger. n8n generates a new webhook URL — update the sender.")
    if kind == "schedule":
        rule = _schedule_rule(t.get("config") or {})
        return _node(
            "Schedule Trigger", "n8n-nodes-base.scheduleTrigger", 1.2,
            {"rule": {"interval": [rule]}},
            pos,
            notes=f"Migrated from Pipedream schedule ({t.get('detail', '')}). Verify the timezone in n8n.")
    if kind == "app_source":
        return _node(
            "App Trigger (manual)", "n8n-nodes-base.manualTrigger", 1, {},
            pos,
            notes=f"Pipedream app event source ({t.get('app', '?')}: {t.get('key', '')}). "
                  f"In n8n, replace this node with the matching app trigger and reconnect the account.")
    return _node(
        "Start (manual)", "n8n-nodes-base.manualTrigger", 1, {},
        pos,
        notes="Pipedream trigger was not recognized — pick the right trigger in n8n.")


def _schedule_rule(cfg):
    """Map a Pipedream timer config to an n8n schedule rule."""
    if isinstance(cfg, dict):
        for k in ("interval_seconds", "intervalSeconds"):
            if k in cfg:
                try:
                    s = int(cfg[k])
                    if s >= 3600 and s % 3600 == 0:
                        return {"field": "hours", "hoursInterval": s // 3600}
                    if s >= 60 and s % 60 == 0:
                        return {"field": "minutes", "minutesInterval": s // 60}
                    return {"field": "seconds", "secondsInterval": s}
                except (ValueError, TypeError):
                    pass
        cron = cfg.get("cron") or cfg.get("cron_expression") or cfg.get("cronExpression")
        if cron:
            return {"field": "cron", "cronExpression": str(cron)}
    return {"field": "hours", "hoursInterval": 1}  # safe default, flagged in notes


def _step_node(step, pos):
    kind = step["kind"]
    header = (f"// Migrated from Pipedream step: {step['name']}\n"
              f"// ⚠ REVIEW REQUIRED — verify logic against the original workflow.\n")

    if kind == "code":
        lang = step.get("language", "nodejs")
        code = step.get("code", "")
        if lang in ("python", "py"):
            note = ("# Migrated from Pipedream Python code step: " + step["name"] + "\n"
                    "# ⚠ REVIEW REQUIRED — n8n runs Python via Pyodide (no pip packages,\n"
                    "# different stdlib availability). Test before relying on it.\n\n")
            return _node(step["name"], "n8n-nodes-base.code", 2,
                         {"mode": "runOnceForAllItems", "language": "python",
                          "pythonCode": note + code},
                         pos)
        note = (header + "// n8n differences: steps.X → $input/$json, "
                "$.respond() → Respond to Webhook node, $.flow.delay → Wait node.\n\n")
        return _node(step["name"], "n8n-nodes-base.code", 2,
                     {"mode": "runOnceForAllItems", "language": "javaScript",
                      "jsCode": note + code},
                     pos)

    if kind == "condition":
        return _node(
            step["name"], "n8n-nodes-base.if", 2.2,
            {"conditions": {"options": {"caseSensitive": True, "leftValue": "",
                                        "typeValidation": "loose"},
                            "conditions": [{"id": uuid.uuid4().hex,
                                            "leftValue": "={{ $json.value }}",
                                            "rightValue": "",
                                            "operator": {"type": "string",
                                                         "operation": "equals"}}],
                            "combinator": "and"}},
            pos,
            notes="Migrated from Pipedream condition/branch. Rewrite the condition to match the original logic.")

    if kind == "delay":
        return _node(
            step["name"], "n8n-nodes-base.wait", 1.1,
            {"resume": "afterTimeInterval",
             "amount": 5, "unit": "minutes"},
            pos,
            notes="Migrated from Pipedream delay ($.flow.delay). Set the wait duration to match the original.")

    if kind == "action":
        app = (step.get("app") or "").lower().replace(" ", "_")
        n8n_type = APP_TO_N8N.get(app)
        props_json = json.dumps(step.get("props") or {}, indent=2)[:2000]
        cred_note = (f"Migrated from Pipedream action `{step.get('key') or step['name']}`.\n"
                     f"⚠ RECONNECT the {step.get('app') or 'app'} credentials in n8n, then verify parameters.\n"
                     f"Original configured props (for manual mapping):\n{props_json}")
        if n8n_type:
            return _node(step["name"], n8n_type, 1, {}, pos, notes=cred_note)
        # Unknown app: honest placeholder that still imports cleanly
        placeholder = (header + f"// MANUAL ACTION REQUIRED\n// Original Pipedream action: "
                       f"{step.get('key') or step['name']} (app: {step.get('app') or 'unknown'})\n"
                       f"// Rebuild this step with the matching n8n node.\n"
                       f"// Original props:\n// {props_json.replace(chr(10), chr(10) + '// ')}\n"
                       f"return [{{ json: {{ _migrated: true }} }}];")
        return _node(step["name"] + " (manual)", "n8n-nodes-base.code", 2,
                     {"mode": "runOnceForAllItems", "language": "javaScript",
                      "jsCode": placeholder},
                     pos)

    # unknown
    placeholder = (header + "// UNRECOGNIZED STEP — the export structure for this step could not be\n"
                   "// classified. Rebuild it manually in n8n using the original workflow as reference.\n"
                   "return [{{ json: {{ _migrated: true }} }}}];")
    return _node(step["name"] + " (manual)", "n8n-nodes-base.code", 2,
                 {"mode": "runOnceForAllItems", "language": "javaScript",
                  "jsCode": placeholder},
                 pos)


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:48] or "pipedream-migrated"


def generate_n8n_workflow(wf):
    """Generate an import-ready n8n workflow dict for one parsed workflow."""
    nodes = []
    x = 240
    y = 300
    nodes.append(_trigger_node(wf, [x, y]))
    for step in wf["steps"]:
        x += 220
        nodes.append(_step_node(step, [x, y]))

    connections = {}
    for i in range(len(nodes) - 1):
        src = nodes[i]["name"]
        dst = nodes[i + 1]["name"]
        connections[src] = {"main": [[{"node": dst, "type": "main", "index": 0}]]}

    return {
        "name": f"[Migrated] {wf['name']}",
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
        "staticData": None,
        "meta": {
            "migrated_from": "pipedream",
            "pipedream_workflow_id": wf["id"],
            "generated_by": "pipedream-migrator",
            "review_required": True,
        },
    }


def n8n_preview(n8n_wf, max_lines=60):
    """First N lines of the n8n JSON for the free preview."""
    text = json.dumps(n8n_wf, indent=2, ensure_ascii=False)
    lines = text.splitlines()
    truncated = len(lines) > max_lines
    return "\n".join(lines[:max_lines]), truncated, len(lines)


# ------------------------------------------------------- cost comparison

def cost_comparison(pipedream_monthly_usd=None):
    """Build the cost comparison table. pipedream_monthly_usd is user input."""
    try:
        pd = float(pipedream_monthly_usd) if pipedream_monthly_usd not in (None, "") else None
    except (ValueError, TypeError):
        pd = None
    rows = []
    if pd is not None:
        rows.append({"option": "Pipedream (your current bill)",
                     "monthly": pd, "note": "Your input. Pipedream billed compute credits; subscriptions end Mar 31, 2027 with prorated refund."})
    else:
        rows.append({"option": "Pipedream (your current bill)",
                     "monthly": None, "note": "Enter your monthly Pipedream spend to compare."})
    rows.append({"option": "n8n self-hosted",
                 "monthly": 0,
                 "note": "n8n Community Edition is free and open source; you pay only for your own server (often $5–10/mo VPS)."})
    rows.append({"option": "n8n Cloud",
                 "monthly": None,
                 "note": "Paid tiers start around $20/mo (verify current pricing at n8n.io/pricing)."})
    saving = None
    if pd is not None and pd > 0:
        # vs self-hosted infra estimate $6/mo
        saving = round(pd * 12 - 72, 2)
    return {"rows": rows, "est_yearly_saving_vs_selfhosted": saving}
