"""SEO guides section: loads markdown articles from guides/ and renders them."""

import os
import re

GUIDES_DIR = os.path.join(os.path.dirname(__file__), "guides")

# slug -> (title, description) — used for the index page and <title>/meta.
GUIDE_META = {
    "pipedream-workflows-shutting-down-how-to-migrate-to-n8n": (
        "Pipedream Workflows Is Shutting Down: How to Migrate to n8n",
        "Pipedream Workflows shuts down March 31, 2027 and all data is permanently deleted April 30, 2027. A practical guide to migrating your workflows to n8n — what the export gives you, what doesn't transfer, and the checklist that matters.",
    ),
    "pipedream-alternative-2027": (
        "Pipedream Alternative 2027: Where to Move Your Workflows",
        "Pipedream Workflows is shutting down. Comparing the realistic alternatives — n8n, Make, Activepieces, FlowRunner — and how to pick one before the April 30, 2027 deletion.",
    ),
}

_markdown = None


def _get_markdown():
    global _markdown
    if _markdown is None:
        import markdown as _md
        _markdown = _md.Markdown(extensions=["extra", "toc"])
    return _markdown


def list_guides():
    out = []
    for slug, (title, desc) in GUIDE_META.items():
        path = os.path.join(GUIDES_DIR, slug + ".md")
        if os.path.exists(path):
            out.append({"slug": slug, "title": title, "description": desc})
    return out


def render_guide(slug):
    meta = GUIDE_META.get(slug)
    if not meta:
        return None
    path = os.path.join(GUIDES_DIR, slug + ".md")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        src = f.read()
    src = re.sub(r"^# .+\n+", "", src, count=1)
    md = _get_markdown()
    md.reset()
    html = md.convert(src)
    return meta[0], meta[1], html
