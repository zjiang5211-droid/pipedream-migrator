"""Tests for the second import channel (GitHub Sync + Pipedream API).

Run: python3 tests/test_importers.py   (from the repo root)
Covers: URL parsing, GitHub Sync ZIP parsing (fixture shaped like a real
sync repo), API import against a local mock of api.pipedream.com, Chinese
error messages, and the API-key-never-persisted guarantee.
"""

import io
import json
import os
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importers
from importers import (ImportProblem, parse_github_repo_url,
                       fetch_github_project, fetch_api_project)
from analyzer import generate_n8n_workflow

PASS = 0


def check(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ok {name}")


# ------------------------------------------------------------- URL parsing

def test_urls():
    o, r, b = parse_github_repo_url("https://github.com/cfjedimaster/GHIntegrationTest2")
    check("plain url", (o, r, b) == ("cfjedimaster", "GHIntegrationTest2", None))
    o, r, b = parse_github_repo_url("https://github.com/a/b.git")
    check(".git url", (o, r) == ("a", "b"))
    o, r, b = parse_github_repo_url("https://github.com/a/b/tree/production")
    check("tree branch", b == "production")
    for bad in ("", "https://gitlab.com/a/b", "github.com/a/b", "hello",
                "https://github.com/onlyowner"):
        try:
            parse_github_repo_url(bad)
            check(f"reject {bad!r}", False)
        except ImportProblem as e:
            check(f"reject {bad!r} (zh msg)", "。" in e.message or "：" in e.message)


# ------------------------------------------------------- GitHub Sync fixture
# Shaped after a real sync repo (cf. Raymond Camden's public example):
# one folder per workflow, workflow.yaml inside, code steps as
# <namespace>/entry.js files, triggers as id-only (hi_ = HTTP interface).

WF1_YAML = """schema: workflow/2022.04
name: GetDepecheModeTracks
settings:
  error_notification: true
triggers:
- id: hi_ZbHvJ98
steps:
- namespace: get_artist_top_tracks
  runtime: nodejs18.x
  uses: spotify-get-artist-top-tracks@0.1.1
  props:
    spotify:
      authProvisionId: apn_3JhQwL
    artistId: 762310PdDnwsDxAQxzQkfX
    market: US
- namespace: filterResults
  runtime: nodejs18.x
  uses: "./filterResults/entry.js"
- namespace: returnJSON
  runtime: nodejs18.x
  uses: "./returnJSON/entry.js"
"""

WF2_YAML = """schema: workflow/2022.04
name: NightlySync
triggers:
- id: ti_QwErTy1
  props:
    cron: "0 9 * * 1"
steps:
- namespace: fetch_data
  runtime: python3.11
  uses: "./fetch_data/entry.py"
- namespace: notify
  runtime: nodejs20.x
  uses: slack-send-message@1.0.0
  props:
    slack:
      authProvisionId: apn_XyZ987
    channel: "#ops"
    text: "done"
"""

ENTRY_JS = ('export default defineComponent({\n'
            '  async run({ steps, $ }) { return steps.x; },\n})\n')
ENTRY_PY = "# fetch\nimport urllib.request\nprint(urllib.request)\n"


def fixture_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("repo-main/README.md", "# sync repo")
        z.writestr("repo-main/GetDepecheModeTracks/workflow.yaml", WF1_YAML)
        z.writestr("repo-main/GetDepecheModeTracks/filterResults/entry.js", ENTRY_JS)
        z.writestr("repo-main/GetDepecheModeTracks/returnJSON/entry.js", ENTRY_JS)
        z.writestr("repo-main/billing/NightlySync/workflow.yaml", WF2_YAML)
        z.writestr("repo-main/billing/NightlySync/fetch_data/entry.py", ENTRY_PY)
    return buf.getvalue()


def test_github_import():
    orig = importers._download_repo_zip
    importers._download_repo_zip = lambda o, r, b: (fixture_zip(), "main")
    try:
        proj = fetch_github_project("https://github.com/someone/sync-repo")
    finally:
        importers._download_repo_zip = orig
    check("2 workflows", len(proj["workflows"]) == 2)
    by_name = {w["name"]: w for w in proj["workflows"]}
    w1 = by_name["GetDepecheModeTracks"]
    check("http trigger from hi_ id", w1["trigger"]["kind"] == "webhook")
    kinds = [s["kind"] for s in w1["steps"]]
    check("wf1 steps action+code+code", kinds == ["action", "code", "code"])
    check("wf1 code content read", "defineComponent" in w1["steps"][1]["code"])
    w2 = by_name["NightlySync"]
    check("timer trigger from cron props", w2["trigger"]["kind"] == "schedule")
    check("wf2 python code", w2["steps"][0]["kind"] == "code"
          and w2["steps"][0]["language"] == "python")
    blob = json.dumps(proj)
    check("authProvisionId values redacted", "apn_3JhQwL" not in blob
          and "apn_XyZ987" not in blob)
    check("redacted counted", proj["inventory"]["redacted_count"] >= 2)
    check("inventory apps", "spotify" in proj["inventory"]["unique_apps"]
          and "slack" in proj["inventory"]["unique_apps"])
    check("checklist built", len(proj["checklist"]) >= 2)
    n8n = generate_n8n_workflow(w1)
    check("n8n nodes = trigger+3", len(n8n["nodes"]) == 4)
    check("n8n first node webhook", n8n["nodes"][0]["type"] == "n8n-nodes-base.webhook")


def test_github_not_a_sync_repo():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("repo-main/src/main.py", "print(1)")
    orig = importers._download_repo_zip
    importers._download_repo_zip = lambda o, r, b: (buf.getvalue(), "main")
    try:
        fetch_github_project("https://github.com/someone/plain-repo")
        check("non-sync repo rejected", False)
    except ImportProblem as e:
        check("non-sync repo rejected (zh)", "workflow.yaml" in e.message)
    finally:
        importers._download_repo_zip = orig


# ------------------------------------------------------------- mock PD API
# Payload shapes follow Pipedream's official REST API docs examples.

LIST_PAYLOAD = {"data": [
    {"id": "p_abc123", "name": "RSS to Slack", "active": True},
    {"id": "p_def456", "name": "Daily digest", "active": True},
]}

DETAIL_1 = {
    "id": "p_abc123", "name": "RSS to Slack",
    "triggers": [{
        "id": "dc_abc123", "key": "rss-new-item-in-feed",
        "name": "New Item in Feed", "name_slug": "new-item-in-feed",
        "configured_props": {"url": "https://example.com/feed.xml"},
        "active": True,
    }],
    "steps": [
        {"namespace": "send_message", "lang": "nodejs20.x", "component": True,
         "key": "slack-send-message",
         "configured_props": {"slack": {"authProvisionId": "apn_QqWwEe"},
                              "channel": "#general", "text": "hi"}},
        {"namespace": "cleanup", "lang": "nodejs20.x",
         "code": "export default defineComponent({ async run({ steps, $ }) { return 1; } })"},
    ],
}

DETAIL_2 = {
    "id": "p_def456", "name": "Daily digest",
    "triggers": [{
        "id": "dc_def456", "key": "schedule-custom-interval",
        "name": "Schedule", "configured_props": {"cron": "0 8 * * *"},
    }],
    "steps": [
        {"namespace": "build_digest", "lang": "python3.11",
         "code": "import json\nprint(json.dumps({}))"},
    ],
}


class MockPD(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer test-key-123":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error": "unauthorized"}')
            return
        payload = None
        if self.path == "/v1/users/me":
            payload = {"data": {"id": "u_abc", "username": "tester"}}
        elif self.path.startswith("/v1/workflows?"):
            payload = LIST_PAYLOAD
        elif self.path == "/v1/workflows/p_abc123":
            payload = DETAIL_1
        elif self.path == "/v1/workflows/p_def456":
            payload = DETAIL_2
        if payload is None:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_api_import():
    srv = HTTPServer(("127.0.0.1", 0), MockPD)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["PDM_PIPEDREAM_API_BASE"] = f"http://127.0.0.1:{port}/v1"
    try:
        proj = fetch_api_project("test-key-123")
        check("api: 2 workflows", len(proj["workflows"]) == 2)
        by_name = {w["name"]: w for w in proj["workflows"]}
        w1 = by_name["RSS to Slack"]
        check("api: rss trigger app_source", w1["trigger"]["kind"] == "app_source")
        kinds = [s["kind"] for s in w1["steps"]]
        check("api: steps action+code", kinds == ["action", "code"])
        check("api: code preserved", "defineComponent" in w1["steps"][1]["code"])
        w2 = by_name["Daily digest"]
        check("api: schedule trigger", w2["trigger"]["kind"] == "schedule")
        check("api: python step", w2["steps"][0]["language"] == "python")
        blob = json.dumps(proj)
        check("api: key NOT in project", "test-key-123" not in blob)
        check("api: authProvisionId redacted", "apn_QqWwEe" not in blob)
        check("api: inventory", proj["inventory"]["workflow_count"] == 2)

        try:
            fetch_api_project("wrong-key")
            check("api: bad key rejected", False)
        except ImportProblem as e:
            check("api: bad key rejected (zh)", "拒绝" in e.message
                  and "wrong-key" not in e.message)
        try:
            fetch_api_project("   ")
            check("api: empty key rejected", False)
        except ImportProblem as e:
            check("api: empty key rejected (zh)", "粘贴" in e.message)
    finally:
        del os.environ["PDM_PIPEDREAM_API_BASE"]
        srv.shutdown()


if __name__ == "__main__":
    print("== URL parsing ==")
    test_urls()
    print("== GitHub Sync import ==")
    test_github_import()
    test_github_not_a_sync_repo()
    print("== Pipedream API import ==")
    test_api_import()
    print(f"\nALL {PASS} checks passed")
