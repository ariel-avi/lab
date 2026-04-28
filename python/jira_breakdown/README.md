# `jira_breakdown.py` — Usage Guide

Stand-alone script. Reads a Jira Epic + its stories, asks Claude to break each story into subtasks, and creates the subtasks in Jira. Epic and stories are never modified.

## 1. Prerequisites

- Python 3.10+
- A Jira Cloud account with permission to read the Epic, read its stories, and create Sub-tasks in the same project
- An Anthropic API key

## 2. Install dependencies

The script depends on three PyPI packages only — no project setup needed.

```bash
pip install requests anthropic mistune
```

`mistune` is used to convert the markdown subtask bodies returned by Claude into Atlassian Document Format so Jira renders headings, lists, tables, code spans, and emphasis natively.

## 3. Get a Jira API token

1. Go to <https://id.atlassian.com/manage-profile/security/api-tokens>
2. Click **Create API token**, copy the value
3. Note your account email and your domain (e.g. `your-org.atlassian.net`)

## 4. Prepare the breakdown instructions file

A plain markdown file describing how subtasks should be split. Example: copy `prompts/context/workflow/story-breakdown-strategy.md` from the `prompt-lib` repo and adjust as needed. Save it as e.g. `breakdown.md`.

> The script passes this file verbatim to Claude as the breakdown contract. Claude will adapt it to the components introduced in the Epic.

## 5. Identify the Epic key

Open the Epic in Jira and copy the issue key from the URL or breadcrumb (e.g. `PROJ-123`).

## 6. Dry run (recommended first)

Prints what *would* be created, creates nothing:

```bash
python jira_breakdown.py \
  --jira-domain your-org.atlassian.net \
  --jira-email you@example.com \
  --jira-token "$JIRA_TOKEN" \
  --anthropic-key "$ANTHROPIC_API_KEY" \
  --epic PROJ-123 \
  --instructions ./breakdown.md \
  --dry-run
```

## 7. Real run

Drop `--dry-run` to actually create the subtasks:

```bash
python jira_breakdown.py \
  --jira-domain your-org.atlassian.net \
  --jira-email you@example.com \
  --jira-token "$JIRA_TOKEN" \
  --anthropic-key "$ANTHROPIC_API_KEY" \
  --epic PROJ-123 \
  --instructions ./breakdown.md
```

## 8. Inputs reference

| Flag              | Required | Description                                                          |
|-------------------|----------|----------------------------------------------------------------------|
| `--jira-domain`   | yes      | Atlassian site host, e.g. `your-org.atlassian.net`                   |
| `--jira-email`    | yes      | Email tied to the API token                                          |
| `--jira-token`    | yes      | Jira Cloud API token                                                 |
| `--anthropic-key` | yes      | Anthropic API key                                                    |
| `--epic`          | yes      | Epic issue key, e.g. `PROJ-123`                                      |
| `--instructions`  | yes      | Path to `.md` file with breakdown instructions                       |
| `--dry-run`       | no       | Print proposed subtasks without writing to Jira                      |

## 9. What the script does, step by step

1. Authenticates to Jira via HTTP Basic auth (`email` + API token)
2. Fetches the Epic (`GET /rest/api/3/issue/{epic}`) and reads its description
3. Fetches all child issues whose `parent = <epic>` and `issuetype != Sub-task` (paginated)
4. For each child story:
    1. Reads the story description and concatenates it to the Epic context (in-memory only — no writes)
    2. Calls Claude with: the breakdown instructions, the Epic context, and the story
    3. Parses Claude's JSON array of `{summary, description}` subtasks
    4. Creates each as a new `Sub-task` under the story (`POST /rest/api/3/issue`)
5. Prints a per-story log with the new subtask keys

The Epic and the stories are read-only throughout.

## 10. Troubleshooting

