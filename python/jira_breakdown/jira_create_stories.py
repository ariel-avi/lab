#!/usr/bin/env python3
"""
Create Jira Tasks from a .zip package of Markdown files.

Each .md file inside the zip becomes one Jira Task:
  - Summary  = first H1 heading in the file (fallback: filename without extension)
  - Description = ADF (Atlassian Document Format) converted from the rest of the markdown

Usage:
    python create_jira_tasks.py <package.zip> \
        --jira-email <email> \
        --jira-domain <domain> \
        --jira-token <api_token> \
        --jira-project <project_key>

Example:
    python create_jira_tasks.py jira-tasks.zip \
        --jira-email me@example.com \
        --jira-domain mycompany.atlassian.net \
        --jira-token ATATT3xFfGF0... \
        --jira-project PLAT

Dependencies:
    pip install mistune requests
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from base64 import b64encode
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import mistune


# ---------------------------------------------------------------------------
# Markdown -> ADF conversion
# ---------------------------------------------------------------------------
#
# ADF reference: https://developer.atlassian.com/cloud/jira/platform/apis/document/
#
# Mistune v3 returns an AST: a list of block tokens, each potentially with a
# "children" key containing inline tokens. We walk that AST and emit ADF nodes.

ADF_DOC_VERSION = 1


def _inline_to_adf(tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert a list of mistune inline tokens into ADF inline nodes."""
    nodes: list[dict[str, Any]] = []

    for tok in tokens:
        ttype = tok.get("type")

        if ttype == "text":
            text = tok.get("raw", "")
            if text:
                nodes.append({"type": "text", "text": text})

        elif ttype == "linebreak":
            nodes.append({"type": "hardBreak"})

        elif ttype == "softbreak":
            # Render soft breaks as a space — matches how Jira renders prose.
            nodes.append({"type": "text", "text": " "})

        elif ttype == "codespan":
            nodes.append(
                {
                    "type": "text",
                    "text": tok.get("raw", ""),
                    "marks": [{"type": "code"}],
                }
            )

        elif ttype in ("strong", "emphasis"):
            mark = "strong" if ttype == "strong" else "em"
            for child in _inline_to_adf(tok.get("children") or []):
                if child.get("type") == "text":
                    child.setdefault("marks", []).append({"type": mark})
                nodes.append(child)

        elif ttype == "link":
            href = tok["attrs"]["url"]
            for child in _inline_to_adf(tok.get("children") or []):
                if child.get("type") == "text":
                    child.setdefault("marks", []).append(
                        {"type": "link", "attrs": {"href": href}}
                    )
                nodes.append(child)

        elif ttype == "image":
            # ADF supports media nodes but they require an upload step.
            # Fall back to a link so the description stays readable.
            href = tok["attrs"]["url"]
            alt_children = tok.get("children") or []
            alt_text = "".join(c.get("raw", "") for c in alt_children) or href
            nodes.append(
                {
                    "type": "text",
                    "text": alt_text,
                    "marks": [{"type": "link", "attrs": {"href": href}}],
                }
            )

        else:
            # Unknown inline -> degrade to plain text
            raw = tok.get("raw")
            if raw:
                nodes.append({"type": "text", "text": raw})

    # Merge adjacent text nodes that share the exact same marks. Cleaner output.
    merged: list[dict[str, Any]] = []
    for n in nodes:
        if (
            merged
            and n.get("type") == "text"
            and merged[-1].get("type") == "text"
            and merged[-1].get("marks") == n.get("marks")
        ):
            merged[-1]["text"] += n["text"]
        else:
            merged.append(n)
    return merged


def _list_items_to_adf(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert mistune list_item tokens into ADF listItem nodes."""
    list_items: list[dict[str, Any]] = []
    for item in items:
        # Each list_item's children are block tokens (paragraphs, nested lists, ...)
        item_blocks = _blocks_to_adf(item.get("children") or [])
        # ADF requires listItem.content to be non-empty.
        if not item_blocks:
            item_blocks = [{"type": "paragraph", "content": []}]
        list_items.append({"type": "listItem", "content": item_blocks})
    return list_items


def _blocks_to_adf(tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert a list of mistune block tokens into ADF block nodes."""
    blocks: list[dict[str, Any]] = []

    for tok in tokens:
        ttype = tok.get("type")

        if ttype == "blank_line":
            continue

        if ttype == "heading":
            level = tok["attrs"]["level"]
            # ADF supports heading levels 1-6.
            blocks.append(
                {
                    "type": "heading",
                    "attrs": {"level": max(1, min(6, level))},
                    "content": _inline_to_adf(tok.get("children") or []),
                }
            )

        elif ttype == "paragraph":
            content = _inline_to_adf(tok.get("children") or [])
            # ADF rejects paragraphs whose content is just an empty list — skip those.
            blocks.append({"type": "paragraph", "content": content})

        elif ttype == "block_text":
            # Mistune sometimes emits block_text inside list items.
            blocks.append(
                {
                    "type": "paragraph",
                    "content": _inline_to_adf(tok.get("children") or []),
                }
            )

        elif ttype == "block_code" or ttype == "fenced_code":
            attrs = tok.get("attrs") or {}
            language = attrs.get("info") or attrs.get("language") or None
            code_text = tok.get("raw", "").rstrip("\n")
            node: dict[str, Any] = {
                "type": "codeBlock",
                "content": [{"type": "text", "text": code_text}] if code_text else [],
            }
            if language:
                node["attrs"] = {"language": language}
            blocks.append(node)

        elif ttype == "block_quote":
            blocks.append(
                {
                    "type": "blockquote",
                    "content": _blocks_to_adf(tok.get("children") or []),
                }
            )

        elif ttype == "list":
            attrs = tok.get("attrs") or {}
            ordered = attrs.get("ordered", False)
            list_type = "orderedList" if ordered else "bulletList"
            blocks.append(
                {
                    "type": list_type,
                    "content": _list_items_to_adf(tok.get("children") or []),
                }
            )

        elif ttype == "thematic_break":
            blocks.append({"type": "rule"})

        elif ttype == "block_html":
            # ADF has no raw-HTML node — render as a code block so nothing is lost.
            raw = tok.get("raw", "").rstrip("\n")
            blocks.append(
                {
                    "type": "codeBlock",
                    "content": [{"type": "text", "text": raw}] if raw else [],
                }
            )

        else:
            # Unknown block -> degrade to a paragraph of its raw text, if any.
            raw = tok.get("raw")
            if raw:
                blocks.append(
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": raw}],
                    }
                )

    return blocks


