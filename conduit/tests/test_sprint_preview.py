import unittest
from unittest.mock import Mock

from conduit.tools.sprint_tools import register_sprint_tools


def page(data):
    return {"data": data, "cursor": {"after": None}}


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


class SprintPreviewTest(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        mcp = FakeMCP()
        register_sprint_tools(mcp, lambda: self.client)
        self.preview = mcp.tools["phorge_preview_sprint"]
        self.create = mcp.tools["phorge_create_sprint"]
        self.config = {"name": "Demo", "wikiBasePath": "teams/demo"}
        self.tags = {"@ana": "Sprint Ana"}
        self.client.maniphest.get_priority_info.return_value = {
            "data": [{"fields": {"name": "Normal"}, "keywords": ["normal"]}]
        }
        self.client.user.search.return_value = page(
            [{"phid": "PHID-USER-ana", "fields": {"username": "ana"}}]
        )
        self.client.project.search_projects.side_effect = (
            lambda constraints=None, **kwargs: page(
                [{"phid": "PHID-PROJ-" + constraints["query"], "fields": {"name": constraints["query"]}}]
            )
        )
        self.client.project.search_columns.return_value = page([])
        self.client.maniphest.search_tasks.return_value = page([])
        self.client.phriction.get_document_info.return_value = {}
        self.client.maniphest.edit_task.return_value = {
            "object": {"id": 101, "phid": "PHID-TASK-101"}
        }
        self.client.phriction.create_document.return_value = {"slug": "teams/demo/sprint-1/"}

    def _preview(self, text="# Sprint 1\nBuild;;@ana"):
        return self.preview("one.txt", text, self.config, self.tags)

    def test_preview_has_zero_writes_and_returns_todo_remarkup(self):
        result = self._preview()
        self.assertTrue(result["success"])
        self.assertIn("<td>TODO</td>", result["remarkup"])
        self.client.maniphest.edit_task.assert_not_called()
        self.client.phriction.create_document.assert_not_called()
        self.client.phriction.edit_document.assert_not_called()

    def test_preview_accepts_crlf_source_text(self):
        text = "# Sprint 1\r\nBuild;;@ana"
        result = self._preview(text)

        self.assertTrue(result["success"])

    def test_preview_renders_titles_with_estimations_when_requested(self):
        result = self.preview(
            "one.txt", "# Sprint 1\nBuild;1D;@ana", self.config, self.tags, True, True
        )

        self.assertTrue(result["success"])
        self.assertIn("Build [1D]", result["remarkup"])

    def test_preview_id_is_single_use(self):
        preview = self._preview()
        result = self.create(preview["previewId"])
        self.assertTrue(result["success"])
        second = self.create(preview["previewId"])
        self.assertEqual(second["error_code"], "SPRINT_PREVIEW_NOT_FOUND")

    def test_existing_wiki_is_edited_not_created(self):
        self.client.phriction.get_document_info.return_value = {
            "content": "arbitrary existing content"
        }
        preview = self._preview()
        self.assertTrue(preview["wiki"]["exists"])
        result = self.create(preview["previewId"])
        self.assertTrue(result["success"])
        self.client.phriction.edit_document.assert_called_once()
        self.client.phriction.create_document.assert_not_called()
        self.client.phriction.get_document_info.assert_called_once()

    def test_tasks_only_preview_and_execution_do_not_use_phriction(self):
        preview = self.preview(
            "one.txt", "# Sprint 1\nBuild;;@ana", None, self.tags, False
        )

        self.assertTrue(preview["success"])
        self.assertFalse(preview["publishWiki"])
        self.assertNotIn("wiki", preview)
        self.assertNotIn("remarkup", preview)
        self.client.phriction.get_document_info.assert_not_called()

        result = self.create(preview["previewId"])

        self.assertTrue(result["success"])
        self.assertFalse(result["publishedWiki"])
        self.client.phriction.create_document.assert_not_called()
        self.client.phriction.edit_document.assert_not_called()


if __name__ == "__main__":
    unittest.main()
