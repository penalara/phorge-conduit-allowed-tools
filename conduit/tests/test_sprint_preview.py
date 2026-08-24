import hashlib
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
        self.config = {"name": "Demo", "wikiBasePath": "teams/demo", "defaultTag": "Demo"}
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

    def _hash(self, text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _preview(self, text="# Sprint 1\nBuild;;@ana"):
        return self.preview("one.txt", text, self._hash(text), self.config, self.tags)

    def test_preview_has_zero_writes_and_returns_todo_remarkup(self):
        result = self._preview()
        self.assertTrue(result["success"])
        self.assertIn("<td>TODO</td>", result["remarkup"])
        self.client.maniphest.edit_task.assert_not_called()
        self.client.phriction.create_document.assert_not_called()
        self.client.phriction.edit_document.assert_not_called()

    def test_preview_rejects_source_hash_mismatch_before_reads(self):
        result = self.preview("one.txt", "# Sprint 1\nBuild;;@ana", "0" * 64, self.config, self.tags)
        self.assertEqual(result["error_code"], "SPRINT_SOURCE_HASH_MISMATCH")
        self.client.maniphest.search_tasks.assert_not_called()

    def test_preview_accepts_a_canonical_hash_for_crlf_source_text(self):
        text = "# Sprint 1\r\nBuild;;@ana"
        canonical_hash = self._hash(text.replace("\r\n", "\n"))

        result = self.preview("one.txt", text, canonical_hash, self.config, self.tags)

        self.assertTrue(result["success"])

    def test_preview_id_is_single_use(self):
        preview = self._preview()
        result = self.create(preview["previewId"], self._hash("# Sprint 1\nBuild;;@ana"))
        self.assertTrue(result["success"])
        second = self.create(preview["previewId"], self._hash("# Sprint 1\nBuild;;@ana"))
        self.assertEqual(second["error_code"], "SPRINT_PREVIEW_NOT_FOUND")

    def test_wiki_guard_aborts_before_task_writes(self):
        preview = self._preview()
        self.client.phriction.get_document_info.return_value = {"content": "changed"}
        result = self.create(preview["previewId"], self._hash("# Sprint 1\nBuild;;@ana"))
        self.assertEqual(result["error_code"], "SPRINT_WIKI_CHANGED_AFTER_PREVIEW")
        self.client.maniphest.edit_task.assert_not_called()

    def test_existing_wiki_is_edited_not_created(self):
        self.client.phriction.get_document_info.return_value = {
            "content": "<table><tr><th>Tiempo real</th></tr></table>"
        }
        preview = self._preview()
        result = self.create(preview["previewId"], self._hash("# Sprint 1\nBuild;;@ana"))
        self.assertTrue(result["success"])
        self.client.phriction.edit_document.assert_called_once()
        self.client.phriction.create_document.assert_not_called()

    def test_protected_wiki_values_and_unknown_format_request_confirmation(self):
        self.client.phriction.get_document_info.return_value = {
            "content": "<table><tr><th>Título tarea</th><th>Tiempo real</th><th>Observaciones</th></tr><tr><td>A</td><td>2h</td><td>Note</td></tr></table>"
        }
        protected = self._preview()
        self.assertTrue(protected["requiresOverwriteConfirmation"])
        self.assertEqual({item["value"] for item in protected["protectedValues"]}, {"2h", "Note"})
        self.client.phriction.get_document_info.return_value = {"content": "unrecognised"}
        unknown = self._preview("# Sprint 2\nBuild;;@ana")
        self.assertTrue(unknown["formatUnknown"])
        self.assertTrue(unknown["requiresOverwriteConfirmation"])


if __name__ == "__main__":
    unittest.main()
