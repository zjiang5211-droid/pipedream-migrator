"""Pipedream Migrator — analyze Pipedream shutdown exports, generate n8n migration packs.

Positioning: "Pipedream deletes everything April 30, 2027. Upload your export,
walk away with n8n-ready workflows."

Routes:
  GET  /                        landing + upload tool
  POST /api/analyze             ZIP/JSON export -> project analysis (free)
  GET  /project/<pid>           results page: inventory, checklist, n8n previews
  GET  /api/project/<pid>/preview/<wid>   n8n JSON preview, first 60 lines (free)
  GET  /api/project/<pid>/n8n/<wid>       full n8n JSON download ($29 unlock)
  GET  /api/project/<pid>/bundle.zip      all n8n JSONs as ZIP ($29 unlock)
  POST /api/unlock              unlock with PDM_UNLOCK_KEY
  GET  /unlock                  $29 migration pack page -> payment coming soon
  GET  /payment-coming-soon     honest placeholder (no fake checkout)
  GET  /sample-export.zip       demo export for trying the tool
  GET  /guides, /guides/<slug>, /sitemap.xml, /robots.txt
  GET  /api/stats               conversion counter (kill-line tracking)

Free vs paid:
  free  full inventory + full checklist + n8n JSON preview (60 lines/workflow)
  $29   full n8n JSON downloads + bulk ZIP (one-time, per migration pack)
Paid unlock is a key (PDM_UNLOCK_KEY env); Stripe Payment Links get wired in
later — until then the paywall honestly says checkout is not live yet.

Privacy: uploads are parsed in memory and kept only in the in-memory project
store (24h TTL). Nothing is written to disk.
"""

import io
import json
import os
import time
import zipfile

from flask import (Flask, request, jsonify, send_file, render_template,
                   abort, Response, redirect, url_for, session)

import config
from analyzer import analyze_export, generate_n8n_workflow, n8n_preview, cost_comparison
import guides as guides_mod

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB export cap

# --- In-memory project store (24h TTL), same pattern as PubMigrator jobs. ---
PROJECTS = {}
PROJECT_TTL = 24 * 3600


def _sweep():
    now = time.time()
    for pid in [p for p, v in PROJECTS.items() if now - v["ts"] > PROJECT_TTL]:
        del PROJECTS[pid]


def _get_project(pid):
    _sweep()
    proj = PROJECTS.get(pid)
    if not proj:
        abort(404)
    return proj


def _is_unlocked():
    return bool(session.get("unlocked")) or \
        request.args.get("key") == config.UNLOCK_KEY and bool(config.UNLOCK_KEY)


# ------------------------------------------------------------------ pages

@app.get("/")
def index():
    return render_template("index.html", guides=guides_mod.list_guides())


@app.get("/project/<pid>")
def project_page(pid):
    proj = _get_project(pid)
    # Pre-generate n8n workflows once and cache on the project
    if "n8n" not in proj:
        proj["n8n"] = {w["id"]: generate_n8n_workflow(w) for w in proj["workflows"]}
    previews = {}
    for wid, n8n_wf in proj["n8n"].items():
        text, truncated, total = n8n_preview(n8n_wf)
        previews[wid] = {"text": text, "truncated": truncated, "total_lines": total,
                         "node_count": len(n8n_wf["nodes"])}
    return render_template(
        "project.html", project=proj, previews=previews,
        cost=proj.get("cost") or cost_comparison(None),
        unlocked=_is_unlocked(),
    )


@app.get("/unlock")
def unlock_page():
    return render_template("unlock.html")


@app.get("/payment-coming-soon")
def payment_coming_soon():
    return render_template("coming_soon.html")


@app.get("/sample-export.zip")
def sample_export():
    path = os.path.join(os.path.dirname(__file__), "samples", "sample-project.zip")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name="sample-pipedream-export.zip")


# -------------------------------------------------------------------- API

@app.post("/api/analyze")
def api_analyze():
    _sweep()
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "No file uploaded."}), 400
    data = f.read()
    if len(data) > app.config["MAX_CONTENT_LENGTH"]:
        return jsonify({"ok": False, "error": "File too large (25 MB cap)."}), 400
    monthly = request.form.get("monthly_spend")
    try:
        project = analyze_export(data, f.filename)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not parse export: {e}"}), 400
    if not project["workflows"] and not project["errors"]:
        return jsonify({"ok": False,
                        "error": "No workflows found. Upload the ZIP from Pipedream's export tool (one project at a time)."}), 400
    project["ts"] = time.time()
    project["cost"] = cost_comparison(monthly)
    PROJECTS[project["id"]] = project
    _bump_stat("analyses")
    return jsonify({"ok": True, "project_id": project["id"],
                    "redirect": url_for("project_page", pid=project["id"])})


