"""Second import channel: GitHub Sync repos + the Pipedream REST API.

Why: Pipedream's official Export Workflows writes to the project File Store,
which Free and Basic plans don't include — those users cannot produce the
export ZIP at all. But they can still get their own workflows out two ways:

  1. GitHub Sync — if the project was ever synced, the repo already holds a
     `workflow.yaml` per workflow plus one file per code step.
  2. REST API — any user can mint an API key and read their workflows via
     GET /v1/workflows and GET /v1/workflows/{id}.

Both paths convert what they fetch into the same workflow-object shape the
official export produces, then reuse analyzer.parse_workflow /
build_inventory / build_checklist — so the migration pack (inventory, n8n
JSON, checklist, cost comparison) is identical to the upload path's.

Privacy rules (hard requirements):
  - The Pipedream API key is used for this request only: it lives in a
    local variable, is never written to the project store, disk, or logs,
    and is never echoed in error messages.
  - Connected-account references (authProvisionId) fetched from either
    source are replaced with the export's <redacted> marker, so account
    IDs never leak into generated packs.

User-facing errors are raised as ImportProblem with a clear Chinese
message (the audience for this channel reads the Idea in Chinese, and a
silent or cryptic failure helps no one).
"""

import io
import json
import os
import posixpath
import re
import urllib.error
import urllib.request
import uuid
import zipfile

import yaml

from analyzer import parse_workflow, build_inventory, build_checklist, REDACTED

MAX_ZIP_BYTES = 25 * 1024 * 1024          # same cap as the upload path
MAX_WORKFLOWS = 500                        # matches the official export cap
UA = {"User-Agent": "pipedream-migrator-import/1.0"}


class ImportProblem(Exception):
    """A user-facing import failure (Chinese message + HTTP status)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------- http

def _http_get(url, headers=None, timeout=30, max_bytes=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if max_bytes:
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ImportProblem("内容太大了（超过 25 MB 上限），没法在线导入。", 413)
                return data
            return resp.read()
    except ImportProblem:
        raise
    except urllib.error.HTTPError as e:
        raise _map_http_error(e)
    except urllib.error.URLError:
        raise ImportProblem("网络连不上对方服务器，请稍后再试。", 502)
    except TimeoutError:
        raise ImportProblem("对方服务器响应超时，请稍后再试。", 502)


def _map_http_error(e):  # overridden per-source below via `source` attr
    return ImportProblem(f"对方服务器返回了错误（HTTP {e.code}），请稍后再试。", 502)


def _http_json(url, headers=None, timeout=30, http_error=None):
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/json",
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except ImportProblem:
        raise
    except urllib.error.HTTPError as e:
        if http_error:
            raise http_error(e)
        raise _map_http_error(e)
    except (urllib.error.URLError, TimeoutError):
        raise ImportProblem("网络连不上对方服务器，请稍后再试。", 502)
    except (ValueError, UnicodeDecodeError):
        raise ImportProblem("对方服务器返回的内容无法解析，请稍后再试。", 502)


# ------------------------------------------------------------------ helpers

def _scrub(obj):
    """Replace connected-account references with the export redaction marker."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("authProvisionId", "auth_provision_id"):
                out[k] = REDACTED
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def _assemble(objs, project_name, source_errors):
    """objs: [(workflow_obj, filename)] -> project dict (upload-path shape)."""
    workflows = []
    errors = list(source_errors)
    for obj, fname in objs:
        try:
            wf = parse_workflow(obj, fname)
            if wf:
                workflows.append(wf)
            else:
                errors.append(f"{fname}：不是可识别的 workflow，已跳过")
        except Exception as e:  # one bad workflow must not kill the import
            errors.append(f"{fname}：解析失败（{e}），已跳过")
    project = {
        "id": uuid.uuid4().hex[:12],
        "name": project_name,
        "workflows": workflows,
        "skipped": [],
        "errors": errors,
    }
    project["inventory"] = build_inventory(workflows)
    project["checklist"] = build_checklist(project)
    return project