- **`401 Unauthorized`** — Check the email/token combo; tokens are bound to the user that created them.
- **`403 Forbidden` on subtask creation** — Your account lacks "Create Issues" permission on the project.
- **`Sub-task` issuetype not found** — Some Jira projects rename it (e.g. `Subtask`). Edit `"name": "Sub-task"` in `jira_breakdown.py` (`create_subtask`) to match your project's exact issue type name.
- **Claude returned non-JSON** — The script prints the raw response. Tighten the breakdown instructions or rerun.
- **Pagination** — `get_epic_stories` pages 50 at a time and stops when `total` is reached. No action needed.

## 11. Notes

- Descriptions sent to Jira are wrapped in minimal ADF (paragraphs split on blank lines). Markdown formatting from Claude is preserved as text but not rendered as Jira markup.
- The `parent` field is used for both Epic→Story discovery and Story→Sub-task creation — this requires a modern Jira Cloud instance (post next-gen migration). Older "Epic Link" custom-field setups are not supported.

---

# `jira_create_stories.py` — Usage Guide

Stand-alone script. Reads a `.zip` archive of Markdown files and creates one Jira **Task** per file. No AI involved — each file maps directly to a ticket.

## 1. Prerequisites

- Python 3.10+
- A Jira Cloud account with permission to create Issues in the target project

## 2. Install dependencies

```bash
pip install mistune
```

`mistune` converts Markdown to Atlassian Document Format (ADF) so Jira renders headings, lists, code blocks, and emphasis natively.

## 3. Prepare the zip archive

Create a `.zip` containing one `.md` file per story you want to create:

- **Summary** — taken from the first `# H1` heading in the file (falls back to the filename stem if none is found)
- **Description** — the rest of the file, converted to ADF

```
stories.zip
├── 01-setup-ci-pipeline.md
├── 02-add-unit-tests.md
└── 03-write-docs.md
```

Files are processed in alphabetical order.

## 4. Dry run (recommended first)

Prints the parsed summary and ADF payload for every file without touching Jira:

```bash
python jira_create_stories.py stories.zip \
  --jira-email you@example.com \
  --jira-domain your-org.atlassian.net \
  --jira-token "$JIRA_TOKEN" \
  --jira-project PROJ \
  --dry-run
```

## 5. Real run

Drop `--dry-run` to actually create the tasks:

```bash
python jira_create_stories.py stories.zip \
  --jira-email you@example.com \
  --jira-domain your-org.atlassian.net \
  --jira-token "$JIRA_TOKEN" \
  --jira-project PROJ
```

Each created ticket is printed as `created <KEY>: <summary>  (from <filename>)`.

## 6. Inputs reference

| Argument / Flag   | Required | Description                                                      |
|-------------------|----------|------------------------------------------------------------------|
| `zip_path`        | yes      | Path to the `.zip` file containing `.md` stories                 |
| `--jira-email`    | yes      | Email tied to the API token                                      |
| `--jira-domain`   | yes      | Atlassian site host, e.g. `your-org.atlassian.net`               |
| `--jira-token`    | yes      | Jira Cloud API token                                             |
| `--jira-project`  | yes      | Jira project key where Tasks will be created (e.g. `PROJ`)       |
| `--dry-run`       | no       | Print payloads without writing to Jira                           |

## 7. What the script does, step by step

1. Opens the zip and collects all `.md` files (sorted alphabetically)
2. For each file:
   1. Extracts the first `# H1` as the **summary**; drops that line from the body
   2. Converts the remaining Markdown body to ADF
   3. POSTs a new `Task` to `/rest/api/3/issue`
3. Prints a summary line: `created: N    failed: N`

## 8. Troubleshooting

- **`401 Unauthorized`** — Check the email/token combo; tokens are bound to the user that created them.
- **`403 Forbidden`** — Your account lacks "Create Issues" permission on the project.
- **No `.md` files found** — Ensure the zip contains `.md` files at the root or in subdirectories (not only directories).
- **Wrong issue type** — The script creates `Task` issues. If your project uses a different name, edit `"name": "Task"` in `create_task` to match.