# phabricator-mcp

Minimal MCP server that wraps the Phabricator Conduit API for read-only task access. Built for Claude Code integration.

## Setup

### Prerequisites

- Python 3.10+
- A Phabricator instance with Conduit API access
- A Conduit API token (generate at `https://<your-phab>/settings/user/<you>/page/apitokens/`)

### Install

```bash
cd phabricator-mcp
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PHABRICATOR_URL` | Yes | Base URL of your Phabricator instance (e.g. `https://team.phabricator.com`) |
| `PHABRICATOR_TOKEN` | Yes | Conduit API token |

### Claude Code integration

Add to your Claude Code MCP settings (`~/.claude/settings.json` or project `.claude/settings.json`):

```json
{
  "mcpServers": {
    "phabricator": {
      "command": "/home/<user>/phabricator-mcp/.venv/bin/phabricator-mcp",
      "env": {
        "PHABRICATOR_URL": "https://team.phabricator.com",
        "PHABRICATOR_TOKEN": "<your-conduit-token>"
      }
    }
  }
}
```

### Run standalone (for testing)

```bash
export PHABRICATOR_URL="https://team.phabricator.com"
export PHABRICATOR_TOKEN="<your-token>"
phabricator-mcp
```

The server uses stdio transport and speaks the MCP protocol.

## Tools

All tools are **read-only**. No write operations are exposed.

### `get_task`

Fetch a single task by ID.

```
get_task(task_id: "T1364")
get_task(task_id: "1364")     # T prefix is optional
```

**Returns:** title, status, priority, owner (PHID), URL, description.

### `get_task_comments`

Get all comments on a task, ordered newest-first.

```
get_task_comments(task_id: "T1917")
```

**Returns:** list of comments with author PHID, Unix timestamp, and raw text.

### `search_tasks`

Search tasks by free-text query, assignee, project, or status. At least one filter required.

```
search_tasks(query: "nmap")
search_tasks(assigned_to: "username", status: "open")
search_tasks(project: "Edge", status: "any", limit: 50)
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | `""` | Free-text search (title + description) |
| `assigned_to` | string | `""` | Filter by assignee username |
| `project` | string | `""` | Filter by project/tag name |
| `status` | string | `"open"` | `"open"`, `"closed"`, or `"any"` |
| `limit` | int | `20` | Max results (capped at 100) |

### `search_user`

Look up a user by username.

```
search_user(username: "username")
```

**Returns:** PHID, real name, roles.

### `get_user_tasks`

Shortcut for `search_tasks(assigned_to=username)`.

```
get_user_tasks(username: "username")
get_user_tasks(username: "username", status: "closed", limit: 50)
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `username` | string | *(required)* | Phabricator username |
| `status` | string | `"open"` | `"open"`, `"closed"`, or `"any"` |
| `limit` | int | `30` | Max results (capped at 100) |

## Notes

- Owner fields return raw PHIDs (e.g. `PHID-USER-xxx`). Use `search_user` to resolve to human names.
- Comment timestamps are Unix epoch seconds. Convert with standard tools (e.g. `date -d @1774600507`).
- The server calls Phabricator's Conduit REST API (`/api/<method>`) under the hood via `httpx`.