def _strip_version(uses):
    """'spotify-get-x@0.1.1' -> 'spotify-get-x'; '@ws/key@1.0.0' -> '@ws/key'."""
    if "@" in uses[1:]:
        return uses.rsplit("@", 1)[0]
    return uses


# ------------------------------------------------------------- GitHub Sync

_GH_URL = re.compile(
    r"^https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:/tree/([^/]+(?:/[^/]+)*))?/?$")


def parse_github_repo_url(url):
    """Return (owner, repo, branch_hint|None). Raise ImportProblem (Chinese)."""
    u = (url or "").strip()
    if not u:
        raise ImportProblem("请先粘贴 GitHub 仓库地址。")
    m = _GH_URL.match(u)
    if not m:
        raise ImportProblem(
            "这个地址没法识别。需要 GitHub 仓库地址，形如 "
            "https://github.com/你的用户名/仓库名（必须是 Pipedream GitHub Sync 的仓库）。")
    owner, repo, branch = m.group(1), m.group(2), m.group(3)
    return owner, repo, branch


def _gh_http_error(e):
    if e.code == 404:
        return ImportProblem(
            "找不到这个仓库，或仓库不是公开的。GitHub 导入只支持公开仓库——"
            "请确认地址没错；私有仓库请改用上面的导出文件上传，或把仓库设为公开后重试。", 404)
    if e.code == 403:
        return ImportProblem("GitHub 暂时限流了，请过几分钟再试。", 429)
    return ImportProblem(f"GitHub 返回了错误（HTTP {e.code}），请稍后再试。", 502)


def _download_repo_zip(owner, repo, branch_hint):
    """Download the repo ZIP from GitHub. Returns (zip_bytes, branch_used)."""
    candidates = []
    if branch_hint:
        candidates.append(branch_hint)
    try:
        info = _http_json(f"https://api.github.com/repos/{owner}/{repo}",
                          timeout=15, http_error=_gh_http_error)
        if isinstance(info, dict) and info.get("default_branch"):
            candidates.append(info["default_branch"])
    except ImportProblem as e:
        if e.status == 404:
            raise
        # rate-limited etc.: fall through and try the well-known branches
    for b in ("production", "main", "master"):
        if b not in candidates:
            candidates.append(b)
    last_err = None
    for branch in candidates:
        try:
            data = _http_get(
                f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}",
                timeout=60, max_bytes=MAX_ZIP_BYTES)
            return data, branch
        except ImportProblem as e:
            last_err = e
            if e.status in (413, 429):
                raise
            continue  # 404 on this branch -> try the next one
    raise last_err or ImportProblem("没能下载这个仓库，请稍后再试。", 502)


def _yaml_trigger(t):
    if not isinstance(t, dict):
        return {}
    blob = json.dumps(t).lower()
    uses = str(t.get("uses") or t.get("key") or "")
    props = _scrub(t.get("props") or t.get("config") or {})
    tid = str(t.get("id") or "")
    if "http" in uses.lower() or tid.startswith("hi_") or "$.interface.http" in blob:
        return {"key": uses or "http", "interface": "$.interface.http",
                "props": props, "config": props}
    if ("timer" in blob or "schedule" in uses.lower() or "cron" in blob
            or tid.startswith("ti_")):
        return {"key": uses, "interface": "$.interface.timer",
                "props": props, "config": props}
    if uses:
        key = _strip_version(uses)
        return {"key": key, "app": key.split("-")[0].lstrip("@"),
                "props": props, "config": props}
    return {"props": props, "config": props}


