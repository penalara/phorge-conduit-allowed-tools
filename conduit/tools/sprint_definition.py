"""Pure parsing and rendering primitives for sprint definitions.

The input format has a header followed by semicolon-separated task rows::

    # Sprint 17
    Task title;2h;@owner;Normal;Project[Column];@subscriber;Description

Rows contain, in order, a task identifier or new title, estimation, owner,
    priority, projects, subscribers, and an optional description. The module
    deliberately has no Conduit dependencies; orchestration code can enrich
    the mutable row fields.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
import re
import unicodedata
from typing import Dict, List, Optional, Tuple


TASK_IDENTIFIER_RE = re.compile(r"^T[1-9][0-9]*$")
_TASK_LIKE_RE = re.compile(r"^t[0-9]", re.IGNORECASE)
_HEADER_RE = re.compile(r"^#(?!#) ?(\S(?:.*\S)?)\s*$")
_ESTIMATION_RE = re.compile(r"^(?:[0-9]+(?:\.[0-9]+)?)([hD])$")
_USERNAME_RE = re.compile(r"^@[A-Za-z0-9._-]+$")
_PROJECT_RE = re.compile(r"^([^\[\]]+?)(?:\[([^\[\]]+)\])?$")
_DATE_RANGE_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})-(\d{2}/\d{2}/\d{4})$")
_ESTIMATION_SUFFIX_RE = re.compile(r"\s\[(?:[0-9]+(?:\.[0-9]+)?)[hD]\]$")


@dataclass(frozen=True)
class SprintParseError:
    """A validation error tied to a physical input line."""

    line_number: int
    message: str


@dataclass(frozen=True)
class SprintProject:
    """A requested project and, optionally, an exact workboard column."""

    name: str
    column: Optional[str] = None


@dataclass
class SprintTaskRow:
    """One parsed task row, including fields populated by orchestration.

    ``existing_title``, ``final_owner_username``, and
    ``final_task_identifier`` are intentionally assignable after API lookup or
    task creation.  Status and actual time are also renderer inputs.
    """

    row_index: int
    line_number: int
    level: int
    parent_row_index: Optional[int]
    title: Optional[str]
    task_identifier: Optional[str]
    estimation: Optional[str]
    estimation_hours: Optional[Decimal]
    owner: Optional[str]
    priority: Optional[str]
    description: Optional[str] = None
    subscribers: List[str] = field(default_factory=list)
    projects: List[SprintProject] = field(default_factory=list)
    existing_title: Optional[str] = None
    final_owner_username: Optional[str] = None
    final_task_identifier: Optional[str] = None
    status: str = ""
    actual_time: str = ""


@dataclass
class SprintDefinition:
    """The best-effort result of parsing a sprint definition."""

    name: Optional[str]
    slug: Optional[str]
    rows: List[SprintTaskRow] = field(default_factory=list)
    errors: List[SprintParseError] = field(default_factory=list)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    workdays: Optional[int] = None

    @property
    def is_valid(self) -> bool:
        return not self.errors


def normalize_slug(value: str) -> str:
    """Return a lowercase ASCII slug, removing diacritics and punctuation."""

    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    words = re.sub(r"[^A-Za-z0-9]+", "-", ascii_value.lower())
    return words.strip("-")


def _parse_estimation(
    value: str, line_number: int, errors: List[SprintParseError]
) -> Tuple[Optional[str], Optional[Decimal]]:
    if not value:
        return None, None
    match = _ESTIMATION_RE.fullmatch(value)
    if not match:
        errors.append(
            SprintParseError(
                line_number,
                "Estimation must be an integer or decimal followed by h or D",
            )
        )
        return value, None
    try:
        amount = Decimal(value[:-1])
    except InvalidOperation:
        errors.append(SprintParseError(line_number, "Invalid estimation"))
        return value, None
    if match.group(1) == "D":
        amount *= Decimal(8)
    return value, amount


def _parse_user(
    value: str,
    label: str,
    line_number: int,
    errors: List[SprintParseError],
) -> Optional[str]:
    if not value:
        return None
    if not _USERNAME_RE.fullmatch(value):
        errors.append(
            SprintParseError(line_number, "%s must use @username syntax" % label)
        )
        return None
    return value[1:]


def _parse_subscribers(
    value: str, line_number: int, errors: List[SprintParseError]
) -> List[str]:
    if not value:
        return []
    result = []
    for item in value.split(","):
        item = item.strip()
        subscriber = _parse_user(item, "Subscriber", line_number, errors)
        if subscriber is not None:
            result.append(subscriber)
    return result


def _parse_projects(
    value: str, line_number: int, errors: List[SprintParseError]
) -> List[SprintProject]:
    if not value:
        return []
    result = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            errors.append(
                SprintParseError(line_number, "Project entry cannot be empty")
            )
            continue
        match = _PROJECT_RE.fullmatch(item)
        if not match:
            errors.append(
                SprintParseError(
                    line_number,
                    "Project must use Name or exact Name[Column] syntax",
                )
            )
            continue
        name = match.group(1).strip()
        column = match.group(2)
        column = column.strip() if column is not None else None
        if not name or column == "":
            errors.append(
                SprintParseError(line_number, "Project name and column cannot be empty")
            )
            continue
        result.append(SprintProject(name=name, column=column))
    return result


def _workdays(start_date: date, end_date: date) -> int:
    """Count Monday through Friday from start inclusive to end exclusive."""
    count = 0
    current = start_date
    while current < end_date:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def _parse_date_range(
    value: str, line_number: int, errors: List[SprintParseError]
) -> Tuple[Optional[date], Optional[date], Optional[int]]:
    match = _DATE_RANGE_RE.fullmatch(value)
    if not match:
        return None, None, None
    try:
        start_date = datetime.strptime(match.group(1), "%d/%m/%Y").date()
        end_date = datetime.strptime(match.group(2), "%d/%m/%Y").date()
    except ValueError:
        errors.append(SprintParseError(line_number, "Sprint dates must be valid dd/mm/yyyy values"))
        return None, None, None
    if end_date <= start_date:
        errors.append(SprintParseError(line_number, "Sprint end date must be after start date"))
        return None, None, None
    return start_date, end_date, _workdays(start_date, end_date)


def parse_sprint_definition(text: str) -> SprintDefinition:
    """Parse ``text`` without raising for user input errors.

    Blank lines are ignored, including before the header. Every nonblank task
    row is retained so callers can show all errors at once and row indexes
    remain stable.
    """

    lines = text.splitlines()
    errors: List[SprintParseError] = []
    header_index = next(
        (index for index, line in enumerate(lines) if line.strip()), None
    )
    name = None
    if header_index is None:
        errors.append(SprintParseError(1, "Missing sprint header"))
    else:
        header_line_number = header_index + 1
        header = _HEADER_RE.fullmatch(lines[header_index])
        if header:
            name = header.group(1).strip()
            if not name:
                errors.append(
                    SprintParseError(header_line_number, "Sprint title cannot be empty")
                )
                name = None
        else:
            errors.append(
                SprintParseError(
                    header_line_number,
                    "First nonblank line must use '#Title' or '# Title'",
                )
            )

    rows: List[SprintTaskRow] = []
    levels: Dict[int, int] = {}
    previous_level = 0
    start = (header_index + 1) if header_index is not None else len(lines)
    start_date = None
    end_date = None
    workdays = None
    date_line_index = next(
        (index for index in range(start, len(lines)) if lines[index].strip()), None
    )
    if date_line_index is not None:
        date_value = lines[date_line_index].strip()
        start_date, end_date, workdays = _parse_date_range(
            date_value, date_line_index + 1, errors
        )
        if _DATE_RANGE_RE.fullmatch(date_value):
            start = date_line_index + 1
    for line_number, physical_line in enumerate(lines[start:], start=start + 1):
        if not physical_line.strip():
            continue

        prefix_match = re.match(r"^[ \t]*", physical_line)
        prefix = prefix_match.group(0) if prefix_match else ""
        expanded_prefix = prefix.replace("\t", "    ")
        leading_spaces = len(expanded_prefix)
        level = leading_spaces // 4
        if leading_spaces % 4:
            errors.append(
                SprintParseError(
                    line_number, "Indentation must be a multiple of four spaces"
                )
            )
        if level > previous_level + 1:
            errors.append(SprintParseError(line_number, "Hierarchy level cannot jump"))

        content = physical_line[len(prefix) :]
        columns = [part.strip() for part in content.split(";")]
        if len(columns) > 7:
            errors.append(
                SprintParseError(
                    line_number, "Task row cannot have more than seven columns"
                )
            )
            columns = columns[:7]
        columns.extend([""] * (7 - len(columns)))
        (
            task_value,
            estimation_value,
            owner_value,
            priority_value,
            project_value,
            subscriber_value,
            description_value,
        ) = columns

        title = None
        task_identifier = None
        if not task_value:
            errors.append(
                SprintParseError(line_number, "Task title or identifier is required")
            )
        elif TASK_IDENTIFIER_RE.fullmatch(task_value):
            task_identifier = task_value
        elif _TASK_LIKE_RE.match(task_value):
            errors.append(
                SprintParseError(
                    line_number, "Malformed task identifier: %s" % task_value
                )
            )
        else:
            title = task_value

        estimation, estimation_hours = _parse_estimation(
            estimation_value, line_number, errors
        )
        owner = _parse_user(owner_value, "Owner", line_number, errors)
        if title is not None and owner is None and not owner_value:
            errors.append(
                SprintParseError(line_number, "Owner is required for a new task")
            )
        subscribers = _parse_subscribers(subscriber_value, line_number, errors)
        projects = _parse_projects(project_value, line_number, errors)

        row_index = len(rows)
        parent_row_index = levels.get(level - 1) if level else None
        if level and parent_row_index is None:
            errors.append(
                SprintParseError(
                    line_number, "Indented task has no level-%d parent" % (level - 1)
                )
            )
        row = SprintTaskRow(
            row_index=row_index,
            line_number=line_number,
            level=level,
            parent_row_index=parent_row_index,
            title=title,
            task_identifier=task_identifier,
            estimation=estimation,
            estimation_hours=estimation_hours,
            owner=owner,
            priority=priority_value or None,
            description=(description_value or None) if title is not None else None,
            subscribers=subscribers,
            projects=projects,
        )
        rows.append(row)
        levels[level] = row_index
        for stale_level in [known for known in levels if known > level]:
            del levels[stale_level]
        previous_level = level

    return SprintDefinition(
        name=name,
        slug=normalize_slug(name) if name is not None else None,
        rows=rows,
        errors=errors,
        start_date=start_date,
        end_date=end_date,
        workdays=workdays,
    )


def render_sprint_remarkup(
    definition: SprintDefinition,
    preview: bool = False,
    owner_sprint_tags: Optional[Dict[str, str]] = None,
    append_estimation_to_title: bool = False,
) -> str:
    """Render sprint rows as HTML Remarkup grouped by final owner.

    Owner groups follow the first appearance of each resolved owner.  No sprint
    title heading is emitted, allowing callers to place the table in any page.
    Only parent relationships declared by the sprint document are represented.
    """

    owner_sprint_tags = owner_sprint_tags or {}
    groups: Dict[str, List[SprintTaskRow]] = {}
    order: List[str] = []
    for row in definition.rows:
        owner = row.final_owner_username or row.owner
        if owner is None:
            raise ValueError(
                "Every sprint row must have a final owner before rendering"
            )
        if owner not in groups:
            groups[owner] = []
            order.append(owner)
        groups[owner].append(row)

    visual_depths: Dict[int, int] = {}

    def owner_for(row: SprintTaskRow) -> str:
        owner = row.final_owner_username or row.owner
        if owner is None:
            raise ValueError("Every sprint row must have a final owner before rendering")
        return owner

    def visual_depth(row: SprintTaskRow) -> int:
        if row.row_index in visual_depths:
            return visual_depths[row.row_index]
        if row.parent_row_index is None:
            depth = 0
        else:
            parent = definition.rows[row.parent_row_index]
            depth = visual_depth(parent) + 1 if owner_for(parent) == owner_for(row) else 0
        visual_depths[row.row_index] = depth
        return depth

    def rendered_title(row: SprintTaskRow) -> str:
        title = row.existing_title or row.title or ""
        if append_estimation_to_title and row.estimation:
            title = _ESTIMATION_SUFFIX_RE.sub("", title) + " [{}]".format(
                row.estimation
            )
        title = escape(title)
        if row.parent_row_index is None:
            return title
        parent = definition.rows[row.parent_row_index]
        if owner_for(parent) != owner_for(row):
            parent_code = escape(
                parent.final_task_identifier
                or parent.task_identifier
                or ("TODO" if preview and parent.title is not None else "")
            )
            return "(Hija de %s) %s" % (parent_code, title)
        return "⭢" * visual_depth(row) + " " + title

    chunks: List[str] = []
    if definition.start_date is not None and definition.end_date is not None:
        duration = definition.workdays or 0
        duration_label = "{} {}".format(
            duration, "día" if duration == 1 else "días"
        )
        chunks.extend(
            [
                "===Fechas===",
                "",
                "Inicio: " + definition.start_date.strftime("%d/%m/%Y"),
                "Fin: " + definition.end_date.strftime("%d/%m/%Y"),
                "",
                "**Duraciones Sprint**",
                "",
                "Sprint: " + duration_label,
            ]
        )
        chunks.extend("@{}: {}".format(owner, duration_label) for owner in order)
        chunks.append("")
    for owner in order:
        sprint_tag = owner_sprint_tags.get("@" + owner)
        heading = "@%s" % escape(owner)
        if sprint_tag:
            heading += " : #%s" % sprint_tag.lower().replace(" ", "_")
        chunks.append("== %s ==" % heading)
        chunks.append("<table>")
        chunks.append(
            "<tr><th>Título tarea</th><th>Código</th><th>Estimación</th>"
            "<th>Tiempo real</th><th>Observaciones</th></tr>"
        )
        for row in groups[owner]:
            identifier = (
                row.final_task_identifier
                or row.task_identifier
                or ("TODO" if preview and row.title is not None else "")
            )
            values = [
                rendered_title(row),
                escape(identifier),
                escape(row.estimation or ""),
                escape(row.actual_time),
                "",
            ]
            chunks.append(
                "<tr>%s</tr>"
                % "".join("<td>%s</td>" % value for value in values)
            )
        chunks.append("</table>")
    return "\n".join(chunks)


__all__ = [
    "SprintDefinition",
    "SprintParseError",
    "SprintProject",
    "SprintTaskRow",
    "TASK_IDENTIFIER_RE",
    "normalize_slug",
    "parse_sprint_definition",
    "render_sprint_remarkup",
]
