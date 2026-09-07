# ruff: noqa: FA100
"""Atomic-precheck workflow for creating a sprint from a text definition."""

import re
import secrets
import threading
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastmcp import FastMCP

from conduit.client import PhabricatorAPIError
from conduit.client.maniphest import ManiphestClient
from conduit.client.unified import PhabricatorClient
from conduit.tools.handlers import handle_api_errors
from conduit.tools.pagination import read_all_pages
from conduit.tools.sprint_definition import (
    parse_sprint_definition,
    render_sprint_remarkup,
)

_CONFIG_KEYS = {"name", "wikiBasePath"}
_USERNAME = re.compile(r"^@[A-Za-z0-9._-]+$")
_PHID = re.compile(r"\bPHID-[A-Z]+-[A-Za-z0-9]+\b", re.IGNORECASE)
_ESTIMATION_SUFFIX = re.compile(r"\s\[(?:[0-9]+(?:\.[0-9]+)?)[hD]\]$")
_PLACEHOLDER = "<CONFIGURAR>"
_DESCRIPTION = "Pendiente especificación tras crear con IA"


def _validation(errors: List[Dict[str, Any]]) -> dict:
    return {
        "success": False,
        "ok": False,
        "phase": "precheck",
        "error_code": "SPRINT_PRECHECK_FAILED",
        "error": "Sprint validation failed",
        "errors": errors,
        "mutationsAttempted": 0,
    }


def _error(errors: List[Dict[str, Any]], line: int, code: str, message: str) -> None:
    errors.append({"line": line, "code": code, "message": message})


def _fields_name(item: Any) -> Optional[str]:
    if isinstance(item, dict) and isinstance(item.get("name"), str):
        return item["name"]
    fields = item.get("fields") if isinstance(item, dict) else None
    return fields.get("name") if isinstance(fields, dict) else None


def _exact(items: List[Dict[str, Any]], name: str) -> List[Dict[str, Any]]:
    wanted = name.strip().casefold()
    return [
        item
        for item in items
        if (_fields_name(item) or "").strip().casefold() == wanted
    ]


def _exact_visible_name(
    items: List[Dict[str, Any]], name: str
) -> List[Dict[str, Any]]:
    """Match an installation-provided name without aliases or translation."""

    wanted = name.strip()
    return [
        item for item in items if (_fields_name(item) or "").strip() == wanted
    ]


def _unique(values: List[str]) -> List[str]:
    return list(dict.fromkeys(values))


def _api_error(error: Exception) -> Dict[str, Any]:
    result = {"message": str(error)}
    if isinstance(error, PhabricatorAPIError):
        if error.error_code:
            result["error_code"] = error.error_code
        if error.error_info:
            result["error_info"] = error.error_info
    return result


def _record_groups(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "created": [record for record in records if record["status"] == "created"],
        "updated": [record for record in records if record["status"] == "updated"],
        "unchanged": [record for record in records if record["status"] == "unchanged"],
    }


def _edit_object(response: Any) -> Tuple[int, str]:
    obj = response.get("object") if isinstance(response, dict) else None
    if not isinstance(obj, dict):
        obj = response if isinstance(response, dict) else {}
    identifier, phid = obj.get("id"), obj.get("phid")
    if isinstance(identifier, str) and identifier.isdigit():
        identifier = int(identifier)
    if not isinstance(identifier, int) or not isinstance(phid, str) or not phid:
        raise ValueError("maniphest.edit returned no task id/phid")
    return identifier, phid


def _task_attachment(task: Dict[str, Any], name: str, key: str) -> List[str]:
    attachments = task.get("attachments")
    attachment = attachments.get(name) if isinstance(attachments, dict) else None
    value = attachment.get(key) if isinstance(attachment, dict) else []
    return value if isinstance(value, list) else []


def _title_with_estimation(title: str, estimation: Optional[str], enabled: bool) -> str:
    if not enabled or not estimation:
        return title
    return _ESTIMATION_SUFFIX.sub("", title) + " [{}]".format(estimation)


def _structured_error(code: str, message: str) -> dict:
    return {"success": False, "ok": False, "phase": "precheck", "error_code": code,
            "error": message, "errors": [{"line": 0, "code": code, "message": message}],
            "mutationsAttempted": 0}


