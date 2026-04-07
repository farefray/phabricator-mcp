"""
Phabricator Conduit MCP Server

A minimal MCP server that wraps the Phabricator Conduit API for task
management: look up tasks, read comments, search by user/project/query,
create new tasks, and post comments.

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


@mcp.tool()
def get_user_activity(
    username: str = "",
    user_phid: str = "",
    days: int = 7,
    limit: int = 100,
    comments_only: bool = False,
) -> str:
    """Get recent Phabricator activity for a user — comments, edits, and task updates.

    Useful for generating weekly status summaries. Returns activity newest-first
    within the requested time window. If the user's PHID is already known (e.g.
    from agentic_docs/people.md), pass it via user_phid to skip the username lookup.

    Args:
        username: Phabricator username (e.g. 'max'). Ignored if user_phid is given.
        user_phid: User PHID (e.g. 'PHID-USER-...') — takes precedence over username.
        days: Days of history to retrieve (default 7).
        limit: Max activity items to return (default 100, max 500).
        comments_only: If True, only return tasks where the user left a comment,
                       and include the comment text. Filters out pure status/board
                       moves. Makes one extra API call per task found. Default False.
    """
    import time
    from datetime import datetime, timezone

    # Resolve PHID — accept direct PHID to avoid an extra round-trip when
    # the PHID is already known (e.g. from agentic_docs/people.md).
    phid = user_phid.strip() or None
    if not phid:
        if not username.strip():
            return "Provide either username or user_phid."
        phid = _resolve_user_phid(username.strip())
        if not phid:
            return f"User '{username}' not found."

    since_ts = int(time.time()) - (days * 86400)
    cap = min(limit, 500)

    # feed.query filters activity by PHID. Date filtering is client-side:
    # the API has no "since" constraint — only cursor-based pagination via
    # the "before" chronological key.
    params: dict[str, Any] = {
        "filterPHIDs[0]": phid,
        "limit": min(cap, 200),
    }

    result = conduit("feed.query", params)

    # feed.query returns a dict keyed by chronological story key, not a list.
    if not isinstance(result, dict):
        return "Unexpected response format from feed.query."

    # Collect stories within the time window, tracking earliest activity per task.
    # objectPHID is nested inside story["data"], not at the top level.
    task_activity: dict[str, dict] = {}  # objectPHID -> {first_epoch, last_epoch, count}
    for story in result.values():
        epoch = story.get("epoch", 0)
        if epoch < since_ts:
            continue
        obj_phid = (story.get("data") or {}).get("objectPHID", "")
        if not obj_phid or not obj_phid.startswith("PHID-TASK-"):
            continue  # skip non-task activity (files, users, etc.)
        if obj_phid not in task_activity:
            task_activity[obj_phid] = {"first": epoch, "last": epoch, "count": 0}
        entry = task_activity[obj_phid]
        entry["count"] += 1
        entry["first"] = min(entry["first"], epoch)
        entry["last"] = max(entry["last"], epoch)

    display_name = username.strip() or user_phid.strip()
    if not task_activity:
        return f"No task activity found for {display_name} in the last {days} days."

    # Batch-fetch task titles for all touched tasks in one API call.
    phid_list = list(task_activity.keys())[:cap]
    params_lookup: dict[str, Any] = {"limit": len(phid_list)}
    for i, p in enumerate(phid_list):
        params_lookup[f"constraints[phids][{i}]"] = p
    tasks_result = conduit("maniphest.search", params_lookup)
    title_map: dict[str, str] = {}
    status_map: dict[str, str] = {}
    id_map: dict[str, str] = {}
    for t in tasks_result.get("data", []):
        p = t.get("phid", "")
        fields = t.get("fields", {})
        title_map[p] = fields.get("name", "(no title)")
        status_map[p] = (fields.get("status", {}) or {}).get("name", "")
        id_map[p] = f"T{t.get('id', '')}"

    # Sort tasks by most recent activity first.
    sorted_tasks = sorted(phid_list, key=lambda p: task_activity[p]["last"], reverse=True)

    # --- Comments-only pass ---------------------------------------------------
    # For each task, fetch transactions and extract comments by this user.
    # Filters out tasks where the user only changed status, moved boards, etc.
    task_comments: dict[str, list[dict]] = {}  # obj_phid -> [{date, text}]
    if comments_only:
        for obj_phid in sorted_tasks:
            txn_result = conduit("transaction.search", {
                "objectIdentifier": obj_phid,
            })
            for txn in txn_result.get("data", []):
                if txn.get("authorPHID") != phid:
                    continue
                if txn.get("dateCreated", 0) < since_ts:
                    continue  # comment posted outside the time window
                comments = txn.get("comments", [])
                if not comments:
                    continue
                text = comments[0].get("content", {}).get("raw", "").strip()
                if text:
                    if obj_phid not in task_comments:
                        task_comments[obj_phid] = []
                    task_comments[obj_phid].append({
                        "date": txn.get("dateCreated", 0),
                        "text": text,
                    })
        # Keep only tasks that have actual comments; re-sort by latest comment date.
        sorted_tasks = [p for p in sorted_tasks if p in task_comments]
        sorted_tasks.sort(
            key=lambda p: max(c["date"] for c in task_comments[p]),
            reverse=True,
        )

    # --- Format output --------------------------------------------------------
    if not sorted_tasks:
        label = "comments" if comments_only else "task activity"
        return f"No {label} found for {display_name} in the last {days} days."

    mode_label = "Comments by" if comments_only else "Tasks touched by"
    lines = [
        f"# {mode_label} {display_name} — last {days} days ({len(sorted_tasks)} tasks)",
        "",
    ]
    for obj_phid in sorted_tasks:
        entry = task_activity[obj_phid]
        task_id = id_map.get(obj_phid, obj_phid)
        title = title_map.get(obj_phid, "(unknown)")
        status = status_map.get(obj_phid, "")
        status_str = f" [{status}]" if status else ""

        if comments_only:
            comments_for_task = task_comments.get(obj_phid, [])
            last_dt = datetime.fromtimestamp(
                max(c["date"] for c in comments_for_task), tz=timezone.utc
            ).strftime("%Y-%m-%d")
            lines.append(f"## {task_id}{status_str} {title}")
            lines.append(f"_{last_dt} · {len(comments_for_task)} comment(s) · {PHAB_URL}/{task_id}_")
            lines.append("")
            for c in sorted(comments_for_task, key=lambda x: x["date"]):
                dt = datetime.fromtimestamp(c["date"], tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                lines.append(f"> **{dt}**")
                # Indent comment lines for readability
                for cline in c["text"].splitlines():
                    lines.append(f"> {cline}")
                lines.append("")
        else:
            last_dt = datetime.fromtimestamp(entry["last"], tz=timezone.utc).strftime("%Y-%m-%d")
            lines.append(
                f"- **{task_id}**{status_str} {title}  "
                f"_(last active {last_dt}, {entry['count']} action(s))_"
            )
            lines.append(f"  {PHAB_URL}/{task_id}")
            lines.append("")

    return "\n".join(lines)


# --- Write tools --------------------------------------------------------------

@mcp.tool()
def create_task(
    title: str,
    description: str = "",
    priority: str = "",
    owner: str = "",
    projects: str = "",
) -> str:
    """Create a new Phabricator task (Maniphest).

    Returns the new task ID and URL on success.

    Args:
        title: Task title (required).
        description: Task description in Remarkup format.
        priority: Priority keyword: 'unbreak', 'triage', 'high', 'normal', 'low', 'wishlist'.
        owner: Username to assign the task to.
        projects: Comma-separated project/tag names to add to the task.
    """
    if not title.strip():
        return "Title is required."

    params: dict[str, Any] = {}
    idx = 0

    params[f"transactions[{idx}][type]"] = "title"
    params[f"transactions[{idx}][value]"] = title.strip()
    idx += 1

    if description:
        params[f"transactions[{idx}][type]"] = "description"
        params[f"transactions[{idx}][value]"] = description
        idx += 1

    if priority:
        valid = ("unbreak", "triage", "high", "normal", "low", "wishlist")
        p = priority.strip().lower()
        if p not in valid:
            return f"Invalid priority '{priority}'. Use one of: {', '.join(valid)}"
        params[f"transactions[{idx}][type]"] = "priority"
        params[f"transactions[{idx}][value]"] = p
        idx += 1

    if owner:
        user_phid = _resolve_user_phid(owner)
        if not user_phid:
            return f"User '{owner}' not found."
        params[f"transactions[{idx}][type]"] = "owner"
        params[f"transactions[{idx}][value]"] = user_phid
        idx += 1

    if projects:
        proj_names = [p.strip() for p in projects.split(",") if p.strip()]
        params[f"transactions[{idx}][type]"] = "projects.add"
        for j, proj_name in enumerate(proj_names):
            proj_result = conduit("project.search", {
                "constraints[query]": proj_name,
            })
            proj_data = proj_result.get("data", [])
            if not proj_data:
                return f"Project '{proj_name}' not found."
            params[f"transactions[{idx}][value][{j}]"] = proj_data[0]["phid"]
        idx += 1

    result = conduit("maniphest.edit", params)
    obj = result.get("object", {})
    task_id = obj.get("id", "")
    phid = obj.get("phid", "")

    return "\n".join([
        f"# Created: T{task_id}",
        f"**URL:** {PHAB_URL}/T{task_id}",
        f"**PHID:** {phid}",
    ])


@mcp.tool()
def add_comment(task_id: str, comment: str) -> str:
    """Add a comment to a Phabricator task.

    Args:
        task_id: The task identifier (e.g. 'T1364' or '1364').
        comment: The comment text in Remarkup format.
    """
    if not comment.strip():
        return "Comment text is required."

    clean = "".join(c for c in task_id if c.isdigit())
    if not clean:
        return f"Invalid task ID: {task_id}"

    phid = _resolve_task_phid(task_id)
    if not phid:
        return f"Task {task_id} not found."

    conduit("maniphest.edit", {
        "objectIdentifier": phid,
        "transactions[0][type]": "comment",
        "transactions[0][value]": comment,
    })

    return f"Comment added to T{clean}.\n**URL:** {PHAB_URL}/T{clean}"


# --- Entry point --------------------------------------------------------------

def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
