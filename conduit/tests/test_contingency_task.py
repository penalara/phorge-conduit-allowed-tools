import unittest
from unittest.mock import Mock

from conduit.tools.contingency_task_tools import (
    register_contingency_task_tools,
)


def page(data):
    return {"data": data, "cursor": {"after": None}}


def named(phid, name, **fields):
    return {"phid": phid, "fields": {"name": name, **fields}}


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


class ContingencyTaskTest(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        mcp = FakeMCP()
        register_contingency_task_tools(mcp, lambda: self.client)
        self.create = mcp.tools["pha_task_create_contingency"]
        self.client.user.search.return_value = page(
            [{"phid": "PHID-USER-ana", "fields": {"username": "ana"}}]
        )
        self.client.project.search_projects.return_value = page(
            [named("PHID-PROJ-sprint-ana", "Sprint Ana")]
        )
        self.client.project.search_columns.return_value = page(
            [
                named("PHID-PCOL-backlog", "Sprint Backlog"),
                named("PHID-PCOL-hidden", "Sprint Backlog", isHidden=True),
                named("PHID-PCOL-progress", "En curso"),
            ]
        )
        self.client.maniphest.search_tasks.return_value = page(
            [
                {
                    "id": 12,
                    "phid": "PHID-TASK-parent",
                    "fields": {
                        "name": "Contingencias sprint",
                        "status": {"name": "Open"},
                    },
                }
            ]
        )
        self.client.maniphest.edit_task.return_value = {
            "object": {"id": 13, "phid": "PHID-TASK-child"}
        }

    def test_creates_child_in_the_requested_owners_sprint_workboard(self):
        result = self.create(
            "Resolver incidencia de acceso", "ana", "Sprint Ana"
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["task"]["id"], 13)
        self.assertEqual(result["owner"]["username"], "ana")
        self.assertEqual(result["parent"]["task"], "T12")
        self.assertEqual(result["destination"]["column"], "En curso")
        self.client.user.search.assert_called_once()
        self.client.maniphest.search_tasks.assert_called_once()
        self.assertEqual(
            self.client.maniphest.search_tasks.call_args.kwargs["constraints"],
            {
                "projects": ["PHID-PROJ-sprint-ana"],
                "columnPHIDs": ["PHID-PCOL-backlog"],
            },
        )
        transactions = self.client.maniphest.edit_task.call_args.kwargs[
            "transactions"
        ]
        self.assertEqual(
            transactions,
            [
                {"type": "title", "value": "Resolver incidencia de acceso"},
                {"type": "owner", "value": "PHID-USER-ana"},
                {"type": "priority", "value": "normal"},
                {"type": "custom.penalara:tasktype", "value": "3"},
                {"type": "projects.add", "value": ["PHID-PROJ-sprint-ana"]},
                {"type": "parent", "value": "PHID-TASK-parent"},
                {"type": "column", "value": ["PHID-PCOL-progress"]},
            ],
        )

    def test_missing_backlog_column_aborts_before_a_write(self):
        self.client.project.search_columns.return_value = page(
            [named("PHID-PCOL-progress", "En curso")]
        )

        result = self.create("Resolver incidencia", "ana", "Sprint Ana")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "BACKLOG_COLUMN_NOT_FOUND")
        self.assertEqual(result["mutationsAttempted"], 0)
        self.client.maniphest.search_tasks.assert_not_called()
        self.client.maniphest.edit_task.assert_not_called()

    def test_missing_contingency_parent_aborts_before_a_write(self):
        self.client.maniphest.search_tasks.return_value = page(
            [
                {
                    "id": 12,
                    "phid": "PHID-TASK-other",
                    "fields": {
                        "name": "Tarea normal",
                        "status": {"name": "Open"},
                    },
                }
            ]
        )

        result = self.create("Resolver incidencia", "ana", "Sprint Ana")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "CONTINGENCY_PARENT_NOT_FOUND")
        self.assertEqual(result["mutationsAttempted"], 0)
        self.client.maniphest.edit_task.assert_not_called()

    def test_multiple_contingency_parents_abort_before_a_write(self):
        self.client.maniphest.search_tasks.return_value = page(
            [
                {
                    "id": 12,
                    "phid": "PHID-TASK-one",
                    "fields": {
                        "name": "Contingencias de acceso",
                        "status": {"name": "Open"},
                    },
                },
                {
                    "id": 14,
                    "phid": "PHID-TASK-two",
                    "fields": {
                        "name": "CONTINGENCIAS de red",
                        "status": {"name": "Open"},
                    },
                },
            ]
        )

        result = self.create("Resolver incidencia", "ana", "Sprint Ana")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "AMBIGUOUS_CONTINGENCY_PARENT")
        self.assertEqual(result["mutationsAttempted"], 0)
        self.client.maniphest.edit_task.assert_not_called()

    def test_missing_owner_aborts_before_a_write(self):
        self.client.user.search.return_value = page([])

        result = self.create("Resolver incidencia", "missing", "Sprint Ana")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "OWNER_NOT_FOUND")
        self.assertEqual(result["mutationsAttempted"], 0)
        self.client.project.search_projects.assert_not_called()
        self.client.maniphest.edit_task.assert_not_called()

    def test_closed_contingency_parent_is_ignored(self):
        self.client.maniphest.search_tasks.return_value = page(
            [
                {
                    "id": 12,
                    "phid": "PHID-TASK-closed",
                    "fields": {
                        "name": "Contingencias resueltas",
                        "status": {"name": "Resolved"},
                    },
                }
            ]
        )

        result = self.create("Resolver incidencia", "ana", "Sprint Ana")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "CONTINGENCY_PARENT_NOT_FOUND")
        self.assertEqual(result["mutationsAttempted"], 0)
        self.client.maniphest.edit_task.assert_not_called()

    def test_ambiguous_owner_aborts_before_a_write(self):
        self.client.user.search.return_value = page(
            [
                {"phid": "PHID-USER-one", "fields": {"username": "ana"}},
                {"phid": "PHID-USER-two", "fields": {"username": "ana"}},
            ]
        )

        result = self.create("Resolver incidencia", "ana", "Sprint Ana")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "AMBIGUOUS_OWNER")
        self.assertEqual(result["mutationsAttempted"], 0)
        self.client.project.search_projects.assert_not_called()
        self.client.maniphest.edit_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
