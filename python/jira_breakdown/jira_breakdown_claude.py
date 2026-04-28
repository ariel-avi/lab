#!/usr/bin/env python3
"""
Generate Jira subtasks for an Epic's user stories using Anthropic Claude.

Reads an Epic + its child stories from Jira, asks Claude to break each story
down into subtasks per the supplied breakdown instructions, and creates the
subtasks under each story. The Epic and stories themselves are never modified.

Standalone: only requires `requests` and `anthropic` from PyPI.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import requests
from anthropic import Anthropic
from requests.auth import HTTPBasicAuth

# ---------- Config ---------------------------------------------------------

CLAUDE_MODEL = "claude-opus-4-5"          # change if desired
CLAUDE_MAX_TOKENS = 32000                 # Opus 4.x supports up to 32k output
HTTP_TIMEOUT = 30


# ---------- Jira client ----------------------------------------------------

class JiraClient:
    def __init__(self, domain: str, email: str, api_token: str):
        self.base = f"https://{domain.rstrip('/').removeprefix('https://')}"
        self.auth = HTTPBasicAuth(email, api_token)
        self.headers = {"Accept": "application/json", "Content-Type": "application/json"}

    # ---- read ----
    def get_issue(self, key: str) -> dict[str, Any]:
        r = requests.get(
            f"{self.base}/rest/api/3/issue/{key}",
            auth=self.auth, headers=self.headers, timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    def get_epic_stories(self, epic_key: str) -> list[dict[str, Any]]:
        """Return all child issues of `epic_key` (stories, tasks, bugs - excluding sub-tasks).

        Uses `/rest/api/3/search/jql` (the legacy `/search` endpoint was removed in
        2025 and now returns 410 Gone). Pagination is token-based via `nextPageToken`.
        """
        jql = f'parent = "{epic_key}" AND issuetype != Sub-task'
        issues: list[dict[str, Any]] = []
        next_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "jql": jql,
                "maxResults": 100,
                # New endpoint defaults to id only; request the fields we need explicitly.
                "fields": "summary,description,issuetype,project",
            }
            if next_token:
                params["nextPageToken"] = next_token
            r = requests.get(
                f"{self.base}/rest/api/3/search/jql",
                auth=self.auth, headers=self.headers, timeout=HTTP_TIMEOUT,
                params=params,
            )
            r.raise_for_status()
            data = r.json()
            issues.extend(data.get("issues", []))
            if data.get("isLast") or not data.get("nextPageToken"):
                break
            new_token = data["nextPageToken"]
            if new_token == next_token:  # safety against the known infinite-loop bug
                break
            next_token = new_token
        return issues

    # ---- write ----
    def create_subtask(self, parent_key: str, project_key: str,
                       summary: str, description: str) -> str:
        payload = {
            "fields": {
                "project": {"key": project_key},
                "parent": {"key": parent_key},
                "summary": summary[:250],
                "description": _to_adf(description),
                "issuetype": {"name": "Sub-task"},
            }
        }
        r = requests.post(
            f"{self.base}/rest/api/3/issue",
            auth=self.auth, headers=self.headers, timeout=HTTP_TIMEOUT,
            data=json.dumps(payload),
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Subtask creation failed ({r.status_code}): {r.text}")
        return r.json()["key"]


# ---------- ADF helpers ----------------------------------------------------

def _adf_to_text(node: Any) -> str:
    """Flatten Atlassian Document Format to plain text (best-effort, for READING)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    children = _adf_to_text(node.get("content", []))
    if t in ("paragraph", "heading", "listItem", "codeBlock", "blockquote"):
        return children + "\n"
    if t in ("bulletList", "orderedList"):
        return children + "\n"
    return children


# Lazy mistune parser; built once on first use.
_md_parser = None


def _get_md_parser():
    global _md_parser
    if _md_parser is None:
        import mistune  # imported lazily so the script runs even without it for read-only ops
        _md_parser = mistune.create_markdown(
            renderer=None,
            plugins=["table", "strikethrough"],
        )
    return _md_parser


