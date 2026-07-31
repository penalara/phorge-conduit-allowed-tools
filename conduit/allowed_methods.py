"""Loading and registering the administrator-controlled Conduit allowlist."""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from fastmcp import FastMCP
from platformdirs import user_config_path

from conduit.tools.handlers import handle_api_errors


CONFIG_FILENAME = "conduit-allowed-methods.json"
CONFIG_ENV_VAR = "CONDUIT_TOOLS_CONFIG"
MAX_CONFIG_BYTES = 1024 * 1024
MAX_ALLOWED_TOOLS = 10000
MAX_METHOD_NAME_LENGTH = 256
METHOD_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


def resolve_config_path(explicit_path: Optional[str] = None) -> Path:
    """Resolve the configuration path without reading or creating it."""
    configured_path = explicit_path or os.getenv(CONFIG_ENV_VAR)
    if configured_path:
        return Path(configured_path).expanduser()
    return user_config_path("conduit-mcp", appauthor=False) / CONFIG_FILENAME


def _validate_method_name(method: Any) -> str:
    if not isinstance(method, str) or not method:
        raise ValueError("Each allowed_tools entry must be a non-empty string")
    if len(method) > MAX_METHOD_NAME_LENGTH:
        raise ValueError("An allowed Conduit method name is too long")
    if not METHOD_NAME_PATTERN.fullmatch(method):
        raise ValueError("Invalid Conduit method name: {}".format(method))
    return method


def validate_allowed_methods(config: Any) -> List[str]:
    """Validate the intentionally small JSON configuration contract."""
    if not isinstance(config, dict):
        raise ValueError("The configuration root must be a JSON object")
    if set(config) != {"allowed_tools"}:
        raise ValueError("The configuration must contain only an allowed_tools key")

    methods = config["allowed_tools"]
    if not isinstance(methods, list):
        raise ValueError("allowed_tools must be a JSON array")
    if len(methods) > MAX_ALLOWED_TOOLS:
        raise ValueError("The configuration contains too many allowed tools")

    validated = [_validate_method_name(method) for method in methods]
    if len(set(validated)) != len(validated):
        raise ValueError("allowed_tools must not contain duplicate methods")
    return sorted(validated)


def load_allowed_methods(path: Path) -> List[str]:
    """Read an allowlist from a regular UTF-8 JSON file."""
    if not path.exists():
        raise FileNotFoundError(str(path))
    if path.is_symlink() or not path.is_file():
        raise ValueError("Configuration path must be a regular file: {}".format(path))
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("Configuration file is too large: {}".format(path))

    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as error:
        raise ValueError("Configuration file must be UTF-8: {}".format(path)) from error
    except json.JSONDecodeError as error:
        raise ValueError(
            "Configuration file is not valid JSON: {}".format(error)
        ) from error
    return validate_allowed_methods(config)


def methods_from_conduit_query(result: Any) -> List[str]:
    """Extract valid visible method names from a conduit.query response."""
    if not isinstance(result, dict):
        raise ValueError("conduit.query returned an invalid response")
    return sorted(
        {
            method
            for method in result.keys()
            if isinstance(method, str) and METHOD_NAME_PATTERN.fullmatch(method)
        }
    )


def write_allowed_methods(
    path: Path, methods: Iterable[str], force: bool = False
) -> None:
    """Atomically write a validated allowlist without exposing partial files."""
    validated = validate_allowed_methods({"allowed_tools": list(methods)})
    if path.exists() and not force:
        raise FileExistsError(str(path))

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".conduit-allowed-methods-", dir=str(path.parent), text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump({"allowed_tools": validated}, temporary_file, indent=2)
            temporary_file.write("\n")
        os.replace(temporary_name, str(path))
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def register_allowed_methods(
    mcp: FastMCP,
    methods: Iterable[str],
    call_method: Callable[[str, Dict[str, Any]], Any],
) -> None:
    """Register one fixed-endpoint MCP tool for every allowed method."""

    for method in methods:

        def make_invoke(
            fixed_method: str,
        ) -> Callable[[Optional[Dict[str, Any]]], dict]:
            def invoke(params: Optional[Dict[str, Any]] = None) -> dict:
                if params is None:
                    params = {}
                if not isinstance(params, dict):
                    raise ValueError("params must be a JSON object")
                return {"success": True, "result": call_method(fixed_method, params)}

            return invoke

        invoke = make_invoke(method)

        invoke.__name__ = "call_" + method.replace(".", "_")
        invoke.__doc__ = (
            'Call the Phorge Conduit method "{}". '
            'Pass native Conduit parameters in the "params" object.'.format(method)
        )
        mcp.tool(name=method, description=invoke.__doc__)(handle_api_errors(invoke))
