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

CLAUDE_MODEL = "claude-opus-4-5"
CLAUDE_MAX_TOKENS = 8000
HTTP_TIMEOUT = 30


# ---------- Jira client ----------------------------------------------------

class JiraClient:
    def __init__(self, domain: str, email: str, api_token: str):
        self.base = f"https://{domain.rstrip('/').removeprefix('https://')}"
        self.auth = HTTPBasicAuth(email, api_token)
        self.headers = {"Accept": "application/json",
                        "Content-Type": "application/json"}

    # ---- read ----
    def get_issue(self, key: str) -> dict[str, Any]:
        r = requests.get(
            f"{self.base}/rest/api/3/issue/{key}",
            auth=self.auth, headers=self.headers, timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    def get_epic_stories(self, epic_key: str) -> list[dict[str, Any]]:
        """Return all child issues of `epic_key` (stories, tasks, bugs - excluding sub-tasks)."""
        jql = f'parent = "{epic_key}" AND issuetype != Sub-task'
        issues, start, page = [], 0, 50
        while True:
            r = requests.get(
                f"{self.base}/rest/api/3/search",
                auth=self.auth, headers=self.headers, timeout=HTTP_TIMEOUT,
                params={"jql": jql, "startAt": start, "maxResults": page,
                        "fields": "summary,description,issuetype,project"},
            )
            r.raise_for_status()
            data = r.json()
            issues.extend(data.get("issues", []))
            if start + page >= data.get("total", 0):
                break
            start += page
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
    """Flatten Atlassian Document Format to plain text (best-effort)."""
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


def _to_adf(text: str) -> dict[str, Any]:
    """Wrap plain/markdown text into a minimal ADF doc (paragraphs split on blank lines)."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    content = [
                  {"type": "paragraph",
                   "content": [{"type": "text", "text": block}]}
                  for block in blocks
              ] or [{"type": "paragraph", "content": []}]
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
    msg = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": USER_TEMPLATE.format(
                instructions=instructions, epic=epic_text, story=story_text),
        }],
    )
    raw = "".join(
        b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude did not return valid JSON: {e}\n---\n{raw}") from e
    if not isinstance(data, list):
        raise RuntimeError(f"Expected a JSON array, got: {type(data).__name__}")
    out = []
    for i, item in enumerate(data):
        if not isinstance(item,
                          dict) or "summary" not in item or "description" not in item:
            raise RuntimeError(f"Subtask {i} missing required fields: {item}")
        out.append(
            {"summary": str(item["summary"]), "description": str(item["description"])})
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
                print(f"  ! Failed to create subtask '{st['summary']}': {e}",
                      file=sys.stderr)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