def markdown_to_adf(markdown_text: str) -> dict[str, Any]:
    """Convert a Markdown string to an ADF document."""
    parser = mistune.create_markdown(
        renderer=None,  # AST mode
        plugins=["strikethrough", "table"],
    )
    tokens = parser(markdown_text)
    if not isinstance(tokens, list):
        # Defensive: AST mode should always return a list, but guard anyway.
        tokens = []
    content = _blocks_to_adf(tokens)
    if not content:
        content = [{"type": "paragraph", "content": []}]
    return {"version": ADF_DOC_VERSION, "type": "doc", "content": content}


# ---------------------------------------------------------------------------
# Markdown file parsing
# ---------------------------------------------------------------------------

def split_summary_and_body(markdown_text: str, fallback_summary: str) -> tuple[str, str]:
    """
    Pull the first H1 line out of the markdown to use as the Jira summary.
    The H1 line itself is dropped from the body so it isn't repeated in the
    description.
    """
    lines = markdown_text.splitlines()
    summary = fallback_summary
    summary_idx: int | None = None

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            summary = stripped[2:].strip() or fallback_summary
            summary_idx = idx
            break

    if summary_idx is None:
        return summary, markdown_text

    body_lines = lines[:summary_idx] + lines[summary_idx + 1 :]
    # Strip leading blank lines that the H1 removal left behind.
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    return summary, "\n".join(body_lines)


# ---------------------------------------------------------------------------
# Jira REST client (stdlib only — no requests dependency)
# ---------------------------------------------------------------------------

class JiraClient:
    def __init__(self, domain: str, email: str, token: str):
        # Accept domain with or without scheme; normalise to https.
        domain = domain.strip().rstrip("/")
        if domain.startswith("http://") or domain.startswith("https://"):
            self.base_url = domain
        else:
            self.base_url = f"https://{domain}"
        creds = f"{email}:{token}".encode("utf-8")
        self.auth_header = "Basic " + b64encode(creds).decode("ascii")

    def create_task(
        self, project_key: str, summary: str, description_adf: dict[str, Any]
    ) -> dict[str, Any]:
        url = f"{self.base_url}/rest/api/3/issue"
        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "issuetype": {"name": "Task"},
                "description": description_adf,
            }
        }
        body = json.dumps(payload).encode("utf-8")
        req = Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": self.auth_header,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Jira API {e.code} {e.reason}: {err_body}"
            ) from e
        except URLError as e:
            raise RuntimeError(f"Jira API connection error: {e.reason}") from e


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create Jira Tasks from a .zip of Markdown files.",
    )
    parser.add_argument(
        "zip_path",
        type=Path,
        help="Path to the .zip package containing one .md file per Jira task.",
    )
    parser.add_argument("--jira-email", required=True, help="Jira account email.")
    parser.add_argument(
        "--jira-domain",
        required=True,
        help="Jira Cloud domain, e.g. mycompany.atlassian.net",
    )
    parser.add_argument(
        "--jira-token", required=True, help="Jira API token (Atlassian account token)."
    )
    parser.add_argument(
        "--jira-project",
        required=True,
        help="Jira project key (e.g. PLAT) where tasks will be created.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print payloads without calling Jira.",
    )
    return parser.parse_args()


def iter_markdown_entries(zip_path: Path):
    """Yield (filename, markdown_text) tuples, sorted by filename."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = sorted(
            n
            for n in zf.namelist()
            if n.lower().endswith(".md") and not n.endswith("/")
        )
        if not names:
            raise RuntimeError(f"No .md files found inside {zip_path}")
        for name in names:
            with zf.open(name) as f:
                yield name, f.read().decode("utf-8")


def main() -> int:
    args = parse_args()

    if not args.zip_path.is_file():
        print(f"error: {args.zip_path} is not a file", file=sys.stderr)
        return 2

    client: JiraClient | None = None
    if not args.dry_run:
        client = JiraClient(args.jira_domain, args.jira_email, args.jira_token)

    created: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []

    for filename, md_text in iter_markdown_entries(args.zip_path):
        fallback_summary = Path(filename).stem.replace("-", " ").replace("_", " ")
        summary, body = split_summary_and_body(md_text, fallback_summary)
        adf = markdown_to_adf(body)

        if args.dry_run:
            print(f"--- {filename} ---")
            print(f"summary: {summary}")
            print(json.dumps(adf, indent=2))
            print()
            continue

        try:
            assert client is not None
            result = client.create_task(args.jira_project, summary, adf)
            issue_key = result.get("key", "<unknown>")
            print(f"created {issue_key}: {summary}  (from {filename})")
            created.append((filename, issue_key))
        except RuntimeError as e:
            print(f"FAILED  {filename}: {e}", file=sys.stderr)
            failed.append((filename, str(e)))

    if not args.dry_run:
        print()
        print(f"created: {len(created)}    failed: {len(failed)}")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())