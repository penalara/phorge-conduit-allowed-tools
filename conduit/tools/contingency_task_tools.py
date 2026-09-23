"""High-level workflow for creating sprint contingency tasks."""

from typing import Any, Callable, Dict, List, Optional

from fastmcp import FastMCP

from conduit.client.maniphest import ManiphestClient
from conduit.client.unified import PhabricatorClient
from conduit.tools.handlers import handle_api_errors
from conduit.tools.pagination import read_all_pages


_BACKLOG_COLUMN = "sprint backlog"
_IN_PROGRESS_COLUMN = "en curso"
_CONTINGENCY_TEXT = "contingencias"


def _name(item: Dict[str, Any]) -> Optional[str]:
    fields = item.get("fields")
    return fields.get("name") if isinstance(fields, dict) else None


def _visible_exact_column(
    columns: List[Dict[str, Any]],
    expected_name: str,
) -> List[Dict[str, str]]:
    matches = []
    for column in columns:
        fields = column.get("fields") if isinstance(column, dict) else None
        name = fields.get("name") if isinstance(fields, dict) else None
        phid = column.get("phid") if isinstance(column, dict) else None
        hidden = (
            fields.get("isHidden", False)
            if isinstance(fields, dict)
            else False
        )
        if (
            isinstance(name, str)
            and isinstance(phid, str)
            and not hidden
            and name.strip().casefold() == expected_name
        ):
            matches.append({"name": name, "phid": phid})
    return matches


def _result_error(code: str, message: str, **details: Any) -> dict:
    return {
        "success": False,
        "ok": False,
        "phase": "precheck",
        "error_code": code,
        "error": message,
        "mutationsAttempted": 0,
        **details,
    }


def register_contingency_task_tools(
    mcp: FastMCP,
    get_client_func: Callable[[], PhabricatorClient],
) -> None:
    """Register the contingency task creation workflow."""

    @mcp.tool()
    @handle_api_errors
    def pha_task_create_contingency(title: str, sprint_tag: str) -> dict:
        """Create an in-progress contingency subtask for the current user.

        The tool resolves the current user's personal sprint tag, its visible
        ``Sprint Backlog`` and ``En curso`` columns, and exactly one backlog
        task whose title contains ``contingencias``. It performs no writes
        unless every precheck succeeds.

        Args:
            title: Required title for the new contingency task.
            sprint_tag: Exact visible name of the current user's sprint tag.

        Returns:
            The created task and the resolved parent and workboard destination.
        """
        if not isinstance(title, str) or not title.strip():
            return _result_error(
                "INVALID_TITLE", "title must be a nonempty string"
            )
        if not isinstance(sprint_tag, str) or not sprint_tag.strip():
            return _result_error(
                "INVALID_SPRINT_TAG", "sprint_tag must be a nonempty string"
            )

        client = get_client_func()
        current_user = client.user.whoami()
        user_phid = (
            current_user.get("phid")
            if isinstance(current_user, dict)
            else None
        )
        user_fields = (
            current_user.get("fields")
            if isinstance(current_user, dict)
            else None
        )
        username = (
            current_user.get("username")
            if isinstance(current_user, dict)
            else None
        ) or (
            user_fields.get("username")
            if isinstance(user_fields, dict)
            else None
        )
        if not isinstance(user_phid, str) or not isinstance(username, str):
            raise ValueError("user.whoami returned incomplete user data")

        projects = read_all_pages(
            client.project.search_projects,
            "project.search",
            constraints={"query": sprint_tag},
        )
        project_matches = [
            project
            for project in projects
            if isinstance(project, dict)
            and isinstance(_name(project), str)
            and _name(project).strip() == sprint_tag.strip()
            and isinstance(project.get("phid"), str)
        ]
        if not project_matches:
            return _result_error(
                "SPRINT_TAG_NOT_FOUND",
                "Sprint tag '{}' must resolve exactly once".format(sprint_tag),
            )
        if len(project_matches) != 1:
            return _result_error(
                "AMBIGUOUS_SPRINT_TAG",
                "Sprint tag '{}' resolved more than once".format(sprint_tag),
            )
        sprint_project = project_matches[0]
        sprint_project_phid = sprint_project["phid"]

        columns = read_all_pages(
            client.project.search_columns,
            "project.column.search",
            constraints={"projects": [sprint_project_phid]},
        )
        backlog_columns = _visible_exact_column(columns, _BACKLOG_COLUMN)
        in_progress_columns = _visible_exact_column(
            columns, _IN_PROGRESS_COLUMN
        )
        if len(backlog_columns) != 1:
            return _result_error(
                "BACKLOG_COLUMN_NOT_FOUND"
                if not backlog_columns
                else "AMBIGUOUS_BACKLOG_COLUMN",
                "The sprint workboard must have exactly one visible "
                '"Sprint Backlog" column',
                columns=backlog_columns,
            )
        if len(in_progress_columns) != 1:
            return _result_error(
                "IN_PROGRESS_COLUMN_NOT_FOUND"
                if not in_progress_columns
                else "AMBIGUOUS_IN_PROGRESS_COLUMN",
                "The sprint workboard must have exactly one visible "
                '"En curso" column',
                columns=in_progress_columns,
            )

        backlog = backlog_columns[0]
        destination = in_progress_columns[0]
        tasks = read_all_pages(
            client.maniphest.search_tasks,
            "maniphest.search",
            constraints={
                "projects": [sprint_project_phid],
                "columnPHIDs": [backlog["phid"]],
            },
        )
        parents = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_title = _name(task)
            task_phid = task.get("phid")
            task_id = task.get("id")
            if (
                isinstance(task_title, str)
                and isinstance(task_phid, str)
                and isinstance(task_id, int)
                and _CONTINGENCY_TEXT in task_title.casefold()
            ):
                parents.append(
                    {"id": task_id, "phid": task_phid, "title": task_title}
                )
        if not parents:
            return _result_error(
                "CONTINGENCY_PARENT_NOT_FOUND",
                'No task containing "contingencias" was found in '
                '"Sprint Backlog"',
            )
        if len(parents) != 1:
            return _result_error(
                "AMBIGUOUS_CONTINGENCY_PARENT",
                'More than one task containing "contingencias" was found in '
                '"Sprint Backlog"',
                parents=parents,
            )

        parent = parents[0]
        transactions = [
            ManiphestClient.create_title_transaction(title.strip()),
            ManiphestClient.create_owner_transaction(user_phid),
            ManiphestClient.create_priority_transaction("normal"),
            {"type": "custom.penalara:tasktype", "value": "3"},
            ManiphestClient.create_projects_add_transaction(
                [sprint_project_phid]
            ),
            ManiphestClient.create_parent_transaction(parent["phid"]),
            ManiphestClient.create_column_transaction([destination["phid"]]),
        ]
        result = client.maniphest.edit_task(transactions=transactions)
        obj = result.get("object") if isinstance(result, dict) else None
        if not isinstance(obj, dict) or not isinstance(obj.get("id"), int):
            raise ValueError("maniphest.edit returned no task id")
        return {
            "success": True,
            "ok": True,
            "task": {
                "id": obj["id"],
                "phid": obj.get("phid"),
                "title": title.strip(),
            },
            "owner": {"username": username, "phid": user_phid},
            "sprintTag": {
                "name": _name(sprint_project),
                "phid": sprint_project_phid,
            },
            "parent": {
                "task": "T{}".format(parent["id"]),
                "title": parent["title"],
            },
            "destination": {
                "column": destination["name"],
                "columnPHID": destination["phid"],
            },
            "mutationsAttempted": 1,
        }


__all__ = ["register_contingency_task_tools"]
