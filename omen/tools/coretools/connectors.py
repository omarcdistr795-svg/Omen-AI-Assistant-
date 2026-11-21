"""MCP-style connector interface for optional external calendar/notes/web tools.

Connectors are disabled by default. Local-first storage is always used unless a
connector is explicitly enabled in ``core_tools.connectors``. External
connectors are refused while the Phase 1 cloud budget is $0/disabled, so no
network call or cloud spend can happen by accident.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from .base import CoreToolError


class Connector(Protocol):
    name: str
    kind: str
    external: bool

    def health(self) -> dict[str, Any]: ...

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


class MockConnector:
    """Clearly labelled offline connector used for tests and demos."""

    external = False

    def __init__(
        self,
        name: str,
        kind: str,
        *,
        responses: dict[str, Any] | None = None,
        external: bool = False,
    ) -> None:
        self.name = name
        self.kind = kind
        self.external = bool(external)
        self._responses = dict(responses or {})
        self.calls: list[dict[str, Any]] = []

    def health(self) -> dict[str, Any]:
        return {"ok": True, "connector": self.name, "kind": self.kind, "mode": "mock_offline"}

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"method": method, "params": dict(params)})
        key = str(method)
        if key not in self._responses:
            raise CoreToolError(f"Mock connector {self.name!r} has no response for {key!r}")
        value = self._responses[key]
        if callable(value):  # pragma: no cover - convenience for test fixtures
            return dict(value(dict(params)))
        return dict(value)


ConnectorFactory = Callable[[str, dict[str, Any], dict[str, Any]], Connector]

def _mock_factory(name: str, spec: dict[str, Any], _config: dict[str, Any]) -> Connector:
    return MockConnector(
        name,
        str(spec.get("kind") or "generic"),
        responses=dict(spec.get("responses") or {}),
        external=bool(spec.get("external", False)),
    )


_BUILTIN_FACTORIES: dict[str, ConnectorFactory] = {"mock": _mock_factory}


class ConnectorRegistry:
    """Resolve connectors by capability (``web_search``, ``calendar``, ...)."""

    def __init__(
        self,
        config: Any,
        *,
        factories: dict[str, ConnectorFactory] | None = None,
    ) -> None:
        self.config = config
        self._factories = dict(_BUILTIN_FACTORIES)
        self._factories.update(factories or {})
        self._by_kind: dict[str, Connector] = {}
        specs: dict[str, Any] = dict(config.get("core_tools.connectors") or {})
        for name, raw in specs.items():
            spec = dict(raw or {})
            if not spec.get("enabled", False):
                continue
            external = bool(spec.get("external", True))
            if external and not self._external_allowed():
                raise CoreToolError(
                    f"Connector {name!r} is external but the cloud budget is disabled/$0; "
                    "enable a budget in config.local.yaml to opt in (BLOCKED-EXTERNAL)"
                )
            kind = str(spec.get("kind") or "")
            if not kind:
                raise CoreToolError(f"Connector {name!r} must declare a kind")
            factory_name = str(spec.get("type") or "")
            factory = self._factories.get(factory_name)
            if factory is None:
                raise CoreToolError(f"Unknown connector type {factory_name!r} for {name!r}")
            connector = factory(name, spec, config.sections if hasattr(config, "sections") else {})
            if connector.kind != kind:
                raise CoreToolError(f"Connector {name!r} kind mismatch: {connector.kind!r} != {kind!r}")
            if kind in self._by_kind:
                raise CoreToolError(f"More than one connector registered for kind {kind!r}")
            self._by_kind[kind] = connector

    def _external_allowed(self) -> bool:
        enabled = bool(self.config.get("cost.enabled", False))
        budget = float(self.config.get("cost.monthly_budget_usd", 0.0) or 0.0)
        return enabled and budget > 0

    def get(self, kind: str) -> Connector | None:
        return self._by_kind.get(kind)

    def get_by_name(self, name: str) -> Connector | None:
        for connector in self._by_kind.values():
            if connector.name == name:
                return connector
        return None

    def status(self) -> dict[str, Any]:
        return {
            kind: {"name": connector.name, "external": connector.external, **connector.health()}
            for kind, connector in sorted(self._by_kind.items())
        }