def _yaml_step(s, idx, files, wf_dir, errors, wf_name):
    if not isinstance(s, dict):
        return {"name": f"step_{idx}"}
    name = str(s.get("namespace") or s.get("name") or f"step_{idx}")
    uses = str(s.get("uses") or "")
    runtime = str(s.get("runtime") or "")
    props = _scrub(s.get("props") or {})
    if uses.startswith("./") or uses.startswith("../"):
        path = posixpath.normpath(posixpath.join(wf_dir, uses))
        raw = files.get(path)
        lang = "python" if runtime.startswith("python") or path.endswith(".py") else "nodejs"
        if raw is None:
            errors.append(f"{wf_name} → {name}：代码文件 {uses} 在仓库里没找到，"
                          f"已按空代码步处理，请迁移时手动补回这段代码")
            raw = b""
        return {"name": name, "type": "code", "language": lang,
                "code": raw.decode("utf-8", errors="replace")}
    if uses:
        return {"name": name, "key": _strip_version(uses), "props": props}
    return {"name": name, "props": props}


def fetch_github_project(repo_url):
    """Import all workflows from a public Pipedream GitHub Sync repo."""
    owner, repo, branch_hint = parse_github_repo_url(repo_url)
    data, _branch = _download_repo_zip(owner, repo, branch_hint)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportProblem("下载到的仓库内容不是有效的压缩包，请稍后再试。", 502)

    files = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        # strip the "<repo>-<branch>/" top folder codeload adds
        parts = info.filename.split("/", 1)
        rel = parts[1] if len(parts) == 2 else info.filename
        if rel:
            files[rel] = zf.read(info.filename)

    yaml_paths = sorted(p for p in files
                        if posixpath.basename(p) in ("workflow.yaml", "workflow.yml"))
    if not yaml_paths:
        raise ImportProblem(
            "这个仓库里没有找到 workflow.yaml —— 它看起来不是 Pipedream GitHub Sync 仓库。"
            "请确认粘贴的是 Pipedream 同步过去的那个仓库地址。")

    objs, errors = [], []
    for path in yaml_paths[:MAX_WORKFLOWS]:
        wf_dir = posixpath.dirname(path)
        try:
            y = yaml.safe_load(files[path].decode("utf-8", errors="replace"))
        except Exception as e:
            errors.append(f"{path}：YAML 解析失败（{e}），已跳过")
            continue
        if not isinstance(y, dict) or not isinstance(y.get("steps"), list):
            errors.append(f"{path}：没有 steps，不像 workflow 定义，已跳过")
            continue
        name = str(y.get("name") or posixpath.basename(wf_dir) or path)
        triggers = y.get("triggers") or []
        steps = [_yaml_step(s, i, files, wf_dir, errors, name)
                 for i, s in enumerate(y["steps"])]
        objs.append(({
            "id": str(y.get("id") or ""),
            "name": name,
            "trigger": _yaml_trigger(triggers[0]) if triggers else {},
            "steps": steps,
        }, path))
    project = _assemble(objs, f"{owner}/{repo} (GitHub Sync)", errors)
    return project


# ------------------------------------------------------------ Pipedream API

def _api_base():
    return os.environ.get("PDM_PIPEDREAM_API_BASE",
                          "https://api.pipedream.com/v1").rstrip("/")


def _pd_http_error(e):
    if e.code in (401, 403):
        return ImportProblem(
            "Pipedream 拒绝了这个 API 密钥（无效、已过期或已被删除）。"
            "密钥在 Pipedream 里打开 Settings → API 生成，粘贴时注意别多空格。", 401)
    if e.code == 429:
        return ImportProblem("Pipedream API 限流了，请过一会儿再试。", 429)
    if e.code == 404:
        return ImportProblem("Pipedream API 找不到对应资源，请稍后再试。", 404)
    return ImportProblem(f"Pipedream API 返回了错误（HTTP {e.code}），请稍后再试。", 502)


