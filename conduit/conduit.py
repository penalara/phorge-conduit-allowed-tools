import argparse
import os
import sys

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from conduit.allowed_methods import (
    CONFIG_ENV_VAR,
    load_allowed_methods,
    methods_from_conduit_query,
    register_allowed_methods,
    resolve_config_path,
    write_allowed_methods,
)
from conduit.client import PhabricatorClient


class PhabricatorConfig(object):
    def __init__(self, token=None, require_token=True):
        self.token = token or os.getenv("PHABRICATOR_TOKEN")
        self.url = os.getenv("PHABRICATOR_URL")
        self.proxy = os.getenv("PHABRICATOR_PROXY")
        self.disable_cert_verify = os.getenv(
            "PHABRICATOR_DISABLE_CERT_VERIFY", ""
        ).lower() in ("1", "true", "yes")

        if require_token and not self.token:
            raise ValueError("PHABRICATOR_TOKEN is required")

        if not self.url:
            raise ValueError("PHABRICATOR_URL environment variable is required")

        if self.token and len(self.token) != 32:
            raise ValueError("PHABRICATOR_TOKEN must be exactly 32 characters long")

        if not self.url.startswith(("http://", "https://")):
            raise ValueError("PHABRICATOR_URL must start with http:// or https://")

        if self.url and not self.url.endswith("/"):
            self.url += "/"

    @property
    def api_headers(self):
        return {"Content-Type": "application/x-www-form-urlencoded"}

    @property
    def base_params(self):
        return {"api.token": self.token}