def _adf_inline(children: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Convert mistune inline tokens to ADF inline content nodes."""
    out: list[dict[str, Any]] = []
    if not children:
        return out
    for tok in children:
        t = tok.get("type")
        if t == "text":
            text = tok.get("raw", "")
            if text:
                out.append({"type": "text", "text": text})
        elif t == "softbreak":
            # Treat soft line breaks as a single space — Jira paragraphs don't preserve them.
            out.append({"type": "text", "text": " "})
        elif t == "linebreak":
            out.append({"type": "hardBreak"})
        elif t == "codespan":
            out.append({"type": "text", "text": tok.get("raw", ""),
                        "marks": [{"type": "code"}]})
        elif t == "strong":
            for n in _adf_inline(tok.get("children")):
                marks = n.setdefault("marks", [])
                marks.append({"type": "strong"})
                out.append(n)
        elif t == "emphasis":
            for n in _adf_inline(tok.get("children")):
                marks = n.setdefault("marks", [])
                marks.append({"type": "em"})
                out.append(n)
        elif t == "strikethrough":
            for n in _adf_inline(tok.get("children")):
                marks = n.setdefault("marks", [])
                marks.append({"type": "strike"})
                out.append(n)
        elif t == "link":
            url = tok.get("attrs", {}).get("url", "")
            for n in _adf_inline(tok.get("children")):
                marks = n.setdefault("marks", [])
                marks.append({"type": "link", "attrs": {"href": url}})
                out.append(n)
        else:
            # Fallback: flatten unknown inline node to its text content.
            for n in _adf_inline(tok.get("children")):
                out.append(n)
    return out


def _adf_inline_plain(children: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Same as _adf_inline but strips marks not allowed in headings/table headers if needed."""
    return _adf_inline(children)


def _adf_list_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert mistune list_item tokens to ADF listItem nodes."""
    adf_items: list[dict[str, Any]] = []
    for item in items:
        item_content: list[dict[str, Any]] = []
        for child in item.get("children", []):
            ctype = child.get("type")
            if ctype == "block_text":
                # Tight list — wrap inline children in a paragraph.
                item_content.append({
                    "type": "paragraph",
                    "content": _adf_inline(child.get("children")),
                })
            else:
                item_content.extend(_adf_block(child))
        if not item_content:
            item_content = [{"type": "paragraph", "content": []}]
        adf_items.append({"type": "listItem", "content": item_content})
    return adf_items


def _adf_table(token: dict[str, Any]) -> dict[str, Any]:
    """Convert mistune table token to ADF table."""
    rows: list[dict[str, Any]] = []
    for child in token.get("children", []):
        ctype = child.get("type")
        if ctype == "table_head":
            cells = [
                {"type": "tableHeader",
                 "content": [{"type": "paragraph",
                              "content": _adf_inline(c.get("children"))}]}
                for c in child.get("children", [])
            ]
            rows.append({"type": "tableRow", "content": cells})
        elif ctype == "table_body":
            for row in child.get("children", []):
                cells = [
                    {"type": "tableCell",
                     "content": [{"type": "paragraph",
                                  "content": _adf_inline(c.get("children"))}]}
                    for c in row.get("children", [])
                ]
                rows.append({"type": "tableRow", "content": cells})
    return {"type": "table", "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
            "content": rows}


def _adf_block(token: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a single mistune block token to one or more ADF block nodes."""
    t = token.get("type")
    if t == "heading":
        level = max(1, min(6, token.get("attrs", {}).get("level", 1)))
        return [{"type": "heading", "attrs": {"level": level},
                 "content": _adf_inline(token.get("children"))}]
    if t == "paragraph":
        return [{"type": "paragraph", "content": _adf_inline(token.get("children"))}]
    if t == "block_code":
        code = token.get("raw", "")
        attrs = {}
        info = token.get("attrs", {}).get("info") or token.get("info")
        if info:
            attrs["language"] = info.strip().split()[0]
        node: dict[str, Any] = {"type": "codeBlock",
                                "content": [{"type": "text", "text": code}] if code else []}
        if attrs:
            node["attrs"] = attrs
        return [node]
    if t == "block_quote":
        inner: list[dict[str, Any]] = []
        for child in token.get("children", []):
            inner.extend(_adf_block(child))
        return [{"type": "blockquote", "content": inner or [{"type": "paragraph", "content": []}]}]
    if t == "list":
        ordered = bool(token.get("attrs", {}).get("ordered"))
        list_type = "orderedList" if ordered else "bulletList"
        return [{"type": list_type, "content": _adf_list_items(token.get("children", []))}]
    if t == "thematic_break":
        return [{"type": "rule"}]
    if t == "table":
        return [_adf_table(token)]
    if t == "blank_line":
        return []
    # Unknown block — render as a paragraph with its raw text if present.
    raw = token.get("raw", "").strip()
    if raw:
        return [{"type": "paragraph", "content": [{"type": "text", "text": raw}]}]
    return []


def _to_adf(text: str) -> dict[str, Any]:
    """Convert markdown text to a Jira ADF document.

    Falls back to a plain-paragraph doc if markdown parsing produces nothing usable.
    """
    if not text or not text.strip():
        return {"type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": []}]}
    try:
        parser = _get_md_parser()
        tokens = parser(text)
    except Exception:
        tokens = None

    content: list[dict[str, Any]] = []
    if isinstance(tokens, list):
        for tok in tokens:
            content.extend(_adf_block(tok))

    if not content:
        # Fallback: split on blank lines as plain paragraphs.
        for block in re.split(r"\n\s*\n", text.strip()):
            block = block.strip()
            if block:
                content.append({"type": "paragraph",
                                "content": [{"type": "text", "text": block}]})

    if not content:
        content = [{"type": "paragraph", "content": []}]

    return {"type": "doc", "version": 1, "content": content}


def issue_description_text(issue: dict[str, Any]) -> str:
    desc = issue.get("fields", {}).get("description")
    if isinstance(desc, str):
        return desc
    return _adf_to_text(desc).strip()


# ---------- Claude breakdown -----------------------------------------------