def _api_trigger(t):
    if not isinstance(t, dict):
        return {}
    comp = t.get("component") if isinstance(t.get("component"), dict) else {}
    key = str(t.get("key") or t.get("component_key") or comp.get("key") or "")
    props = _scrub(t.get("configured_props") or t.get("props") or {})
    name = str(t.get("name") or "")
    out = {"key": key, "name": name, "props": props, "config": props}
    low = f"{key} {name}".lower()
    if "http" in low or "webhook" in low:
        out["interface"] = "$.interface.http"
    elif ("schedule" in low or "timer" in low
          or "cron" in json.dumps(props).lower()):
        out["interface"] = "$.interface.timer"
    if key:
        out["app"] = key.split("-")[0].lstrip("@")
    return out


def _api_step(s, idx):
    if not isinstance(s, dict):
        return {"name": f"step_{idx}"}
    name = str(s.get("namespace") or s.get("name") or s.get("name_slug")
               or f"step_{idx}")
    comp = s.get("component") if isinstance(s.get("component"), dict) else {}
    saved = s.get("savedComponent") if isinstance(s.get("savedComponent"), dict) else {}
    key = str(s.get("key") or s.get("component_key") or comp.get("key")
              or saved.get("key") or "")
    code = s.get("code")
    lang_raw = str(s.get("lang") or s.get("language") or "")
    lang = "python" if lang_raw.startswith("python") else "nodejs"
    props = _scrub(s.get("configured_props") or s.get("configuredProps")
                   or s.get("props") or {})
    if isinstance(code, str) and code:
        return {"name": name, "type": "code", "language": lang, "code": code}
    if key:
        return {"name": name, "key": key, "props": props}
    return {"name": name, "props": props}


def _pd_key_error(e):
    """Error mapping for the key-validation call: any auth-shaped failure
    (401/403, and the 404 Pipedream returns for unauthenticated requests)
    means the key itself is the problem."""
    if e.code in (401, 403, 404):
        return ImportProblem(
            "Pipedream 拒绝了这个 API 密钥（无效、已过期或已被删除）。"
            "密钥在 Pipedream 里打开 Settings → API 生成，粘贴时注意别多空格。", 401)
    return _pd_http_error(e)


def fetch_api_project(api_key):
    """Import all workflows readable by a Pipedream user API key.

    The key is used only for these requests and is never stored or logged.
    """
    key = (api_key or "").strip()
    if not key:
        raise ImportProblem("请先粘贴你的 Pipedream API 密钥。")
    headers = {"Authorization": f"Bearer {key}"}
    base = _api_base()
    # Validate the key first, so a bad key gets the invalid-key message
    # instead of a confusing downstream error.
    _http_json(f"{base}/users/me", headers=headers,
               timeout=30, http_error=_pd_key_error)
    listing = _http_json(f"{base}/workflows?limit=100", headers=headers,
                         timeout=30, http_error=_pd_http_error)
    items = listing.get("data") if isinstance(listing, dict) else listing
    if not isinstance(items, list) or not items:
        raise ImportProblem(
            "这个 API 密钥名下没有读到任何 workflow。"
            "请确认密钥属于有 workflow 的 Pipedream 账号，或改用 GitHub Sync 仓库导入。")
    objs = []
    for item in items[:MAX_WORKFLOWS]:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        detail = _http_json(f"{base}/workflows/{item['id']}", headers=headers,
                            timeout=30, http_error=_pd_http_error)
        d = detail
        if isinstance(detail, dict) and isinstance(detail.get("data"), dict):
            d = detail["data"]
        if not isinstance(d, dict):
            continue
        triggers = d.get("triggers") or []
        objs.append(({
            "id": str(d.get("id") or item.get("id") or ""),
            "name": str(d.get("name") or item.get("name") or "Unnamed workflow"),
            "trigger": _api_trigger(triggers[0]) if triggers else {},
            "steps": [_api_step(s, i) for i, s in enumerate(d.get("steps") or [])],
        }, str(item.get("name") or item.get("id"))))
    if not objs:
        raise ImportProblem(
            "workflow 列表读到了，但详情都没能解析。请稍后再试，或改用 GitHub Sync 仓库导入。")
    project = _assemble(objs, "Pipedream API 导入", [])
    return project
