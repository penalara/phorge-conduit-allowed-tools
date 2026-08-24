"""Pure unit tests for sprint definition parsing and rendering."""

from decimal import Decimal

import pytest

from conduit.tools.sprint_definition import (
    SprintDefinition,
    SprintProject,
    SprintTaskRow,
    normalize_slug,
    parse_sprint_definition,
    render_sprint_remarkup,
)


@pytest.mark.parametrize("header", ["#Sprint 17", "# Sprint 17"])
def test_parses_header_on_first_nonblank_line_and_ignores_blank_lines(header):
    result = parse_sprint_definition(
        "\n  \n%s\n\nNew task;;@alice;;;\n   \nT42;;;;;" % header
    )

    assert result.is_valid
    assert result.name == "Sprint 17"
    assert result.slug == "sprint-17"
    assert [row.title for row in result.rows] == ["New task", None]
    assert result.rows[1].task_identifier == "T42"
    assert [row.line_number for row in result.rows] == [5, 7]


def test_header_must_be_the_first_nonblank_line():
    result = parse_sprint_definition("\nSprint 17\nTask")

    assert not result.is_valid
    assert result.name is None
    assert result.errors[0].line_number == 2
    assert "First nonblank line" in result.errors[0].message


@pytest.mark.parametrize(
    "header", ["#  Sprint 17", "#\tSprint 17", "## Sprint 17"]
)
def test_rejects_unsupported_header_spacing(header):
    result = parse_sprint_definition("%s\nTask;;@alice" % header)

    assert not result.is_valid
    assert result.name is None
    assert any(
        "First nonblank line" in error.message for error in result.errors
    )


def test_short_rows_are_padded_and_six_columns_are_parsed():
    result = parse_sprint_definition(
        "# Sprint Alfa\nCreate report;1.5h;@alice;High;"
        "Core, Delivery[Doing];@bob, @carol"
    )

    row = result.rows[0]
    assert result.is_valid
    assert row.title == "Create report"
    assert row.estimation == "1.5h"
    assert row.estimation_hours == Decimal("1.5")
    assert row.owner == "alice"
    assert row.priority == "High"
    assert row.subscribers == ["bob", "carol"]
    assert row.projects == [
        SprintProject("Core"),
        SprintProject("Delivery", "Doing"),
    ]
    assert row.description is None


def test_seventh_column_is_a_description_for_new_tasks_only():
    result = parse_sprint_definition(
        "# Sprint 1\nTask;;@alice;;;;Task description\nT123;;;;;;Ignored"
    )

    assert result.is_valid
    assert result.rows[0].description == "Task description"
    assert result.rows[1].description is None


def test_more_than_seven_columns_is_reported_without_raising():
    result = parse_sprint_definition("# Sprint 1\nTask;;@alice;;;;extra;more")

    assert len(result.rows) == 1
    assert any("more than seven" in error.message for error in result.errors)


@pytest.mark.parametrize("identifier", ["T0", "t123", "T123abc"])
def test_rejects_task_like_values_that_are_not_exact_identifiers(identifier):
    result = parse_sprint_definition("# Sprint 1\n%s;;@alice" % identifier)

    assert result.rows[0].task_identifier is None
    assert result.rows[0].title is None
    assert "Malformed task identifier" in result.errors[0].message


def test_non_task_like_text_is_a_new_title():
    result = parse_sprint_definition("# Sprint 1\nTriage customer report;;@alice")

    assert result.is_valid
    assert result.rows[0].title == "Triage customer report"


@pytest.mark.parametrize(
    "source, original, hours",
    [
        ("2h", "2h", Decimal("2")),
        ("0.25h", "0.25h", Decimal("0.25")),
        ("1D", "1D", Decimal("8")),
        ("1.5D", "1.5D", Decimal("12.0")),
    ],
)
def test_estimation_preserves_source_and_converts_days(source, original, hours):
    result = parse_sprint_definition("# Sprint 1\nTask;%s;@alice" % source)

    assert result.is_valid
    assert result.rows[0].estimation == original
    assert result.rows[0].estimation_hours == hours


