"""Reusable client for connecting to a FortiGate over its REST API.

Authentication uses an API token (Bearer). Create a REST API admin on the
FortiGate, generate a token, and add an entry for it to ``inventory.yaml``
(see :mod:`fortigate.api.inventory`).

This module holds the raw :class:`FortiGateClient` and the
:class:`FortiGateAPIError` it raises -- nothing more than a typed way to
speak HTTP to the appliance. Helpers that discover how a particular
appliance is laid out and read config out of it are a separate layer built
on top of it, in :mod:`fortigate.config.exporter`.

Run this module directly to perform a proof-of-connection call against the
first firewall in the inventory::

    python -m fortigate.api.client
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if TYPE_CHECKING:
    from .inventory import FirewallEntry, VerifyType

__all__ = ["FortiGateClient", "FortiGateAPIError"]

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class FortiGateAPIError(Exception):
    """Raised when the FortiGate returns an HTTP error.

    Wraps the HTTP status code and, when available, the JSON error payload
    returned by the appliance (which typically carries ``http_status`` and
    ``cli_error`` fields useful for debugging).
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        payload: Any = None,
    ) -> None:
        self.status_code = status_code
        self.payload = payload
        detail = message
        if status_code is not None:
            detail = f"[HTTP {status_code}] {detail}"
        if payload is not None:
            detail = f"{detail} -- {payload}"
        super().__init__(detail)


