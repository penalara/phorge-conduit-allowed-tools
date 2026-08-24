import unittest
from unittest.mock import Mock

from conduit.client import PhabricatorAPIError
from conduit.tools.sprint_tools import register_sprint_tools


def page(data):
    return {"data": data, "cursor": {"after": None}}


def named(phid, name):
    return {"phid": phid, "fields": {"name": name}}


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


class SprintWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        mcp = FakeMCP()
        register_sprint_tools(mcp, lambda: self.client)
        self.preview = mcp.tools["phorge_preview_sprint"]
        self.apply_preview = mcp.tools["phorge_create_sprint"]
        self.create = self._create
        self.config = {
            "name": "Demo",
            "wikiBasePath": "teams/demo",
            "defaultTag": "Demo",
        }
        self.tags = {"@ana": "Sprint Ana", "@bob": "Sprint Bob"}
        self.client.maniphest.get_priority_info.return_value = {
            "data": [
                {"fields": {"name": "Normal"}, "keywords": ["normal"]},
                {"fields": {"name": "High"}, "keywords": ["high"]},
            ]
        }
        self.client.user.search.side_effect = self._users
        self.client.project.search_projects.side_effect = self._projects
        self.client.project.search_columns.return_value = page([])
        self.client.maniphest.search_tasks.return_value = page([])
        self.client.phriction.get_document_info.return_value = {}
        self.client.phriction.create_document.return_value = {
            "slug": "teams/demo/sprint-1/"
        }
        self.next_task = 100

        def edit(**kwargs):
            self.next_task += 1
            return {
                "object": {"id": self.next_task, "phid": f"PHID-TASK-{self.next_task}"}
            }

        self.client.maniphest.edit_task.side_effect = edit

    def _create(self, source_path, source_text, config, tags):
        preview = self.preview(source_path, source_text, config, tags)
        if not preview.get("success"):
            return preview
        return self.apply_preview(preview["previewId"])

    def _users(self, constraints=None, **kwargs):
        constraints = constraints or {}
        if "usernames" in constraints:
            username = constraints["usernames"][0]
            return page(
                [{"phid": "PHID-USER-" + username, "fields": {"username": username}}]
            )
        phid = constraints["phids"][0]
        return page([{"phid": phid, "fields": {"username": phid.rsplit("-", 1)[-1]}}])

    def _projects(self, constraints=None, **kwargs):
        constraints = constraints or {}
        name = constraints["query"]
        return page([named("PHID-PROJ-" + name.replace(" ", "-"), name)])

    def _existing_task(
        self, task_id=7, owner="ana", projects=None, subscribers=None, columns=None
    ):
        return {
            "id": task_id,
            "phid": f"PHID-TASK-{task_id}",
            "fields": {
                "name": f"Existing {task_id}",
                "ownerPHID": f"PHID-USER-{owner}" if owner else None,
                "priority": {"name": "Normal"},
            },
            "attachments": {
                "projects": {"projectPHIDs": projects or []},
                "subscribers": {"subscriberPHIDs": subscribers or []},
                "columns": {"columnPHIDs": columns or []},
            },
        }

    def _assert_zero_writes(self, result):
        self.assertFalse(result["success"])
        self.assertEqual(result["phase"], "precheck")
        self.assertEqual(result["mutationsAttempted"], 0)
        self.client.maniphest.edit_task.assert_not_called()
        self.client.phriction.create_document.assert_not_called()

    def test_minimal_new_task_and_wiki(self):
        result = self.create(
            "sprints/one.txt", "# Sprint 1\nBuild it;;@ana", self.config, self.tags
        )
        self.assertTrue(result["success"])
        tx = self.client.maniphest.edit_task.call_args.kwargs["transactions"]
        self.assertEqual(
            [item["type"] for item in tx[:5]],
            ["title", "description", "owner", "priority", "projects.add"],
        )
        self.assertEqual(tx[2]["value"], "PHID-USER-ana")
        self.assertEqual(tx[3]["value"], "normal")
        self.assertEqual(
            tx[4]["value"],
            ["PHID-PROJ-Demo", "PHID-PROJ-Sprint-Ana"],
        )
        self.assertEqual(len(result["created"]), 1)
        content = self.client.phriction.create_document.call_args.kwargs["content"]
        self.assertIn("== @ana : #sprint_ana ==", content)

    def test_new_task_estimate_is_rendered_in_phriction(self):
        result = self.create(
            "one.txt", "# Sprint 1\nBuild;1.5h;@ana", self.config, self.tags
        )

        self.assertTrue(result["success"])
        content = self.client.phriction.create_document.call_args.kwargs["content"]
        self.assertIn("<td>1.5h</td>", content)

    def test_new_task_uses_the_optional_description(self):
        result = self.create(
            "one.txt", "# Sprint 1\nBuild;;@ana;;;;Detailed work", self.config, self.tags
        )

        self.assertTrue(result["success"])
        transactions = self.client.maniphest.edit_task.call_args.kwargs["transactions"]
        self.assertEqual(transactions[1], {"type": "description", "value": "Detailed work"})

    def test_new_task_without_explicit_tags_uses_default_and_owner_sprint_tag(self):
        result = self.create(
            "one.txt", "# Sprint 1\nBuild;;@ana", self.config, self.tags
        )

        self.assertTrue(result["success"])
        transactions = self.client.maniphest.edit_task.call_args.kwargs["transactions"]
        projects = next(
            item["value"] for item in transactions if item["type"] == "projects.add"
        )
        self.assertEqual(projects, ["PHID-PROJ-Demo", "PHID-PROJ-Sprint-Ana"])

    def test_new_task_explicit_tags_replace_default_but_keep_owner_sprint_tag(self):
        result = self.create(
            "one.txt", "# Sprint 1\nBuild;;@ana;;Core,Ops", self.config, self.tags
        )

        self.assertTrue(result["success"])
        transactions = self.client.maniphest.edit_task.call_args.kwargs["transactions"]
        projects = next(
            item["value"] for item in transactions if item["type"] == "projects.add"
        )
        self.assertEqual(
            projects,
            ["PHID-PROJ-Core", "PHID-PROJ-Ops", "PHID-PROJ-Sprint-Ana"],
        )
        searched = [
            call.kwargs["constraints"]["query"]
            for call in self.client.project.search_projects.call_args_list
        ]
        self.assertNotIn("Demo", searched)

    def test_project_column_resolves_on_its_existing_workboard(self):
        self.client.project.search_columns.return_value = page(
            [named("PHID-PCOL-doing", "Doing")]
        )

        result = self.create(
            "one.txt", "# Sprint 1\nBuild;;@ana;;Core[Doing]", self.config, self.tags
        )

        self.assertTrue(result["success"])
        self.client.project.search_columns.assert_called_once()
        self.assertEqual(
            self.client.project.search_columns.call_args.kwargs["constraints"],
            {"projects": ["PHID-PROJ-Core"]},
        )
        transactions = self.client.maniphest.edit_task.call_args.kwargs["transactions"]
        self.assertIn({"type": "column", "value": ["PHID-PCOL-doing"]}, transactions)

    def test_multiple_tags_and_columns_are_applied_in_source_order(self):
        self.client.project.search_columns.side_effect = [
            page([named("PHID-PCOL-doing", "Doing")]),
            page([named("PHID-PCOL-ready", "Ready")]),
        ]

        result = self.create(
            "one.txt",
            "# Sprint 1\nBuild;;@ana;;Core[Doing],Ops[Ready]",
            self.config,
            self.tags,
        )

        self.assertTrue(result["success"])
        transactions = self.client.maniphest.edit_task.call_args.kwargs["transactions"]
        self.assertIn(
            {"type": "column", "value": ["PHID-PCOL-doing", "PHID-PCOL-ready"]},
            transactions,
        )

    def test_existing_unchanged_is_not_edited(self):
        self.client.maniphest.search_tasks.return_value = page(
            [
                {
                    "id": 7,
                    "phid": "PHID-TASK-seven",
                    "fields": {
                        "name": "Old",
                        "ownerPHID": "PHID-USER-ana",
                        "priority": {"name": "Normal"},
                    },
                    "attachments": {
                        "projects": {"projectPHIDs": []},
                        "subscribers": {"subscriberPHIDs": []},
                        "columns": {"columnPHIDs": []},
                    },
                }
            ]
        )
        result = self.create("one.txt", "# Sprint 1\nT7", self.config, self.tags)
        self.assertTrue(result["success"])
        self.client.maniphest.edit_task.assert_not_called()
        self.assertEqual(result["unchanged"][0]["task"], "T7")

    def test_existing_explicit_patch_replaces_tags_and_sets_subscribers(self):
        self.client.maniphest.search_tasks.return_value = page(
            [
                {
                    "id": 7,
                    "phid": "PHID-TASK-seven",
                    "fields": {"name": "Old", "ownerPHID": None},
                    "attachments": {},
                }
            ]
        )
        result = self.create(
            "one.txt", "# Sprint 1\nT7;;@ana;Normal;Demo;@bob;Ignored", self.config, self.tags
        )
        self.assertTrue(result["success"])
        types = [
            item["type"]
            for item in self.client.maniphest.edit_task.call_args.kwargs["transactions"]
        ]
        self.assertEqual(
            types, ["owner", "priority", "projects.set", "subscribers.set"]
        )
        values = [
            item["value"]
            for item in self.client.maniphest.edit_task.call_args.kwargs["transactions"]
        ]
        self.assertEqual(
            values,
            [
                "PHID-USER-ana",
                "normal",
                ["PHID-PROJ-Demo", "PHID-PROJ-Sprint-Ana"],
                ["PHID-USER-bob"],
            ],
        )

    def test_new_parent_and_new_child_relationship_is_applied(self):
        self.client.project.search_columns.return_value = page(
            [named("PHID-PCOL-doing", "Doing")]
        )
        text = "# Sprint 1\nParent;;@ana;;;\n    Child;;@bob;;Demo[Doing]"
        result = self.create("one.txt", text, self.config, self.tags)
        self.assertTrue(result["success"])
        child = self.client.maniphest.edit_task.call_args_list[1].kwargs["transactions"]
        self.assertIn({"type": "parent", "value": "PHID-TASK-101"}, child)
        self.assertIn({"type": "column", "value": ["PHID-PCOL-doing"]}, child)
        self.assertEqual(result["tasks"][1]["columns"][0]["name"], "Doing")

    def test_existing_parent_is_used_for_new_child(self):
        self.client.maniphest.search_tasks.return_value = page([self._existing_task(7)])

        result = self.create(
            "one.txt", "# Sprint 1\nT7\n    Child;;@bob", self.config, self.tags
        )

        self.assertTrue(result["success"])
        child = self.client.maniphest.edit_task.call_args.kwargs["transactions"]
        self.assertIn({"type": "parent", "value": "PHID-TASK-7"}, child)

    def test_existing_child_is_added_below_new_parent(self):
        self.client.maniphest.search_tasks.return_value = page([self._existing_task(7)])

        result = self.create(
            "one.txt", "# Sprint 1\nParent;;@ana\n    T7", self.config, self.tags
        )

        self.assertTrue(result["success"])
        child = self.client.maniphest.edit_task.call_args_list[1].kwargs["transactions"]
        self.assertEqual(child, [{"type": "parents.add", "value": ["PHID-TASK-101"]}])

    def test_existing_task_can_be_patched_into_workboard_column(self):
        self.client.maniphest.search_tasks.return_value = page([self._existing_task(7)])
        self.client.project.search_columns.return_value = page(
            [named("PHID-PCOL-doing", "Doing")]
        )

        result = self.create(
            "one.txt", "# Sprint 1\nT7;;;;Core[Doing]", self.config, self.tags
        )

        self.assertTrue(result["success"])
        transactions = self.client.maniphest.edit_task.call_args.kwargs["transactions"]
        self.assertEqual(
            transactions,
            [
                {"type": "projects.set", "value": ["PHID-PROJ-Core"]},
                {"type": "column", "value": ["PHID-PCOL-doing"]},
            ],
        )

    def test_validation_errors_make_no_writes(self):
        cases = [
            ("../one.txt", "# Sprint 1\nBuild;;@ana", self.config, self.tags),
            ("one.txt", "# Sprint 1", self.config, self.tags),
            ("one.txt", "# !!!\nBuild;;@ana", self.config, self.tags),
            (
                "one.txt",
                "# Sprint 1\nBuild;;@ana",
                {"name": "<CONFIGURAR>", "wikiBasePath": "x", "defaultTag": "Demo"},
                self.tags,
            ),
            (
                "one.txt",
                "# Sprint 1\nBuild;;@ana",
                {
                    "name": "Demo",
                    "wikiBasePath": "/teams/demo/",
                    "defaultTag": "Demo",
                },
                self.tags,
            ),
            ("one.txt", "# Sprint 1\nBuild;;@ana", self.config, {"ana": "Sprint Ana"}),
        ]
        for args in cases:
            with self.subTest(args=args):
                self.client.reset_mock()
                result = self.create(*args)
                self._assert_zero_writes(result)

    def test_resolution_errors_abort_before_writes_and_existing_wiki_is_updated(self):
        self.client.user.search.side_effect = lambda **kwargs: page([])
        result = self.create(
            "one.txt", "# Sprint 1\nBuild;;@ana", self.config, self.tags
        )
        self._assert_zero_writes(result)
        self.client.reset_mock()
        self.client.user.search.side_effect = self._users
        self.client.phriction.get_document_info.return_value = {"content": "existing"}
        result = self.create(
            "one.txt", "# Sprint 1\nBuild;;@ana", self.config, self.tags
        )
        self.assertTrue(result["success"])
        self.client.phriction.edit_document.assert_called_once()

    def test_missing_owner_sprint_tag_uses_default_tag_and_warns(self):
        result = self.create(
            "one.txt", "# Sprint 1\nBuild;;@ana;;Demo", self.config, {"@bob": "Sprint Bob"}
        )

        self.assertTrue(result["success"])
        transactions = self.client.maniphest.edit_task.call_args.kwargs["transactions"]
        projects = next(
            item["value"] for item in transactions if item["type"] == "projects.add"
        )
        self.assertEqual(projects, ["PHID-PROJ-Demo"])
        self.assertEqual(
            result["warnings"],
            ['No hay un proyecto personal de sprint configurado para @ana. Se ha utilizado el tag por defecto "Demo".'],
        )

    def test_missing_referenced_task_is_a_zero_write_precheck(self):
        result = self.create("one.txt", "# Sprint 1\nT404", self.config, self.tags)

        self._assert_zero_writes(result)
        self.assertIn("TASK_NOT_FOUND", [error["code"] for error in result["errors"]])

    def test_duplicate_existing_task_is_a_zero_write_precheck(self):
        result = self.create(
            "one.txt", "# Sprint 1\nT7\n    T7", self.config, self.tags
        )

        self._assert_zero_writes(result)
        self.assertEqual(
            [error["code"] for error in result["errors"]].count("DUPLICATE_TASK"),
            2,
        )

    def test_configuration_rejects_phids_before_reads_or_writes(self):
        config = dict(self.config, defaultTag="PHID-PROJ-not-configuration")

        result = self.create("one.txt", "# Sprint 1\nBuild;;@ana", config, self.tags)

        self._assert_zero_writes(result)
        self.client.maniphest.search_tasks.assert_not_called()

    def test_missing_owner_and_subscriber_are_zero_write_prechecks(self):
        for source, missing_name in [
            ("# Sprint 1\nBuild;;@missing", "missing"),
            ("# Sprint 1\nBuild;;@ana;;;@missing", "missing"),
        ]:
            with self.subTest(missing=missing_name, source=source):
                self.client.reset_mock()
                self.client.user.search.side_effect = (
                    lambda constraints=None, **kwargs: (
                        page([])
                        if (constraints or {})["usernames"][0] == "missing"
                        else self._users(constraints)
                    )
                )
                result = self.create("one.txt", source, self.config, self.tags)
                self._assert_zero_writes(result)
                self.assertIn(
                    "USER_NOT_FOUND", [error["code"] for error in result["errors"]]
                )

    def test_missing_priority_tag_and_column_are_zero_write_prechecks(self):
        cases = [
            ("# Sprint 1\nBuild;;@ana;Urgent", "PRIORITY_NOT_FOUND", None),
            ("# Sprint 1\nBuild;;@ana;;Missing", "PROJECT_NOT_FOUND", None),
            ("# Sprint 1\nBuild;;@ana;;Core[Missing]", "COLUMN_NOT_FOUND", page([])),
        ]
        for source, code, columns in cases:
            with self.subTest(code=code):
                self.client.reset_mock()
                self.client.user.search.side_effect = self._users
                self.client.project.search_projects.side_effect = self._projects
                if code == "PROJECT_NOT_FOUND":
                    self.client.project.search_projects.side_effect = (
                        lambda constraints=None, **kwargs: (
                            page([])
                            if (constraints or {})["query"] == "Missing"
                            else self._projects(constraints)
                        )
                    )
                self.client.project.search_columns.return_value = columns or page([])
                result = self.create("one.txt", source, self.config, self.tags)
                self._assert_zero_writes(result)
                self.assertIn(code, [error["code"] for error in result["errors"]])

    def test_priority_requires_exact_english_visible_name(self):
        for priority in ["Alta", "high"]:
            with self.subTest(priority=priority):
                self.client.reset_mock()
                self.client.user.search.side_effect = self._users
                self.client.project.search_projects.side_effect = (
                    self._projects
                )
                result = self.create(
                    "one.txt",
                    "# Sprint 1\nBuild;;@ana;%s" % priority,
                    self.config,
                    self.tags,
                )
                self._assert_zero_writes(result)
                self.assertIn(
                    "PRIORITY_NOT_FOUND",
                    [error["code"] for error in result["errors"]],
                )

    def test_ambiguous_project_and_column_are_zero_write_prechecks(self):
        cases = [
            ("# Sprint 1\nBuild;;@ana;;Core", "AMBIGUOUS_PROJECT"),
            ("# Sprint 1\nBuild;;@ana;;Core[Doing]", "AMBIGUOUS_COLUMN"),
        ]
        for source, code in cases:
            with self.subTest(code=code):
                self.client.reset_mock()
                self.client.user.search.side_effect = self._users
                self.client.project.search_projects.side_effect = self._projects
                if code == "AMBIGUOUS_PROJECT":
                    self.client.project.search_projects.side_effect = (
                        lambda constraints=None, **kwargs: (
                            page(
                                [
                                    named("PHID-PROJ-a", "Core"),
                                    named("PHID-PROJ-b", "Core"),
                                ]
                            )
                            if (constraints or {})["query"] == "Core"
                            else self._projects(constraints)
                        )
                    )
                else:
                    self.client.project.search_columns.return_value = page(
                        [
                            named("PHID-PCOL-a", "Doing"),
                            named("PHID-PCOL-b", "Doing"),
                        ]
                    )
                result = self.create("one.txt", source, self.config, self.tags)
                self._assert_zero_writes(result)
                self.assertIn(code, [error["code"] for error in result["errors"]])

    def test_partial_task_write_stops_and_does_not_create_wiki(self):
        self.client.maniphest.edit_task.side_effect = [
            {"object": {"id": 1, "phid": "PHID-TASK-one"}},
            PhabricatorAPIError("boom", error_code="ERR-X", error_info="detail"),
        ]
        text = "# Sprint 1\nOne;;@ana\nTwo;;@bob\nThree;;@ana"
        result = self.create("one.txt", text, self.config, self.tags)
        self.assertFalse(result["success"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["error"]["error_code"], "ERR-X")
        self.assertEqual(
            result["lastSuccessfulOperation"],
            {"line": 2, "task": "One", "operation": "create"},
        )
        self.assertEqual(result["failedOperation"]["line"], 3)
        self.assertEqual(result["failedOperation"]["task"], "Two")
        self.assertEqual(result["pendingTasks"], [4])
        self.assertEqual([task["task"] for task in result["tasks"]], ["T1"])
        self.assertEqual(self.client.maniphest.edit_task.call_count, 2)
        self.client.phriction.create_document.assert_not_called()

    def test_first_write_failure_is_not_reported_as_partial(self):
        self.client.maniphest.edit_task.side_effect = PhabricatorAPIError(
            "boom", error_code="ERR-X"
        )

        result = self.create(
            "one.txt", "# Sprint 1\nOne;;@ana\nTwo;;@bob", self.config, self.tags
        )

        self.assertFalse(result["success"])
        self.assertFalse(result["partial"])
        self.assertIsNone(result["lastSuccessfulOperation"])
        self.assertEqual(result["pendingTasks"], [3])
        self.client.phriction.create_document.assert_not_called()

    def test_wiki_failure_is_partial_after_all_tasks(self):
        self.client.phriction.create_document.side_effect = PhabricatorAPIError(
            "wiki failed", error_code="ERR-WIKI"
        )
        result = self.create(
            "one.txt", "# Sprint 1\nBuild;;@ana", self.config, self.tags
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["failedOperation"]["operation"], "createWiki")
        self.assertEqual(
            result["lastSuccessfulOperation"],
            {"line": 2, "task": "Build", "operation": "create"},
        )
        self.assertEqual(result["pendingTasks"], [])
        self.assertEqual(len(result["tasks"]), 1)
        self.client.phriction.create_document.assert_called_once()


if __name__ == "__main__":
    unittest.main()