class ConduitApp:
    """Main application class for Conduit MCP Server."""

    def __init__(self, config: PhabricatorConfig, use_sse: bool = False):
        self.config = config
        self.use_sse = use_sse
        self.mcp = FastMCP("Conduit")
        self._client = None

    def get_client(self):
        """Get or create a Phabricator client instance."""
        # In SSE mode, always create a fresh client for each request
        # to prevent user identity confusion in multi-user environments
        if self.use_sse:
            headers = get_http_headers()
            http_token = headers.get("x-phabricator-token")

            if not http_token:
                raise ValueError("Must provide X-PHABRICATOR-TOKEN in SSE mode.")

            if len(http_token) != 32:
                raise ValueError(
                    "PHABRICATOR_TOKEN from HTTP header must be exactly 32 characters long"
                )

            return PhabricatorClient(
                self.config.url,
                http_token,
                proxy=self.config.proxy,
                disable_cert_verify=self.config.disable_cert_verify,
            )

        # For stdio mode, use cached client (backward compatibility)
        if self._client is not None:
            return self._client

        if not self.config.token:
            raise ValueError("PHABRICATOR_TOKEN is required for stdio mode")

        self._client = PhabricatorClient(
            self.config.url,
            self.config.token,
            proxy=self.config.proxy,
            disable_cert_verify=self.config.disable_cert_verify,
        )
        return self._client

    def call_method(self, method, params):
        """Invoke one registered method and release request-scoped SSE clients."""
        client = self.get_client()
        try:
            return client.conduit.call_method(method, params)
        finally:
            if self.use_sse:
                client.close()

    def register_tools(self, allowed_methods):
        """Register only administrator-allowed Conduit methods."""
        register_allowed_methods(self.mcp, allowed_methods, self.call_method)

    def close(self):
        """Close the persistent stdio client, if one was created."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def run_sse_mode(self, host: str, port: int):
        """Run the application in SSE mode."""
        print(f"Starting in HTTP/SSE mode on {host}:{port}", file=sys.stderr)
        self.mcp.run(
            transport="sse",
            host=host,
            port=port,
            path="/sse",
        )

    def run_stdio_mode(self):
        """Run the application in stdio mode."""
        print("Starting in stdio mode", file=sys.stderr)
        self.mcp.run(transport="stdio")


# Global app instance
_app = None


def print_server_info(config):
    """Print server configuration information."""
    print("Starting Conduit MCP Server...", file=sys.stderr)
    print(f"Phabricator URL: {config.url}", file=sys.stderr)
    print(f"Token configured: {'Yes' if config.token else 'No'}", file=sys.stderr)
    print(f"Proxy configured: {'Yes' if config.proxy else 'No'}", file=sys.stderr)
    print(
        f"SSL certificate verification: {'Disabled' if config.disable_cert_verify else 'Enabled'}",
        file=sys.stderr,
    )


def _configuration_error(path, reason):
    print("Conduit MCP configuration error: {}".format(reason), file=sys.stderr)
    print("Configuration path: {}".format(path), file=sys.stderr)
    print("Create it with: conduit-mcp --init-tools-config", file=sys.stderr)
    print(
        "Or select another file with --tools-config or {}.".format(CONFIG_ENV_VAR),
        file=sys.stderr,
    )
    print(
        'Expected JSON: {"allowed_tools": ["phriction.document.search"]}',
        file=sys.stderr,
    )


def main():
    """Main entry point for the Conduit MCP Server."""
    parser = argparse.ArgumentParser(
        description="Conduit MCP Server for Phabricator and Phorge"
    )
    parser.add_argument(
        "--host",
        "-H",
        default=None,
        help="Host to bind to for SSE transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=None,
        help="Port to bind to for SSE transport (default: 8000)",
    )
    parser.add_argument(
        "--tools-config",
        help="Path to conduit-allowed-methods.json",
    )
    parser.add_argument(
        "--print-tools-config-path",
        action="store_true",
        help="Print the effective tools configuration path and exit",
    )
    parser.add_argument(
        "--init-tools-config",
        action="store_true",
        help="Create the tools configuration from conduit.query and exit",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing configuration when initializing",
    )

    args = parser.parse_args()
    config_path = resolve_config_path(args.tools_config)

    if args.print_tools_config_path:
        print(str(config_path))
        return 0

    if args.force and not args.init_tools_config:
        parser.error("--force can only be used with --init-tools-config")

    if args.init_tools_config:
        try:
            config = PhabricatorConfig(require_token=True)
            client = PhabricatorClient(
                config.url,
                config.token,
                proxy=config.proxy,
                disable_cert_verify=config.disable_cert_verify,
            )
            try:
                methods = methods_from_conduit_query(client.conduit.query_methods())
            finally:
                client.close()
            write_allowed_methods(config_path, methods, force=args.force)
        except Exception as error:
            print(
                "Unable to initialize {}: {}".format(config_path, error),
                file=sys.stderr,
            )
            return 2
        print(
            "Wrote {} visible Conduit methods to {}".format(len(methods), config_path),
            file=sys.stderr,
        )
        return 0

    use_sse = args.host is not None or args.port is not None

    try:
        allowed_methods = load_allowed_methods(config_path)
    except Exception as error:
        _configuration_error(config_path, str(error))
        return 2
    try:
        config = PhabricatorConfig(require_token=not use_sse)
    except Exception as error:
        print("Conduit MCP startup error: {}".format(error), file=sys.stderr)
        return 2

    print_server_info(config)
    if use_sse:
        print(
            "In HTTP/SSE mode provide X-PHABRICATOR-TOKEN per request.", file=sys.stderr
        )

    # Create and run the application
    app = ConduitApp(config, use_sse)
    app.register_tools(allowed_methods)

    try:
        if use_sse:
            app.run_sse_mode(args.host or "127.0.0.1", args.port or 8000)
        else:
            app.run_stdio_mode()
    finally:
        app.close()
    return 0


# Backward compatibility functions
def get_config():
    """Get configuration for backward compatibility."""
    return PhabricatorConfig(require_token=False)


def get_client():
    """Get client for backward compatibility."""
    config = get_config()
    return PhabricatorClient(
        config.url,
        config.token or "dummy_token",
        proxy=config.proxy,
        disable_cert_verify=config.disable_cert_verify,
    )


if __name__ == "__main__":
    main()
