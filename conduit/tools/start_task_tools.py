"""High-level Maniphest workflow tools."""

import re
from typing import Any, Callable, Dict, Iterable, List, TypedDict

from fastmcp import FastMCP

from conduit.client.unified import PhabricatorClient
from conduit.tools.handlers import handle_api_errors
from conduit.tools.pagination import page_data, read_all_pages


_TASK_IDENTIFIER = re.compile(r"^T[1-9]\d*$")
_TARGET_COLUMN = "en curso"


class _MovedDestination(TypedDict):
    project: str
    projectPHID: str
    column: str
    columnPHID: str


class _SkippedProject(TypedDict):
    project: str
    projectPHID: str
    reason: str


def _task_project_phids(task_data: Dict[str, Any]) -> List[str]:
    """Read project tags from the modern attachment, with a fields fallback."""
    attachments = task_data.get("attachments")
    project_attachment = (
        attachments.get("projects") if isinstance(attachments, dict) else None
    )
    project_phids = (
        project_attachment.get("projectPHIDs")
        if isinstance(project_attachment, dict)
        else task_data.get("fields", {}).get("projectPHIDs")
    )

    if project_phids is None:
        return []
    if not isinstance(project_phids, list) or not all(
        isinstance(phid, str) and phid.startswith("PHID-PROJ-")
        for phid in project_phids
    ):
        raise ValueError("maniphest.search returned invalid task project tags")

    # Preserve Maniphest order while avoiding duplicate column destinations.
    return list(dict.fromkeys(project_phids))


def _project_names(
    client: PhabricatorClient, project_phids: Iterable[str]
) -> Dict[str, str]:
    projects = read_all_pages(
        client.project.search_projects,
        "project.search",
        constraints={"phids": list(project_phids)},
    )
    names: Dict[str, str] = {}
    for project in projects:
        if not isinstance(project, dict):
            raise ValueError("project.search returned an invalid project")
        phid = project.get("phid")
        fields = project.get("fields")
        name = fields.get("name") if isinstance(fields, dict) else None
        if isinstance(phid, str) and isinstance(name, str):
            names[phid] = name
    return names


def _matching_columns(columns: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    matches = []
    for column in columns:
        if not isinstance(column, dict):
            raise ValueError("project.column.search returned an invalid column")
        phid = column.get("phid")
        fields = column.get("fields")
        name = fields.get("name") if isinstance(fields, dict) else None
        if not isinstance(phid, str) or not isinstance(name, str):
            raise ValueError("project.column.search returned an invalid column")
        if name.strip().casefold() == _TARGET_COLUMN:
            matches.append({"name": name, "phid": phid})
    return matches


def register_start_task_tools(
    mcp: FastMCP,
    get_client_func: Callable[[], PhabricatorClient],
) -> None:
    """Register high-level task-start workflow tools."""

    @mcp.tool()
    @handle_api_errors
    def pha_task_start(task: str) -> dict:
        """
        Put a Maniphest task in "En curso" on every applicable Workboard.

        The tool discovers the task's project tags, finds an exact "En curso"
        column in each associated Workboard, and moves the task to every such
        column in one Maniphest transaction. Projects without that column are
        reported as skipped. Use task as a Maniphest identifier in T123 format.

        Args:
            task: Required Maniphest identifier in T123 format.

        Returns:
            A structured result with movedTo destinations and skipped projects.
        """
        if not isinstance(task, str) or not _TASK_IDENTIFIER.fullmatch(task):
            return {
                "success": False,
                "ok": False,
                "error_code": "VALIDATION_ERROR",
                "error": "task must be a Maniphest identifier in T123 format",
            }

        client = get_client_func()
        task_id = int(task[1:])
        task_result = client.maniphest.search_tasks(
            constraints={"ids": [task_id]},
            attachments={"projects": True},
            limit=1,
        )
        tasks, _ = page_data(task_result, "maniphest.search")
        if not tasks:
            return {
                "success": False,
                "ok": False,
                "task": task,
                "error_code": "NOT_FOUND",
                "error": "Maniphest task {} does not exist".format(task),
            }
        if len(tasks) != 1 or not isinstance(tasks[0], dict):
            raise ValueError("maniphest.search returned an inconsistent task response")

        project_phids = _task_project_phids(tasks[0])
        if not project_phids:
            return {
                "success": True,
                "ok": False,
                "task": task,
                "moved": False,
                "movedTo": [],
                "skipped": [],
                "warning": (
                    "La tarea {} no tiene proyectos ni tags asociados.".format(task)
                ),
            }

        project_names = _project_names(client, project_phids)
        destinations: List[_MovedDestination] = []
        skipped: List[_SkippedProject] = []
        ambiguities = []

        # Complete every read and configuration check before the only write.
        for project_phid in project_phids:
            project_name = project_names.get(project_phid, project_phid)
            columns = read_all_pages(
                client.project.search_columns,
                "project.column.search",
                constraints={"projects": [project_phid]},
            )
            matches = _matching_columns(columns)
            if len(matches) > 1:
                ambiguities.append(
                    {
                        "project": project_name,
                        "projectPHID": project_phid,
                        "columns": matches,
                    }
                )
            elif not matches:
                skipped.append(
                    {
                        "project": project_name,
                        "projectPHID": project_phid,
                        "reason": (
                            "El proyecto no tiene un Workboard con columnas"
                            if not columns
                            else 'No existe la columna "En curso"'
                        ),
                    }
                )
            else:
                destinations.append(
                    {
                        "project": project_name,
                        "projectPHID": project_phid,
                        "column": "En curso",
                        "columnPHID": matches[0]["phid"],
                    }
                )

        if ambiguities:
            return {
                "success": False,
                "ok": False,
                "task": task,
                "error_code": "AMBIGUOUS_COLUMN",
                "error": ('Se encontraron varias columnas "En curso" en un Workboard.'),
                "ambiguities": ambiguities,
            }

        if not destinations:
            return {
                "success": True,
                "ok": False,
                "task": task,
                "moved": False,
                "movedTo": [],
                "skipped": skipped,
                "warning": (
                    "Ninguno de los Workboards asociados a los tags de {} "
                    'tiene una columna "En curso".'
                ).format(task),
            }

        transaction = client.maniphest.create_column_transaction(
            [destination["columnPHID"] for destination in destinations]
        )
        client.maniphest.edit_task(object_identifier=task, transactions=[transaction])
        return {
            "success": True,
            "ok": True,
            "task": task,
            "moved": True,
            "column": "En curso",
            "movedTo": destinations,
            "skipped": skipped,
        }
