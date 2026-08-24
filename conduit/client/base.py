import json
import urllib.parse
from abc import ABC
from typing import Any, Dict, FrozenSet, Iterable, Optional

import httpx

from conduit.utils import PhabricatorAPIError


def resolve_client_allowed_methods(
    methods: Optional[Iterable[str]], allow_unrestricted: bool
) -> Optional[FrozenSet[str]]:
    """Resolve an explicit or configured allowlist before opening HTTP clients."""

    if allow_unrestricted and methods is not None:
        raise ValueError(
            "allowed_methods cannot be combined with allow_unrestricted"
        )
    if allow_unrestricted:
        return None
    if methods is not None:
        return frozenset(methods)

    # Import lazily to avoid a client/handler import cycle at module load.
    from conduit.allowed_methods import load_allowed_methods, resolve_config_path

    return frozenset(load_allowed_methods(resolve_config_path()))


class BasePhabricatorClient(ABC):
    def __init__(
        self,
        api_url: str,
        api_token: str,
        http_client: Optional[httpx.Client] = None,
        allowed_methods: Optional[Iterable[str]] = None,
        allow_unrestricted: bool = False,
    ):
        """
        Initialize the base Phabricator client.

        Args:
            api_url: Base URL for the Phabricator API
            api_token: API token for authentication
            http_client: Optional httpx client to reuse
            allowed_methods: Explicit Conduit allowlist. When omitted, load the
                effective conduit-allowed-methods.json configuration.
            allow_unrestricted: Administrative escape hatch used only while
                initializing the allowlist with conduit.query.
        """
        self.api_url = api_url.rstrip("/") + "/"
        self.api_token = api_token
        self._owns_client = http_client is None
        self._allowed_methods = resolve_client_allowed_methods(
            allowed_methods, allow_unrestricted
        )

        if http_client is None:
            self.client = httpx.Client(
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "ModelContextProtocol/1.0 (Autonomous; +https://github.com/modelcontextprotocol/servers)",
                },
                timeout=30.0,
                follow_redirects=True,
            )
        else:
            self.client = http_client

    def set_allowed_methods(self, methods: Optional[Iterable[str]]) -> None:
        """Restrict this client; ``None`` is an intentional deny-all policy."""
        self._allowed_methods = frozenset(methods or ())

    def _make_request(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Make a request to the Phabricator API.

        Args:
            method: API method name (e.g., 'maniphest.search')
            params: Parameters to send with the request, every value is JSON formatted

        Returns:
            Response data from the API

        Raises:
            PhabricatorAPIError: If the API returns an error
            httpx.HTTPError: If there's a network error
        """
        if self._allowed_methods is not None and method not in self._allowed_methods:
            raise PhabricatorAPIError(
                "Conduit method '{}' is not in the allowed methods list".format(
                    method
                ),
                error_code="METHOD_NOT_ALLOWED",
            )

        if params is None:
            params = {}
        else:
            # Callers may reuse parameter objects across requests.
            params = dict(params)

        params["api.token"] = self.api_token

        url = urllib.parse.urljoin(self.api_url, method)

        try:
            response = self.client.post(url, data=params)
            response.raise_for_status()

            data = response.json()

            if data.get("error_code"):
                raise PhabricatorAPIError(
                    message=f"API Error: {data.get('error_info', 'Unknown error')}",
                    error_code=data.get("error_code"),
                    error_info=data.get("error_info"),
                )

            return data.get("result", {})

        except httpx.HTTPError as e:
            raise PhabricatorAPIError(f"Network error: {str(e)}")
        except json.JSONDecodeError as e:
            raise PhabricatorAPIError(f"Invalid JSON response: {str(e)}")

    def close(self):
        """Close the HTTP client if we own it."""
        if self._owns_client and self.client:
            self.client.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
