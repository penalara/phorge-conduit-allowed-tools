import unittest
from unittest.mock import Mock

from conduit.client import PhabricatorAPIError
from conduit.client.maniphest import ManiphestClient
from conduit.tools.start_task_tools import register_start_task_tools


def _page(data):
    return {"data": data, "cursor": {"after": None}}


def _task(project_phids):
    return _page(
        [
            {
                "id": 123,
                "phid": "PHID-TASK-example",
                "fields": {},
                "attachments": {"projects": {"projectPHIDs": project_phids}},
            }
        ]
    )


def _project(phid, name):
    return {"id": "1", "phid": phid, "fields": {"name": name}}


def _column(phid, name):
    return {"id": "1", "phid": phid, "fields": {"name": name}}


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


class TestTaskStartTool(unittest.TestCase):
    def setUp(self):
        self.mcp = FakeMCP()
        self.client = Mock()
        register_start_task_tools(self.mcp, lambda: self.client)
        self.start_task = self.mcp.tools["pha_task_start"]

    def _configure_task(self, project_phids):
        self.client.maniphest.search_tasks.return_value = _task(project_phids)
        self.client.project.search_projects.return_value = _page(
            [
                _project(phid, "Project {}".format(index))
                for index, phid in enumerate(project_phids, 1)
            ]
        )

    def test_invalid_task_formats(self):
        for task in ("22", "t22", "T0", "TT22", "T22abc"):
            with self.subTest(task=task):
                result = self.start_task(task)
                self.assertFalse(result["success"])
                self.assertEqual(result["error_code"], "VALIDATION_ERROR")
        self.client.maniphest.search_tasks.assert_not_called()

    def test_task_not_found(self):
        self.client.maniphest.search_tasks.return_value = _page([])

        result = self.start_task("T123")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "NOT_FOUND")
        self.client.maniphest.edit_task.assert_not_called()

    def test_task_without_tags_returns_warning(self):
        self._configure_task([])

        result = self.start_task("T123")

        self.assertTrue(result["success"])
        self.assertFalse(result["ok"])
        self.assertFalse(result["moved"])
        self.assertIn("no tiene proyectos", result["warning"])
        self.client.project.search_projects.assert_not_called()
        self.client.maniphest.edit_task.assert_not_called()

    def test_single_project_with_target_column(self):
        project_phid = "PHID-PROJ-backend"
        self._configure_task([project_phid])
        self.client.project.search_columns.return_value = _page(
            [_column("PHID-PCOL-backend", " En CURSO ")]
        )
        self.client.maniphest.create_column_transaction.return_value = {
            "type": "column",
            "value": ["PHID-PCOL-backend"],
        }

        result = self.start_task("T123")

        self.assertTrue(result["success"])
        self.assertTrue(result["moved"])
        self.assertEqual(
            result["movedTo"][0]["columnPHID"], "PHID-PCOL-backend"
        )
        transaction_factory = self.client.maniphest.create_column_transaction
        transaction_factory.assert_called_once_with(["PHID-PCOL-backend"])
        self.client.maniphest.edit_task.assert_called_once()

    def test_multiple_projects_are_moved_in_one_transaction(self):
        projects = ["PHID-PROJ-backend", "PHID-PROJ-frontend"]
        self._configure_task(projects)
        self.client.project.search_columns.side_effect = [
            _page([_column("PHID-PCOL-backend", "En curso")]),
            _page([_column("PHID-PCOL-frontend", "En curso")]),
        ]
        self.client.maniphest.create_column_transaction.return_value = {
            "type": "column",
            "value": ["PHID-PCOL-backend", "PHID-PCOL-frontend"],
        }

        result = self.start_task("T123")

        self.assertEqual(len(result["movedTo"]), 2)
        transaction_factory = self.client.maniphest.create_column_transaction
        transaction_factory.assert_called_once_with(
            ["PHID-PCOL-backend", "PHID-PCOL-frontend"]
        )
        self.client.maniphest.edit_task.assert_called_once()

    def test_projects_without_target_column_are_skipped(self):
        projects = ["PHID-PROJ-backend", "PHID-PROJ-product"]
        self._configure_task(projects)
        self.client.project.search_columns.side_effect = [
            _page([_column("PHID-PCOL-backend", "En curso")]),
            _page([_column("PHID-PCOL-product", "Pendiente")]),
        ]
        self.client.maniphest.create_column_transaction.return_value = {
            "type": "column",
            "value": ["PHID-PCOL-backend"],
        }

        result = self.start_task("T123")

        self.assertTrue(result["moved"])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(
            result["skipped"][0]["projectPHID"], "PHID-PROJ-product"
        )
        self.assertEqual(
            result["skipped"][0]["reason"], 'No existe la columna "En curso"'
        )

    def test_no_project_with_target_column_returns_warning(self):
        projects = ["PHID-PROJ-backend", "PHID-PROJ-product"]
        self._configure_task(projects)
        self.client.project.search_columns.side_effect = [
            _page([]),
            _page([_column("PHID-PCOL-product", "Pendiente")]),
        ]

        result = self.start_task("T123")

        self.assertTrue(result["success"])
        self.assertFalse(result["ok"])
        self.assertFalse(result["moved"])
        self.assertEqual(len(result["skipped"]), 2)
        self.client.maniphest.edit_task.assert_not_called()

    def test_ambiguous_columns_abort_before_write(self):
        project_phid = "PHID-PROJ-backend"
        self._configure_task([project_phid])
        self.client.project.search_columns.return_value = _page(
            [
                _column("PHID-PCOL-one", "En curso"),
                _column("PHID-PCOL-two", " en CURSO "),
            ]
        )

        result = self.start_task("T123")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "AMBIGUOUS_COLUMN")
        self.assertEqual(
            {column["phid"] for column in result["ambiguities"][0]["columns"]},
            {"PHID-PCOL-one", "PHID-PCOL-two"},
        )
        self.client.maniphest.edit_task.assert_not_called()

    def test_conduit_error_during_precheck_prevents_write(self):
        self.client.maniphest.search_tasks.side_effect = PhabricatorAPIError(
            "search failed", error_code="ERR-CONDUIT"
        )

        result = self.start_task("T123")

        self.assertFalse(result["success"])
        self.client.maniphest.edit_task.assert_not_called()

    def test_conduit_error_during_edit_is_reported(self):
        project_phid = "PHID-PROJ-backend"
        self._configure_task([project_phid])
        self.client.project.search_columns.return_value = _page(
            [_column("PHID-PCOL-backend", "En curso")]
        )
        self.client.maniphest.create_column_transaction.return_value = {
            "type": "column",
            "value": ["PHID-PCOL-backend"],
        }
        self.client.maniphest.edit_task.side_effect = PhabricatorAPIError(
            "edit failed", error_code="ERR-CONDUIT"
        )

        result = self.start_task("T123")

        self.assertFalse(result["success"])
        self.client.maniphest.edit_task.assert_called_once()

    def test_repeated_execution_uses_the_same_idempotent_transaction(self):
        project_phid = "PHID-PROJ-backend"
        self._configure_task([project_phid])
        self.client.project.search_columns.return_value = _page(
            [_column("PHID-PCOL-backend", "En curso")]
        )
        self.client.maniphest.create_column_transaction.return_value = {
            "type": "column",
            "value": ["PHID-PCOL-backend"],
        }

        first = self.start_task("T123")
        second = self.start_task("T123")

        self.assertTrue(first["moved"])
        self.assertTrue(second["moved"])
        self.assertEqual(self.client.maniphest.edit_task.call_count, 2)


class TestAllowedMethodEnforcement(unittest.TestCase):
    def test_typed_client_rejects_unallowed_conduit_method_before_http_request(
        self,
    ):
        http_client = Mock()
        client = ManiphestClient(
            "https://phorge.example/api/", "x" * 32, http_client
        )
        client.set_allowed_methods(["project.search"])

        with self.assertRaisesRegex(
            PhabricatorAPIError, "not in the allowed methods"
        ):
            client.search_tasks(constraints={"ids": [123]})

        http_client.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