def _execute_sprint(plan: Dict[str, Any]) -> dict:
    definition = plan["definition"]
    client = plan["client"]
    users, priorities = plan["users"], plan["priorities"]
    projects, columns = plan["projects"], plan["columns"]
    config = plan["project_config"]
    owner_sprint_tags = plan["owner_sprint_tags"]
    publish_wiki = plan["publish_wiki"]
    append_estimation_to_title = plan["append_estimation_to_title"]
    records: List[Dict[str, Any]] = []
    warnings: List[str] = []

    def write_failure(
        error: PhabricatorAPIError,
        failed_operation: Dict[str, Any],
        pending_rows: List[Any],
    ) -> dict:
        last = None
        if records:
            record = records[-1]
            last = {
                "line": record["line"],
                "task": record["title"],
                "operation": "create" if record["status"] == "created" else "update",
            }
        return {
            "success": False,
            "ok": False,
            "phase": "execution",
            "partial": bool(records),
            "error": {
                "error_code": error.error_code,
                "error_info": error.error_info,
            },
            "lastSuccessfulOperation": last,
            "failedOperation": failed_operation,
            "pendingTasks": [row.line_number for row in pending_rows],
            "tasks": records,
            "warnings": warnings,
            "publishedWiki": False,
            **_record_groups(records),
        }

    for row in definition.rows:
        explicit = [item.name for item in row.projects]
        sprint_tag = owner_sprint_tags.get("@" + row.owner, "") if row.owner else ""
        names = list(explicit)
        if row.owner and sprint_tag:
            names = _unique(names + [sprint_tag])
        project_data = [projects[name] for name in names]
        column_data = [columns[(item.name, item.column)] for item in row.projects if item.column]
        transactions: List[Dict[str, Any]] = []
        if row.title is not None:
            title = _title_with_estimation(
                row.title, row.estimation, append_estimation_to_title
            )
            row.title = title
            transactions += [ManiphestClient.create_title_transaction(title), ManiphestClient.create_description_transaction(row.description or _DESCRIPTION), ManiphestClient.create_owner_transaction(users[row.owner]), ManiphestClient.create_priority_transaction(priorities[row.priority or "Normal"])]
            if project_data:
                transactions.append(
                    ManiphestClient.create_projects_add_transaction(
                        [item["phid"] for item in project_data]
                    )
                )
        else:
            updated_title = _title_with_estimation(
                row.existing_title or "", row.estimation, append_estimation_to_title
            )
            if updated_title != row.existing_title:
                transactions.append(ManiphestClient.create_title_transaction(updated_title))
                row.existing_title = updated_title
            if row.owner: transactions.append(ManiphestClient.create_owner_transaction(users[row.owner]))
            if row.priority: transactions.append(ManiphestClient.create_priority_transaction(priorities[row.priority]))
            if row.projects: transactions.append(ManiphestClient.create_projects_set_transaction([item["phid"] for item in project_data]))
            elif row.owner and project_data: transactions.append(ManiphestClient.create_projects_add_transaction([item["phid"] for item in project_data]))
        if row.subscribers: transactions.append(ManiphestClient.create_subscribers_set_transaction(_unique([users[name] for name in row.subscribers])))
        if row.parent_row_index is not None:
            parent = definition.rows[row.parent_row_index]
            transactions.append(ManiphestClient.create_parent_transaction(getattr(parent, "_task_phid", None)) if row.title is not None else ManiphestClient.create_parents_add_transaction([getattr(parent, "_task_phid", None)]))
        if column_data: transactions.append(ManiphestClient.create_column_transaction([item["phid"] for item in column_data]))
        if transactions:
            operation = "create" if row.title is not None else "update"
            try:
                response = client.maniphest.edit_task(object_identifier=row.task_identifier, transactions=transactions)
            except PhabricatorAPIError as error:
                position = definition.rows.index(row)
                return write_failure(
                    error,
                    {"line": row.line_number, "task": row.existing_title or row.title, "operation": operation},
                    definition.rows[position + 1:],
                )
            task_id, task_phid = _edit_object(response)
            row.final_task_identifier = "T%d" % task_id
            setattr(row, "_task_phid", task_phid)
        records.append({"line": row.line_number, "task": row.final_task_identifier, "title": row.existing_title or row.title, "status": "created" if row.title is not None else ("updated" if transactions else "unchanged"), "projects": project_data, "columns": column_data})
    if not publish_wiki:
        return {"success": True, "ok": True, "sourcePath": plan["source_path"], "sprint": definition.name, "title": definition.name, "project": None, "wiki": None, "publishedWiki": False, "tasks": records, **_record_groups(records), "warnings": warnings}
    content = render_sprint_remarkup(
        definition,
        owner_sprint_tags=owner_sprint_tags,
        append_estimation_to_title=append_estimation_to_title,
    )
    try:
        if plan["wiki_exists"]:
            wiki = client.phriction.edit_document(path=plan["wiki_path"], title=definition.name, content=content)
            wiki_operation = "editWiki"
        else:
            wiki = client.phriction.create_document(path=plan["wiki_path"], title=definition.name, content=content)
            wiki_operation = "createWiki"
    except PhabricatorAPIError as error:
        return write_failure(
            error,
            {"operation": "editWiki" if plan["wiki_exists"] else "createWiki"},
            [],
        )
    return {"success": True, "ok": True, "sourcePath": plan["source_path"], "sprint": definition.name, "title": definition.name, "project": config, "wiki": {"path": plan["wiki_path"], "title": definition.name, "result": wiki}, "publishedWiki": True, "tasks": records, **_record_groups(records), "warnings": warnings}