@pytest.mark.parametrize("value", ["one h", "2", "h", ".5h", "1d"])
def test_rejects_invalid_estimations(value):
    result = parse_sprint_definition("# Sprint 1\nTask;%s;@alice" % value)

    assert result.rows[0].estimation_hours is None
    assert any("Estimation" in error.message for error in result.errors)


def test_owner_subscribers_and_projects_collect_multiple_errors():
    result = parse_sprint_definition(
        "# Sprint 1\nTask;bad;alice;;Core,,Broken[],A[B][C];@ok, bad"
    )

    messages = [error.message for error in result.errors]
    assert len(messages) == 6
    assert result.rows[0].subscribers == ["ok"]
    assert result.rows[0].projects == [SprintProject("Core")]
    assert any("Owner" in message for message in messages)
    assert any("Subscriber" in message for message in messages)
    assert any("empty" in message for message in messages)


def test_mixed_tabs_and_four_space_hierarchy_uses_nearest_parent():
    result = parse_sprint_definition(
        "# Sprint Tree\nRoot;;@a\n\tChild A;;@a\n        Grandchild;;@a\n    Child B;;@a\nRoot 2;;@a\n    Child C;;@a"
    )

    assert result.is_valid
    assert [row.level for row in result.rows] == [0, 1, 2, 1, 0, 1]
    assert [row.parent_row_index for row in result.rows] == [None, 0, 1, 0, None, 4]


@pytest.mark.parametrize("indent", ["\t", "    "])
def test_tab_and_four_spaces_each_create_one_hierarchy_level(indent):
    result = parse_sprint_definition("# Sprint Tree\nRoot;;@a\n%sChild;;@a" % indent)

    assert result.is_valid
    assert result.rows[1].level == 1
    assert result.rows[1].parent_row_index == 0


@pytest.mark.parametrize(
    "body, expected_message",
    [
        ("Root;;@a\n  Odd;;@a", "multiple of four"),
        ("Root;;@a\n        Deep;;@a", "cannot jump"),
        ("    Orphan;;@a", "has no level-0 parent"),
    ],
)
def test_invalid_indentation_jump_and_missing_parent(body, expected_message):
    result = parse_sprint_definition("# Sprint Tree\n" + body)

    messages = [error.message for error in result.errors]
    assert any(expected_message in message for message in messages)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Sprint Ágil Nº 17", "sprint-agil-no-17"),
        ("  Déjà_vu / release  ", "deja-vu-release"),
        ("España -- Málaga", "espana-malaga"),
        ("***", ""),
    ],
)
def test_slug_normalization(value, expected):
    assert normalize_slug(value) == expected


def test_api_resolved_fields_are_mutable():
    row = parse_sprint_definition("# Sprint 1\nT9;;;;;").rows[0]

    row.existing_title = "Resolved title"
    row.final_owner_username = "resolved-owner"
    row.final_task_identifier = "T10"

    assert (
        row.existing_title,
        row.final_owner_username,
        row.final_task_identifier,
    ) == (
        "Resolved title",
        "resolved-owner",
        "T10",
    )


def _render_row(
    index,
    owner,
    identifier,
    title,
    estimate="2h",
    actual="1h",
    parent_row_index=None,
):
    return SprintTaskRow(
        row_index=index,
        line_number=index + 2,
        level=0 if parent_row_index is None else 1,
        parent_row_index=parent_row_index,
        title=title,
        task_identifier=None,
        estimation=estimate,
        estimation_hours=Decimal(estimate[:-1]),
        owner=None,
        priority=None,
        final_owner_username=owner,
        final_task_identifier=identifier,
        actual_time=actual,
    )


