"""
Phabricator Conduit MCP Server

A minimal MCP server that wraps the Phabricator Conduit API for read-oriented
task management: look up tasks, read comments, search by user/project/query.

Env vars:
  PHABRICATOR_URL   - Base URL (e.g. https://team.rootevidence.com)
  PHABRICATOR_TOKEN - Conduit API token
"""

import os
import logging
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# --- Setup -------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("phabricator-mcp")

mcp = FastMCP("phabricator")

PHAB_URL = os.environ.get("PHABRICATOR_URL", "").rstrip("/")
PHAB_TOKEN = os.environ.get("PHABRICATOR_TOKEN", "")


# --- Conduit client -----------------------------------------------------------

def conduit(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a Phabricator Conduit method. Raises on HTTP or Conduit errors."""
    if not PHAB_URL or not PHAB_TOKEN:
        raise RuntimeError(
            "PHABRICATOR_URL and PHABRICATOR_TOKEN env vars must be set"
        )

    payload = dict(params or {})
    payload["api.token"] = PHAB_TOKEN

    resp = httpx.post(
        f"{PHAB_URL}/api/{method}",
        data=payload,
        timeout=30.0,
    )
    resp.raise_for_status()
    body = resp.json()

    if body.get("error_code"):
        raise RuntimeError(
            f"Conduit {method} error: {body.get('error_info', 'unknown')}"
        )
    return body["result"]


# --- PHID helpers -------------------------------------------------------------

def _resolve_user_phid(username: str) -> str | None:
    """Resolve a username to its PHID."""
    result = conduit("user.search", {
        "constraints[usernames][0]": username.strip().lower(),
    })
    data = result.get("data", [])
    return data[0]["phid"] if data else None


def _resolve_task_phid(task_id: str) -> str | None:
    """Resolve a Txxxx or numeric task ID to its PHID."""
    clean = "".join(c for c in task_id if c.isdigit())
    if not clean:
        return None
    result = conduit("maniphest.search", {
        "constraints[ids][0]": clean,
    })
    data = result.get("data", [])
    return data[0]["phid"] if data else None


# --- Formatting helpers -------------------------------------------------------

def _format_task(task: dict) -> dict:
    """Extract the useful fields from a Conduit task object."""
    fields = task.get("fields", {})
    task_id = task.get("id", "")
    return {
        "id": f"T{task_id}",
        "phid": task.get("phid", ""),
        "title": fields.get("name", ""),
        "description": (fields.get("description", {}) or {}).get("raw", ""),
        "status": (fields.get("status", {}) or {}).get("name", ""),
        "priority": (fields.get("priority", {}) or {}).get("name", ""),
        "owner_phid": fields.get("ownerPHID", ""),
        "url": f"{PHAB_URL}/T{task_id}",
        "dateCreated": fields.get("dateCreated"),
        "dateModified": fields.get("dateModified"),
    }


def _format_comment(txn: dict) -> dict | None:
    """Extract a comment from a transaction, if it has one."""
    comments = txn.get("comments", [])
    if not comments:
        return None
    c = comments[0]
    return {
        "id": txn.get("id"),
        "author_phid": txn.get("authorPHID", ""),
        "date": txn.get("dateCreated"),
        "text": c.get("content", {}).get("raw", ""),
    }


# --- MCP Tools ----------------------------------------------------------------

@mcp.tool()
def get_task(task_id: str) -> str:
    """Get a Phabricator task by ID (e.g. 'T1364' or '1364').

    Returns task title, description, status, priority, owner, and URL.

    Args:
        task_id: The task identifier, with or without the T prefix.
    """
    clean = "".join(c for c in task_id if c.isdigit())
    if not clean:
        return f"Invalid task ID: {task_id}"

    result = conduit("maniphest.search", {
        "constraints[ids][0]": clean,
    })
    data = result.get("data", [])
    if not data:
        return f"Task {task_id} not found."

    task = _format_task(data[0])

    lines = [
        f"# {task['id']}: {task['title']}",
        f"**Status:** {task['status']}",
        f"**Priority:** {task['priority']}",
        f"**Owner:** {task['owner_phid'] or 'Unassigned'}",
        f"**URL:** {task['url']}",
        "",
        "## Description",
        task["description"] or "(no description)",
    ]
    return "\n".join(lines)


@mcp.tool()
def get_task_comments(task_id: str) -> str:
    """Get comments on a Phabricator task.

    Returns an ordered list of comments with author PHID and date.

    Args:
        task_id: The task identifier, with or without the T prefix.
    """
    phid = _resolve_task_phid(task_id)
    if not phid:
        return f"Task {task_id} not found."

    result = conduit("transaction.search", {
        "objectIdentifier": phid,
    })
    data = result.get("data", [])

    comments = []
    for txn in data:
        c = _format_comment(txn)
        if c:
            comments.append(c)

    if not comments:
        return f"No comments on {task_id}."

    lines = [f"# Comments on {task_id} ({len(comments)} total)", ""]
    for c in comments:
        lines.append(f"### Comment #{c['id']} by {c['author_phid']}")
        lines.append(f"**Date:** {c['date']}")
        lines.append("")
        lines.append(c["text"])
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def search_tasks(
    query: str = "",
    assigned_to: str = "",
    project: str = "",
    status: str = "open",
    limit: int = 20,
) -> str:
    """Search Phabricator tasks by query, assignee, project, or status.

    At least one of query, assigned_to, or project must be provided.

    Args:
        query: Free-text search query (searches title and description).
        assigned_to: Filter by assignee username.
        project: Filter by project/tag name.
        status: Filter by status: 'open', 'closed', or 'any'. Default 'open'.
        limit: Max results to return (default 20, max 100).
    """
    if not query and not assigned_to and not project:
        return "Provide at least one of: query, assigned_to, or project."

    params: dict[str, Any] = {"limit": min(limit, 100)}

    if query:
        params["constraints[query]"] = query

    if assigned_to:
        user_phid = _resolve_user_phid(assigned_to)
        if not user_phid:
            return f"User '{assigned_to}' not found."
        params["constraints[assigned][0]"] = user_phid

    if project:
        proj_result = conduit("project.search", {
            "constraints[query]": project.strip(),
        })
        proj_data = proj_result.get("data", [])
        if not proj_data:
            return f"Project '{project}' not found."
        params["constraints[projects][0]"] = proj_data[0]["phid"]

    if status == "open":
        params["constraints[statuses][0]"] = "open"
    elif status == "closed":
        params["constraints[statuses][0]"] = "resolved"
        params["constraints[statuses][1]"] = "wontfix"
        params["constraints[statuses][2]"] = "invalid"
        params["constraints[statuses][3]"] = "duplicate"
    # status == "any" -> no constraint

    params["order"] = "newest"

    result = conduit("maniphest.search", params)
    data = result.get("data", [])

    if not data:
        return "No tasks found matching the search criteria."

    lines = [f"# Search results ({len(data)} tasks)", ""]
    for task_raw in data:
        t = _format_task(task_raw)
        lines.append(
            f"- **{t['id']}** [{t['status']}] {t['title']}  "
            f"(Priority: {t['priority']}, Owner: {t['owner_phid'] or 'Unassigned'})"
        )
    return "\n".join(lines)


@mcp.tool()
def search_user(username: str) -> str:
    """Look up a Phabricator user by username.

    Returns their PHID, real name, and roles.

    Args:
        username: The Phabricator username to search for.
    """
    result = conduit("user.search", {
        "constraints[usernames][0]": username.strip().lower(),
    })
    data = result.get("data", [])
    if not data:
        return f"User '{username}' not found."

    user = data[0]
    fields = user.get("fields", {})
    roles = ", ".join(fields.get("roles", [])) or "none"

    lines = [
        f"# User: {fields.get('username', username)}",
        f"**Real name:** {fields.get('realName', 'N/A')}",
        f"**PHID:** {user.get('phid', 'N/A')}",
        f"**Roles:** {roles}",
    ]
    return "\n".join(lines)


@mcp.tool()
def get_user_tasks(username: str, status: str = "open", limit: int = 30) -> str:
    """Get all tasks assigned to a specific user.

    Args:
        username: The Phabricator username.
        status: Filter by status: 'open', 'closed', or 'any'. Default 'open'.
        limit: Max results to return (default 30, max 100).
    """
    return search_tasks(assigned_to=username, status=status, limit=limit)


# --- Entry point --------------------------------------------------------------

def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