def register_sprint_tools(
    mcp: FastMCP, get_client_func: Callable[[], PhabricatorClient]
) -> None:
    """Register the sprint creation workflow."""

    previews: Dict[str, Dict[str, Any]] = {}
    previews_lock = threading.Lock()

    def _prepare_sprint(
        source_path: str,
        source_text: str,
        project_config: Optional[Dict[str, str]],
        owner_sprint_tags: Optional[Dict[str, str]],
        publish_wiki: bool,
        append_estimation_to_title: bool,
    ) -> dict:
        """Create a complete Phorge sprint from already-loaded Markdown.

        The operation fully prevalidates and resolves the document before it
        creates or patches tasks, applies hierarchy and Workboard columns, and
        finally creates Phriction. Call this once instead of reproducing the
        workflow with lower-level tools.
        """
        errors: List[Dict[str, Any]] = []

        if not isinstance(source_path, str) or not source_path.strip():
            _error(errors, 0, "INVALID_SOURCE_PATH", "source_path must be nonempty")
        else:
            paths = (PurePosixPath(source_path), PureWindowsPath(source_path))
            if any(path.is_absolute() or ".." in path.parts for path in paths):
                _error(
                    errors,
                    0,
                    "INVALID_SOURCE_PATH",
                    "source_path must be relative and cannot contain '..'",
                )

        if not isinstance(publish_wiki, bool):
            _error(errors, 0, "INVALID_PUBLISH_WIKI", "publish_wiki must be a boolean")
        if not isinstance(append_estimation_to_title, bool):
            _error(errors, 0, "INVALID_APPEND_ESTIMATION", "append_estimation_to_title must be a boolean")
        if publish_wiki and (
            not isinstance(project_config, dict) or set(project_config) != _CONFIG_KEYS
        ):
            _error(
                errors,
                0,
                "INVALID_PROJECT_CONFIG",
                "project_config must contain exactly name and wikiBasePath",
            )
        elif publish_wiki:
            for key in sorted(_CONFIG_KEYS):
                value = project_config.get(key)
                if (
                    not isinstance(value, str)
                    or not value.strip()
                    or _PLACEHOLDER in value
                    or _PHID.search(value)
                ):
                    _error(
                        errors,
                        0,
                        "INVALID_PROJECT_CONFIG",
                        f"project_config.{key} must be nonempty and configured",
                    )
            base_path = project_config.get("wikiBasePath", "")
            if isinstance(base_path, str):
                base_parts = PurePosixPath(base_path.replace("\\", "/"))
                if (
                    "://" in base_path
                    or base_parts.is_absolute()
                    or ".." in base_parts.parts
                ):
                    _error(
                        errors,
                        0,
                        "INVALID_WIKI_BASE_PATH",
                        "wikiBasePath must be a relative Phriction path",
                    )
        elif project_config is not None:
            _error(
                errors,
                0,
                "INVALID_PROJECT_CONFIG",
                "project_config must be omitted when publish_wiki is false",
            )

        if owner_sprint_tags is None:
            owner_sprint_tags = {}
        if not isinstance(owner_sprint_tags, dict):
            _error(errors, 0, "INVALID_OWNER_TAGS", "owner_sprint_tags must be a map")
        else:
            for owner, tag in owner_sprint_tags.items():
                if not isinstance(owner, str) or not _USERNAME.fullmatch(owner):
                    _error(
                        errors,
                        0,
                        "INVALID_OWNER_TAG",
                        "owner_sprint_tags keys must use @username syntax",
                    )
                if (
                    not isinstance(tag, str)
                    or not tag.strip()
                    or _PLACEHOLDER in tag
                    or _PHID.search(tag)
                ):
                    _error(
                        errors,
                        0,
                        "INVALID_OWNER_TAG",
                        "owner_sprint_tags values must be nonempty exact tag names",
                    )

        definition = parse_sprint_definition(
            source_text if isinstance(source_text, str) else ""
        )
        for parse_error in definition.errors:
            _error(errors, parse_error.line_number, "PARSE_ERROR", parse_error.message)
        if not definition.rows:
            _error(
                errors, 1, "EMPTY_SPRINT", "Sprint must contain at least one task row"
            )
        if not definition.slug:
            _error(errors, 1, "EMPTY_SLUG", "Sprint title must produce a nonempty slug")
        identifier_lines: Dict[str, List[int]] = {}
        for row in definition.rows:
            if row.task_identifier:
                identifier_lines.setdefault(row.task_identifier, []).append(
                    row.line_number
                )
        for identifier, lines in identifier_lines.items():
            if len(lines) > 1:
                for line in lines:
                    _error(
                        errors,
                        line,
                        "DUPLICATE_TASK",
                        "{} appears more than once in the sprint".format(identifier),
                    )
        if errors:
            return _validation(errors)

        client = get_client_func()
        existing_ids = [
            int(row.task_identifier[1:])
            for row in definition.rows
            if row.task_identifier
        ]
        existing_tasks = (
            read_all_pages(
                client.maniphest.search_tasks,
                "maniphest.search",
                constraints={"ids": existing_ids},
                attachments={"projects": True, "subscribers": True, "columns": True},
            )
            if existing_ids
            else []
        )
        by_id: Dict[Any, Dict[str, Any]] = {}
        duplicate_ids = set()
        for task in existing_tasks:
            if not isinstance(task, dict):
                continue
            task_id = task.get("id")
            if task_id in by_id:
                duplicate_ids.add(task_id)
            by_id[task_id] = task

        for row in definition.rows:
            if not row.task_identifier:
                continue
            task_id = int(row.task_identifier[1:])
            task = by_id.get(task_id)
            if task is None:
                _error(
                    errors,
                    row.line_number,
                    "TASK_NOT_FOUND",
                    f"{row.task_identifier} does not exist",
                )
                continue
            if task_id in duplicate_ids:
                _error(
                    errors,
                    row.line_number,
                    "AMBIGUOUS_TASK",
                    f"{row.task_identifier} resolved more than once",
                )
                continue
            fields = task.get("fields") if isinstance(task.get("fields"), dict) else {}
            title = fields.get("name") or fields.get("title")
            owner_phid = fields.get("ownerPHID")
            if not isinstance(task.get("phid"), str) or not isinstance(title, str):
                _error(
                    errors,
                    row.line_number,
                    "INVALID_TASK",
                    "maniphest.search returned incomplete task data",
                )
                continue
            row.existing_title = title
            row.final_task_identifier = row.task_identifier
            setattr(row, "_task_phid", task["phid"])  # noqa: B010
            setattr(
                row,
                "_existing_owner_phid",
                owner_phid if isinstance(owner_phid, str) else None,
            )  # noqa: B010
            setattr(row, "_existing_priority", fields.get("priority"))  # noqa: B010
            setattr(
                row,
                "_existing_projects",
                _task_attachment(task, "projects", "projectPHIDs"),
            )  # noqa: B010
            setattr(
                row,
                "_existing_subscribers",
                _task_attachment(task, "subscribers", "subscriberPHIDs"),
            )  # noqa: B010
            setattr(
                row,
                "_existing_columns",
                _task_attachment(task, "columns", "columnPHIDs"),
            )  # noqa: B010
            if row.owner is None and not owner_phid:
                _error(
                    errors,
                    row.line_number,
                    "OWNER_REQUIRED",
                    "Existing unowned task requires an explicit owner",
                )

        usernames = _unique(
            [
                name
                for row in definition.rows
                for name in ([row.owner] if row.owner else []) + row.subscribers
            ]
        )
        users: Dict[str, str] = {}
        for username in usernames:
            found = read_all_pages(
                client.user.search,
                "user.search",
                constraints={"usernames": [username]},
            )
            matches = [
                user
                for user in found
                if isinstance(user, dict)
                and isinstance(user.get("fields"), dict)
                and user["fields"].get("username") == username
            ]
            lines = [
                row.line_number
                for row in definition.rows
                if row.owner == username or username in row.subscribers
            ]
            if len(matches) != 1 or not isinstance(
                matches[0].get("phid") if matches else None, str
            ):
                code = "USER_NOT_FOUND" if not matches else "AMBIGUOUS_USER"
                for line in lines:
                    _error(
                        errors,
                        line,
                        code,
                        f"User @{username} must resolve exactly once",
                    )
            else:
                users[username] = matches[0]["phid"]

        old_owner_phids = _unique(
            [
                getattr(row, "_existing_owner_phid", None)
                for row in definition.rows
                if row.owner is None and getattr(row, "_existing_owner_phid", None)
            ]
        )
        old_owners: Dict[str, str] = {}
        for phid in old_owner_phids:
            found = read_all_pages(
                client.user.search, "user.search", constraints={"phids": [phid]}
            )
            matches = [
                user
                for user in found
                if isinstance(user, dict) and user.get("phid") == phid
            ]
            fields = matches[0].get("fields") if len(matches) == 1 else None
            username = fields.get("username") if isinstance(fields, dict) else None
            if len(matches) != 1 or not isinstance(username, str) or not username:
                for row in definition.rows:
                    if (
                        getattr(row, "_existing_owner_phid", None) == phid
                        and row.owner is None
                    ):
                        _error(
                            errors,
                            row.line_number,
                            "INVALID_EXISTING_OWNER",
                            "Existing owner must resolve exactly once",
                        )
            else:
                old_owners[phid] = username

        for row in definition.rows:
            if row.owner:
                row.final_owner_username = row.owner
            else:
                row.final_owner_username = old_owners.get(
                    getattr(row, "_existing_owner_phid", None)
                )

        requested_priorities = _unique(
            [
                row.priority or "Normal"
                for row in definition.rows
                if row.title is not None or row.priority is not None
            ]
        )
        priority_items = (
            client.maniphest.get_priority_info().get("data", [])
            if requested_priorities
            else []
        )
        priorities: Dict[str, str] = {}
        for requested in requested_priorities:
            matches = _exact_visible_name(
                priority_items if isinstance(priority_items, list) else [], requested
            )
            keywords = matches[0].get("keywords") if len(matches) == 1 else None
            if (
                len(matches) != 1
                or not isinstance(keywords, list)
                or not keywords
                or not isinstance(keywords[0], str)
            ):
                code = "PRIORITY_NOT_FOUND" if not matches else "AMBIGUOUS_PRIORITY"
                for row in definition.rows:
                    if (
                        row.priority or ("Normal" if row.title is not None else None)
                    ) == requested:
                        _error(
                            errors,
                            row.line_number,
                            code,
                            f"Priority '{requested}' must resolve exactly once",
                        )
            else:
                priorities[requested] = keywords[0]

        project_names: List[str] = []
        for row in definition.rows:
            project_names.extend(project.name for project in row.projects)
            if row.owner and owner_sprint_tags.get("@" + row.owner):
                project_names.append(owner_sprint_tags["@" + row.owner])
        projects: Dict[str, Dict[str, Any]] = {}
        for name in _unique([name for name in project_names if name]):
            found = read_all_pages(
                client.project.search_projects,
                "project.search",
                constraints={"query": name},
            )
            matches = _exact(found, name)
            phid = matches[0].get("phid") if len(matches) == 1 else None
            if len(matches) != 1 or not isinstance(phid, str):
                code = "PROJECT_NOT_FOUND" if not matches else "AMBIGUOUS_PROJECT"
                lines = [
                    row.line_number
                    for row in definition.rows
                    if name in [project.name for project in row.projects]
                    or (
                        row.owner
                        and name
                        == owner_sprint_tags.get("@" + row.owner)
                    )
                ] or [0]
                for line in lines:
                    _error(
                        errors,
                        line,
                        code,
                        f"Project '{name}' must resolve exactly once",
                    )
            else:
                projects[name] = {"name": _fields_name(matches[0]), "phid": phid}

        columns: Dict[Tuple[str, str], Dict[str, str]] = {}
        for row in definition.rows:
            for requested in row.projects:
                if not requested.column or requested.name not in projects:
                    continue
                key = (requested.name, requested.column)
                if key in columns:
                    continue
                found = read_all_pages(
                    client.project.search_columns,
                    "project.column.search",
                    constraints={"projects": [projects[requested.name]["phid"]]},
                )
                matches = _exact(found, requested.column)
                phid = matches[0].get("phid") if len(matches) == 1 else None
                if len(matches) != 1 or not isinstance(phid, str):
                    code = "COLUMN_NOT_FOUND" if not matches else "AMBIGUOUS_COLUMN"
                    _error(
                        errors,
                        row.line_number,
                        code,
                        "Column '{}[{}]' must resolve exactly once".format(*key),
                    )
                else:
                    columns[key] = {
                        "name": _fields_name(matches[0]) or requested.column,
                        "phid": phid,
                    }

        wiki_path = None
        wiki_exists = False
        if publish_wiki:
            base = project_config["wikiBasePath"].strip("/")
            wiki_path = f"{base}/{definition.slug}/" if base else f"{definition.slug}/"
            wiki_exists = bool(client.phriction.get_document_info(wiki_path))
        if errors:
            return _validation(errors)
        return {
            "definition": definition, "client": client, "users": users,
            "priorities": priorities, "projects": projects, "columns": columns,
            "source_path": source_path,
            "project_config": dict(project_config) if project_config else None,
            "owner_sprint_tags": dict(owner_sprint_tags),
            "publish_wiki": publish_wiki,
            "append_estimation_to_title": append_estimation_to_title,
            "wiki_path": wiki_path, "wiki_exists": wiki_exists,
        }

    @mcp.tool()
    @handle_api_errors
    def phorge_preview_sprint(
        source_path: str,
        source_text: str,
        project_config: Optional[Dict[str, str]] = None,
        owner_sprint_tags: Optional[Dict[str, str]] = None,
        publish_wiki: bool = True,
        append_estimation_to_title: bool = False,
    ) -> dict:
        """Validate and resolve a sprint without creating or updating anything."""
        prepared = _prepare_sprint(
            source_path,
            source_text,
            project_config,
            owner_sprint_tags,
            publish_wiki,
            append_estimation_to_title,
        )
        if "success" in prepared:
            return prepared
        preview_id = secrets.token_urlsafe(24)
        with previews_lock:
            previews[preview_id] = prepared
        definition = prepared["definition"]
        result = {
            "success": True, "ok": True, "previewId": preview_id,
            "publishWiki": publish_wiki,
        }
        if publish_wiki:
            result["wiki"] = {"path": prepared["wiki_path"], "title": definition.name,
                              "exists": prepared["wiki_exists"]}
            result["remarkup"] = render_sprint_remarkup(
                definition,
                preview=True,
                owner_sprint_tags=prepared["owner_sprint_tags"],
                append_estimation_to_title=append_estimation_to_title,
            )
        return result

    @mcp.tool()
    @handle_api_errors
    def phorge_create_sprint(preview_id: str) -> dict:
        """Apply one previously validated sprint preview."""
        with previews_lock:
            prepared = previews.pop(preview_id, None)
        if prepared is None:
            return _structured_error("SPRINT_PREVIEW_NOT_FOUND", "Preview was not found or was already used")
        return _execute_sprint(prepared)


__all__ = ["register_sprint_tools"]