class FortiGateClient:
    """A thin, reusable client for the FortiGate REST API.

    Construct it directly, or from an inventory entry via
    :meth:`from_entry`.

    :param host: FortiGate host/IP to connect to (required).
    :param token: REST API token (required).
    :param port: HTTPS port. Defaults to ``443``.
    :param verify: TLS verification -- ``True``/``False`` or a path to a CA
        bundle. Defaults to ``True``.
    :param vdom: Default VDOM sent as the ``vdom`` query param when set.
    :param timeout: Per-request timeout in seconds.
    :param retries: Number of automatic retries for connection errors and
        5xx/429 responses. ``0`` disables retrying.
    """

    def __init__(
        self,
        host: str,
        token: str,
        port: int = 443,
        verify: "VerifyType" = True,
        vdom: Optional[str] = None,
        timeout: float = 10.0,
        retries: int = 3,
    ) -> None:
        if not host:
            raise ValueError("host is required")
        if not token:
            raise ValueError("token is required")

        self.host = host
        self.port = port
        self.vdom = vdom
        self.timeout = timeout
        self.base_url = f"https://{host}:{port}/api/v2"

        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.session.verify = verify

        # Silence the noisy per-request warning when TLS verification is
        # explicitly disabled (common for self-signed lab appliances).
        if verify is False:
            requests.packages.urllib3.disable_warnings(
                requests.packages.urllib3.exceptions.InsecureRequestWarning
            )

        if retries and retries > 0:
            retry = Retry(
                total=retries,
                connect=retries,
                read=retries,
                status=retries,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(
                    {"GET", "HEAD", "DELETE", "POST", "OPTIONS", "PUT"}
                ),
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)

    @classmethod
    def from_entry(
        cls,
        entry: "FirewallEntry",
        *,
        timeout: float = 10.0,
        retries: int = 3,
    ) -> "FortiGateClient":
        """Build a client from an :class:`~fortigate.api.inventory.FirewallEntry`.

        VDOM is deliberately not part of a :class:`FirewallEntry` -- it's
        appliance state, discovered per call and passed explicitly on
        each request, not fixed at client construction.

        Example::

            entry = Inventory.load().get("fw1")
            with FortiGateClient.from_entry(entry) as fg:
                ...
        """
        return cls(
            host=entry.address,
            token=entry.token,
            port=entry.port,
            verify=entry.verify,
            timeout=timeout,
            retries=retries,
        )

    def __enter__(self) -> "FortiGateClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying session and its connection pool."""
        self.session.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Any = None,
        vdom: Optional[str] = None,
    ) -> Any:
        """Send a request to an ``/api/v2/``-relative path and return JSON.

        :param method: HTTP method (``GET``, ``POST``, ``PUT``, ``DELETE``).
        :param path: Path after ``/api/v2/`` (a leading slash is tolerated),
            e.g. ``"cmdb/firewall/address"`` or ``"monitor/system/status"``.
        :param params: Optional query parameters.
        :param json: Optional JSON request body.
        :param vdom: Per-call VDOM override; falls back to the client default.
        :raises FortiGateAPIError: on any HTTP error response.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"

        query = dict(params or {})
        effective_vdom = vdom if vdom is not None else self.vdom
        if effective_vdom is not None and "vdom" not in query:
            query["vdom"] = effective_vdom

        logger.debug("%s %s params=%s", method, url, query)
        try:
            response = self.session.request(
                method,
                url,
                params=query or None,
                json=json,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.error("%s %s failed: %s", method, url, exc)
            raise FortiGateAPIError(str(exc)) from exc

        if not response.ok:
            try:
                payload = response.json()
            except ValueError:
                payload = response.text
            logger.error(
                "%s %s -> HTTP %s: %s", method, url, response.status_code, payload
            )
            raise FortiGateAPIError(
                f"request to {path} failed",
                status_code=response.status_code,
                payload=payload,
            )

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def get(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        vdom: Optional[str] = None,
    ) -> Any:
        """Read data from the FortiGate and return the parsed JSON.

        Example: fg.get("monitor/system/status")
        """
        return self.request("GET", path, params=params, vdom=vdom)

    def post(
        self,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
        vdom: Optional[str] = None,
    ) -> Any:
        """POST to an ``/api/v2/``-relative path.

        Example: fg.post("cmdb/firewall/address", json={"name": "test"})
        """
        return self.request("POST", path, params=params, json=json, vdom=vdom)

    def put(
        self,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
        vdom: Optional[str] = None,
    ) -> Any:
        """PUT to an ``/api/v2/``-relative path.

        Example: fg.put("cmdb/firewall/address/test", json={"comment": "updated"})
        """
        return self.request("PUT", path, params=params, json=json, vdom=vdom)

    def delete(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        vdom: Optional[str] = None,
    ) -> Any:
        """DELETE an ``/api/v2/``-relative path.

        Example: fg.delete("cmdb/firewall/address/test")
        """
        return self.request("DELETE", path, params=params, vdom=vdom)


def _main() -> int:
    """Proof-of-connection: fetch system status and print key facts.

    Connects to the first firewall listed in ``inventory.yaml``.
    """
    from .inventory import Inventory

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    try:
        entry = next(iter(Inventory.load()), None)
        if entry is None:
            logger.error("inventory.yaml is empty")
            return 1

        with FortiGateClient.from_entry(entry) as fg:
            status = fg.get("monitor/system/status")
            if not isinstance(status, dict):
                raise FortiGateAPIError(f"unexpected status response: {status!r}")

            results = status.get("results", {})
            if not isinstance(results, dict):
                results = {}

            hostname = results.get("hostname", "<unknown>")
            model_name = results.get("model_name", "")
            model_number = results.get("model_number", "")
            hardware = (
                f"{model_name} {model_number}".strip()
                or results.get("model", "<unknown>")
            )
            version = status.get("version", "<unknown>")
            build = status.get("build")
            firmware = f"{version} (build {build})" if build is not None else version
            serial = status.get("serial", "<unknown>")
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except (ValueError, FortiGateAPIError) as exc:
        logger.error("connection failed: %s", exc)
        return 1

    print(f"Connected to FortiGate '{hostname}' (inventory name: '{entry.name}')")
    print(f"  hardware: {hardware}")
    print(f"  firmware: {firmware}")
    print(f"  serial:   {serial}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