def test_render_groups_final_owners_in_first_appearance_order():
    definition = SprintDefinition(
        name="Sprint Secret",
        slug="sprint-secret",
        rows=[
            _render_row(0, "bob", "T2", "Second"),
            _render_row(1, "alice", "T1", "First"),
            _render_row(2, "bob", "T3", "Third"),
        ],
    )

    rendered = render_sprint_remarkup(definition)

    assert rendered.index("== @bob ==") < rendered.index("== @alice ==")
    assert rendered.index("T2") < rendered.index("T3") < rendered.index("@alice")
    assert "Sprint Secret" not in rendered
    assert rendered.count("<table>") == 2
    assert "<th>Título tarea</th><th>Código</th><th>Estimación</th>" in rendered
    assert "<th>Tiempo real</th><th>Observaciones</th>" in rendered


def test_render_groups_by_owner_only_and_does_not_add_subscriber_section():
    row = _render_row(0, "alice", "T1", "Task")
    row.subscribers = ["bob"]
    definition = SprintDefinition("Sprint 1", "sprint-1", [row])

    rendered = render_sprint_remarkup(definition)

    assert rendered.count("== @alice ==") == 1
    assert "== @bob ==" not in rendered


def test_render_includes_the_configured_personal_sprint_hashtag_in_owner_heading():
    definition = SprintDefinition(
        "Sprint 1", "sprint-1", [_render_row(0, "alice", "T1", "Task")]
    )

    rendered = render_sprint_remarkup(
        definition, owner_sprint_tags={"@alice": "Sprint Gestión Alice"}
    )

    assert "== @alice : #sprint_gestión_alice ==" in rendered


def test_render_escapes_all_dynamic_html_and_uses_resolved_title():
    row = _render_row(0, "a&b", "T1<script>", "new <title>")
    row.existing_title = "resolved <title>"
    definition = SprintDefinition("Sprint 1", "sprint-1", [row])

    rendered = render_sprint_remarkup(definition)

    assert "@a&amp;b" in rendered
    assert "T1&lt;script&gt;" in rendered
    assert "resolved &lt;title&gt;" in rendered
    assert "<script>" not in rendered


def test_render_uses_document_hierarchy_within_each_owner_section():
    definition = SprintDefinition(
        name="Sprint Tree",
        slug="sprint-tree",
        rows=[
            _render_row(0, "alice", "T100", "Root"),
            _render_row(1, "alice", "T101", "Child", parent_row_index=0),
            _render_row(2, "alice", "T102", "Grandchild", parent_row_index=1),
            _render_row(3, "bob", "T103", "Other owner", parent_row_index=0),
            _render_row(4, "bob", "T104", "Bob child", parent_row_index=3),
            _render_row(5, "alice", "T105", "Alice return", parent_row_index=3),
        ],
    )

    rendered = render_sprint_remarkup(definition)

    assert "<td>Root</td><td>T100</td>" in rendered
    assert "<td>⭢ Child</td><td>T101</td>" in rendered
    assert "<td>⭢⭢ Grandchild</td><td>T102</td>" in rendered
    assert "<td>(Hija de T100) Other owner</td><td>T103</td>" in rendered
    assert "<td>⭢ Bob child</td><td>T104</td>" in rendered
    assert "<td>(Hija de T103) Alice return</td><td>T105</td>" in rendered

    alice_section = rendered[rendered.index("== @alice ==") : rendered.index("== @bob ==")]
    assert alice_section.index("T100") < alice_section.index("T101")
    assert alice_section.index("T101") < alice_section.index("T102")
    assert alice_section.index("T102") < alice_section.index("T105")


def test_render_empty_definition_is_empty():
    assert render_sprint_remarkup(SprintDefinition("Sprint 1", "sprint-1")) == ""


def test_preview_renderer_uses_todo_for_new_external_parent():
    parent = _render_row(0, "alice", None, "Parent")
    child = _render_row(1, "bob", None, "Child", parent_row_index=0)
    rendered = render_sprint_remarkup(SprintDefinition("Sprint", "sprint", [parent, child]), preview=True)
    assert "<td>TODO</td>" in rendered
    assert "(Hija de TODO) Child" in rendered