@app.get("/api/project/<pid>/preview/<wid>")
def api_preview(pid, wid):
    proj = _get_project(pid)
    n8n_wf = proj.get("n8n", {}).get(wid) or generate_n8n_workflow(
        next(w for w in proj["workflows"] if w["id"] == wid))
    proj.setdefault("n8n", {})[wid] = n8n_wf
    text, truncated, total = n8n_preview(n8n_wf, max_lines=200)
    return jsonify({"ok": True, "name": n8n_wf["name"], "preview": text,
                    "truncated": truncated, "total_lines": total})


def _require_unlocked():
    if not _is_unlocked():
        return jsonify({"ok": False, "error": "locked",
                        "unlock_url": url_for("unlock_page")}), 402
    return None


@app.get("/api/project/<pid>/n8n/<wid>")
def api_download_n8n(pid, wid):
    err = _require_unlocked()
    if err:
        return err
    proj = _get_project(pid)
    n8n_wf = proj.get("n8n", {}).get(wid)
    if not n8n_wf:
        wf = next((w for w in proj["workflows"] if w["id"] == wid), None)
        if not wf:
            abort(404)
        n8n_wf = generate_n8n_workflow(wf)
        proj.setdefault("n8n", {})[wid] = n8n_wf
    _bump_stat("downloads")
    buf = io.BytesIO(json.dumps(n8n_wf, indent=2, ensure_ascii=False).encode("utf-8"))
    return send_file(buf, as_attachment=True,
                     download_name=f"n8n-{wid}.json",
                     mimetype="application/json")


@app.get("/api/project/<pid>/bundle.zip")
def api_download_bundle(pid):
    err = _require_unlocked()
    if err:
        return err
    proj = _get_project(pid)
    if "n8n" not in proj:
        proj["n8n"] = {w["id"]: generate_n8n_workflow(w) for w in proj["workflows"]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for w in proj["workflows"]:
            n8n_wf = proj["n8n"][w["id"]]
            z.writestr(f"n8n-{w['id']}.json",
                       json.dumps(n8n_wf, indent=2, ensure_ascii=False))
        z.writestr("MIGRATION-CHECKLIST.md", _checklist_md(proj))
    buf.seek(0)
    _bump_stat("downloads")
    return send_file(buf, as_attachment=True, download_name="n8n-migration-pack.zip",
                     mimetype="application/zip")


def _checklist_md(proj):
    lines = [f"# Migration checklist — {proj['name']}", ""]
    for c in proj["checklist"]:
        mark = "🔴 MUST" if c["severity"] == "must" else "🟡 SHOULD"
        lines += [f"## {mark}: {c['title']}", "", c["detail"], ""]
    return "\n".join(lines)


@app.post("/api/unlock")
def api_unlock():
    key = (request.get_json(silent=True) or {}).get("key", "")
    if config.UNLOCK_KEY and key == config.UNLOCK_KEY:
        session["unlocked"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Invalid key."}), 403


# ------------------------------------------------------------------ guides

@app.get("/guides")
def guides_index():
    return render_template("guides.html", guides=guides_mod.list_guides())


@app.get("/guides/<slug>")
def guide_page(slug):
    g = guides_mod.render_guide(slug)
    if not g:
        abort(404)
    title, desc, html = g
    return render_template("guide.html", title=title, description=desc,
                           content=html, guides=guides_mod.list_guides())


# ------------------------------------------------------------------- misc

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/sitemap.xml")
def sitemap():
    urls = ["/", "/guides", "/unlock"]
    for g in guides_mod.list_guides():
        urls.append(f"/guides/{g['slug']}")
    base = request.host_url.rstrip("/")
    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        xml.append(f"<url><loc>{base}{u}</loc></url>")
    xml.append("</urlset>")
    return Response("\n".join(xml), mimetype="application/xml")


@app.get("/robots.txt")
def robots():
    base = request.host_url.rstrip("/")
    return Response(f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n",
                    mimetype="text/plain")


_STATS = {"analyses": 0, "downloads": 0, "started": time.time()}


def _bump_stat(k):
    _STATS[k] = _STATS.get(k, 0) + 1


@app.get("/api/stats")
def stats():
    return jsonify({"ok": True, **_STATS})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
