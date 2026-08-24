import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from conduit.allowed_methods import (
    CONFIG_ENV_VAR,
    load_allowed_methods,
    methods_from_conduit_query,
    register_allowed_methods,
    resolve_config_path,
    write_allowed_methods,
)
from conduit.client import PhabricatorAPIError, PhabricatorClient
from conduit.client.base import BasePhabricatorClient
from conduit.client.diffusion import DiffusionClient
from conduit.client.project import ProjectClient


REFERENCE_ALLOWED_METHODS = [
    "maniphest.createtask",
    "maniphest.query",
    "maniphest.search",
    "maniphest.edit",
    "transaction.search",
    "maniphest.status.search",
    "maniphest.priority.search",
    "user.search",
    "user.query",
    "project.search",
    "project.column.search",
    "phid.query",
    "feed.query",
    "phriction.content.search",
    "phriction.create",
    "phriction.document.edit",
    "phriction.document.search",
    "phriction.edit",
    "phriction.history",
    "phriction.info",
]


class TestAllowedMethodsConfiguration(unittest.TestCase):
    def test_explicit_path_has_priority(self):
        with patch.dict(os.environ, {CONFIG_ENV_VAR: "from-environment.json"}):
            self.assertEqual(
                resolve_config_path("explicit.json"), Path("explicit.json")
            )

    def test_environment_path_has_priority_over_default(self):
        with patch.dict(os.environ, {CONFIG_ENV_VAR: "from-environment.json"}):
            self.assertEqual(resolve_config_path(), Path("from-environment.json"))

    def test_loads_valid_configuration_in_sorted_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conduit-allowed-methods.json"
            path.write_text(
                json.dumps({"allowed_tools": ["phriction.edit", "conduit.ping"]}),
                encoding="utf-8",
            )
            self.assertEqual(
                load_allowed_methods(path), ["conduit.ping", "phriction.edit"]
            )

    def test_rejects_unknown_keys_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conduit-allowed-methods.json"
            path.write_text(
                json.dumps(
                    {
                        "allowed_tools": ["phriction.edit", "phriction.edit"],
                        "unexpected": True,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_allowed_methods(path)

    def test_empty_allowlist_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conduit-allowed-methods.json"
            path.write_text('{"allowed_tools": []}', encoding="utf-8")
            self.assertEqual(load_allowed_methods(path), [])

    def test_extracts_only_valid_method_names(self):
        methods = methods_from_conduit_query(
            {"phriction.edit": {}, "invalid": {}, "Bad.Name": {}}
        )
        self.assertEqual(methods, ["phriction.edit"])

    def test_write_does_not_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conduit-allowed-methods.json"
            write_allowed_methods(path, ["conduit.ping"])
            with self.assertRaises(FileExistsError):
                write_allowed_methods(path, ["phriction.edit"])
            write_allowed_methods(path, ["phriction.edit"], force=True)
            self.assertEqual(load_allowed_methods(path), ["phriction.edit"])


class TestDynamicToolRegistration(unittest.TestCase):
    def test_registers_fixed_method_tools(self):
        registered = {}

        class FakeMCP:
            def tool(self, name, description):
                def register(function):
                    registered[name] = function
                    return function

                return register

        calls = []
        register_allowed_methods(
            FakeMCP(),
            ["conduit.ping", "phriction.edit"],
            lambda method, params: calls.append((method, params)) or method,
        )

        self.assertEqual(set(registered), {"conduit.ping", "phriction.edit"})
        self.assertEqual(
            registered["conduit.ping"](params={}),
            {"success": True, "result": "conduit.ping"},
        )
        self.assertEqual(
            registered["phriction.edit"](params={"title": "Example"}),
            {"success": True, "result": "phriction.edit"},
        )
        self.assertEqual(
            calls,
            [
                ("conduit.ping", {}),
                ("phriction.edit", {"title": "Example"}),
            ],
        )

    def test_rejects_non_object_params(self):
        registered = []

        class FakeMCP:
            def tool(self, name, description):
                def register(function):
                    registered.append(function)
                    return function

                return register

        register_allowed_methods(FakeMCP(), ["conduit.ping"], Mock())
        result = registered[0](params="not-an-object")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")


class TestClientAllowlistBoundary(unittest.TestCase):
    def _write_config(self, directory, methods=REFERENCE_ALLOWED_METHODS):
        path = Path(directory) / "conduit-allowed-methods.json"
        path.write_text(
            json.dumps({"allowed_tools": methods}), encoding="utf-8"
        )
        return path

    def test_direct_client_loads_effective_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_config(directory)
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "result": {"ok": True},
                "error_code": None,
            }
            http_client = Mock()
            http_client.post.return_value = response

            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}):
                client = BasePhabricatorClient(
                    "https://phorge.example/api/",
                    "api-token",
                    http_client=http_client,
                )

            for method in REFERENCE_ALLOWED_METHODS:
                self.assertEqual(client._make_request(method), {"ok": True})

            self.assertEqual(
                http_client.post.call_count, len(REFERENCE_ALLOWED_METHODS)
            )

    def test_methods_outside_effective_allowlist_are_denied_before_http(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_config(directory)
            http_client = Mock()
            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}):
                client = BasePhabricatorClient(
                    "https://phorge.example/api/",
                    "api-token",
                    http_client=http_client,
                )

            for method in [
                "project.edit",
                "diffusion.repository.edit",
                "differential.revision.edit",
                "conduit.query",
            ]:
                with self.subTest(method=method):
                    with self.assertRaises(PhabricatorAPIError) as raised:
                        client._make_request(method)
                    self.assertEqual(
                        raised.exception.error_code, "METHOD_NOT_ALLOWED"
                    )

            http_client.post.assert_not_called()

    def test_previous_integration_mutations_are_denied_before_http(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_config(directory)
            http_client = Mock()
            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}):
                project = ProjectClient(
                    "https://phorge.example/api/",
                    "api-token",
                    http_client=http_client,
                )
                diffusion = DiffusionClient(
                    "https://phorge.example/api/",
                    "api-token",
                    http_client=http_client,
                )

            with self.assertRaises(PhabricatorAPIError) as project_error:
                project.create_project("Must not be created")
            with self.assertRaises(PhabricatorAPIError) as repository_error:
                diffusion.create_repository("must-not-be-created")

            self.assertEqual(
                project_error.exception.error_code, "METHOD_NOT_ALLOWED"
            )
            self.assertEqual(
                repository_error.exception.error_code, "METHOD_NOT_ALLOWED"
            )
            http_client.post.assert_not_called()

    def test_missing_effective_allowlist_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(missing)}):
                with self.assertRaises(FileNotFoundError):
                    BasePhabricatorClient(
                        "https://phorge.example/api/", "api-token"
                    )

    def test_setting_none_is_deny_all(self):
        client = BasePhabricatorClient(
            "https://phorge.example/api/",
            "api-token",
            http_client=Mock(),
            allowed_methods=["project.search"],
        )
        client.set_allowed_methods(None)

        with self.assertRaises(PhabricatorAPIError) as raised:
            client._make_request("project.search")

        self.assertEqual(raised.exception.error_code, "METHOD_NOT_ALLOWED")
        client.client.post.assert_not_called()

    def test_unified_client_applies_explicit_allowlist_to_every_module(self):
        client = PhabricatorClient(
            "https://phorge.example/api/",
            "api-token",
            allowed_methods=REFERENCE_ALLOWED_METHODS,
        )
        try:
            for module in [
                client.maniphest,
                client.differential,
                client.diffusion,
                client.project,
                client.user,
                client.file,
                client.conduit,
                client.harbormaster,
                client.paste,
                client.phriction,
                client.remarkup,
                client.macro,
                client.flag,
                client.phid,
            ]:
                self.assertEqual(
                    module._allowed_methods, frozenset(REFERENCE_ALLOWED_METHODS)
                )
        finally:
            client.close()

    def test_unrestricted_mode_is_explicit_and_cannot_mix_with_allowlist(self):
        client = BasePhabricatorClient(
            "https://phorge.example/api/",
            "api-token",
            http_client=Mock(),
            allow_unrestricted=True,
        )
        self.assertIsNone(client._allowed_methods)

        with self.assertRaises(ValueError):
            BasePhabricatorClient(
                "https://phorge.example/api/",
                "api-token",
                http_client=Mock(),
                allowed_methods=["conduit.query"],
                allow_unrestricted=True,
            )
