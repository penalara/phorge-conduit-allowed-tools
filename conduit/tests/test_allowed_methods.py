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