SYSTEM_PROMPT = """You break Jira user stories into implementation subtasks.

You will receive:
1. Breakdown instructions describing the team's subtask conventions.
2. The parent Epic description (context for components and goals).
3. The user story to break down.

Adapt the instructions to the components introduced in the Epic. Only emit
subtasks that are justified by the story's scope.

Return ONLY a JSON array, no prose, no markdown fences. Each element:
{
  "summary": "<short subtask title, <= 200 chars>",
  "description": "<full subtask body in markdown>"
}
"""

USER_TEMPLATE = """## Breakdown instructions

{instructions}

---

## Epic context

{epic}

---

## User story to break down

{story}

---

Emit the subtasks as a JSON array now.
"""


def break_down_story(claude: Anthropic, instructions: str,
                     epic_text: str, story_text: str) -> list[dict[str, str]]:
    # Streaming is required when max_tokens is large enough that the request
    # could exceed the 10-minute non-streaming limit.
    with claude.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": USER_TEMPLATE.format(
                    instructions=instructions, epic=epic_text, story=story_text),
            }],
    ) as stream:
        # Drain the stream; the SDK accumulates the final message internally.
        for _ in stream.text_stream:
            pass
        msg = stream.get_final_message()

    # Detect truncation explicitly — otherwise the user sees an opaque JSON parse error.
    if msg.stop_reason == "max_tokens":
        usage = getattr(msg, "usage", None)
        out_tokens = getattr(usage, "output_tokens", "?") if usage else "?"
        raise RuntimeError(
            f"Claude response was truncated (stop_reason=max_tokens, "
            f"output_tokens={out_tokens}, limit={CLAUDE_MAX_TOKENS}). "
            "Increase CLAUDE_MAX_TOKENS or tighten the breakdown instructions."
        )

    raw = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()

    # Be tolerant of stray prose around the array: slice from first `[` to last `]`.
    if not raw.startswith("["):
        first, last = raw.find("["), raw.rfind("]")
        if first != -1 and last > first:
            raw = raw[first:last + 1]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        snippet = raw if len(raw) < 2000 else raw[:1000] + "\n...[truncated]...\n" + raw[-1000:]
        raise RuntimeError(
            f"Claude did not return valid JSON: {e}\n"
            f"stop_reason={msg.stop_reason}\n---\n{snippet}"
        ) from e
    if not isinstance(data, list):
        raise RuntimeError(f"Expected a JSON array, got: {type(data).__name__}")
    out = []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or "summary" not in item or "description" not in item:
            raise RuntimeError(f"Subtask {i} missing required fields: {item}")
        out.append({"summary": str(item["summary"]), "description": str(item["description"])})
    return out


# ---------- Main -----------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--jira-domain", required=True,
                   help="e.g. your-org.atlassian.net")
    p.add_argument("--jira-email", required=True)
    p.add_argument("--jira-token", required=True, help="Jira API token")
    p.add_argument("--anthropic-key", required=True)
    p.add_argument("--epic", required=True, help="Jira Epic key, e.g. PROJ-123")
    p.add_argument("--instructions", required=True, type=Path,
                   help="Path to a markdown file with breakdown instructions")
    p.add_argument("--dry-run", action="store_true",
                   help="Print proposed subtasks without creating them")
    args = p.parse_args()

    if not args.instructions.is_file():
        print(f"Instructions file not found: {args.instructions}", file=sys.stderr)
        return 2
    instructions = args.instructions.read_text(encoding="utf-8")

    jira = JiraClient(args.jira_domain, args.jira_email, args.jira_token)
    claude = Anthropic(api_key=args.anthropic_key)

    print(f"Loading epic {args.epic}...")
    epic = jira.get_issue(args.epic)
    project_key = epic["fields"]["project"]["key"]
    epic_summary = epic["fields"]["summary"]
    epic_desc = issue_description_text(epic)
    epic_text = f"# Epic {args.epic}: {epic_summary}\n\n{epic_desc}"
    print(f"  -> {epic_summary}")

    stories = jira.get_epic_stories(args.epic)
    print(f"Found {len(stories)} child stories.\n")

    for story in stories:
        key = story["key"]
        summary = story["fields"]["summary"]
        story_desc = issue_description_text(story)
        print(f"=== {key}: {summary} ===")

        story_text = f"# Story {key}: {summary}\n\n{story_desc}"
        # Per requirement 3.i: append story description to epic context (not persisted to Jira).
        combined_epic = f"{epic_text}\n\n## Story under analysis\n\n{story_desc}"

        try:
            subtasks = break_down_story(claude, instructions, combined_epic, story_text)
        except Exception as e:
            print(f"  ! Breakdown failed: {e}\n", file=sys.stderr)
            continue

        print(f"  Claude produced {len(subtasks)} subtask(s).")
        for st in subtasks:
            if args.dry_run:
                print(f"  [dry-run] {st['summary']}")
                continue
            try:
                new_key = jira.create_subtask(
                    parent_key=key, project_key=project_key,
                    summary=st["summary"], description=st["description"],
                )
                print(f"  + {new_key}: {st['summary']}")
            except Exception as e:
                print(f"  ! Failed to create subtask '{st['summary']}': {e}", file=sys.stderr)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())