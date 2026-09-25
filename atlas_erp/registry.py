"""Standalone Atlas ERP registry.

The registry is deliberately local.  It has no peer, service, or persistence
runtime dependency.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field


BASELINE_CLUSTERS = (
    "masterdata",
    "inventory",
    "purchasing",
    "sales",
    "finance",
    "admin",
)

DEFAULT_APP_ID = "atlas-erp"
DEFAULT_CONNECT_VERSION = "1.0.0"


class RegistryError(ValueError):
    """Raised when the product registry receives an invalid registration."""


class DuplicateRegistrationError(RegistryError):
    """Raised when a cluster, plugin, module, or capability is duplicated."""


class UnknownRegistrationError(RegistryError):
    """Raised when a plugin or module refers to an unregistered parent."""


def _name(value: object, kind: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{kind} must be a non-empty string")
    return value.strip()


def _permissions(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    values: Iterable[object]
    if isinstance(value, str):
        values = (value,)
    else:
        try:
            values = tuple(value)  # type: ignore[arg-type]
        except TypeError as exc:
            raise RegistryError("permissions must be an iterable of strings") from exc

    cleaned: set[str] = set()
    for permission in values:
        cleaned.add(_name(permission, "permission"))
    return frozenset(cleaned)


@dataclass
class Module:
    """The smallest named unit in the product hierarchy."""

    cluster: str
    plugin: str
    name: str
    capabilities: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def path(self) -> str:
        return f"{self.cluster}.{self.plugin}.{self.name}"


@dataclass
class Plugin:
    """A unit of product functionality within a cluster."""

    cluster: str
    name: str
    modules: dict[str, Module] = field(default_factory=dict)

    @property
    def path(self) -> str:
        return f"{self.cluster}.{self.name}"


@dataclass
class Cluster:
    """A product-level capability area."""

    name: str
    plugins: dict[str, Plugin] = field(default_factory=dict)


class Registry:
    """Register the ``core -> cluster -> plugin -> module`` hierarchy.

    The six contract baseline clusters are seeded on construction.  A caller
    can opt out with ``seed_baseline=False`` when building an isolated fixture.
    """

    def __init__(
        self,
        app_id: str = DEFAULT_APP_ID,
        connect_version: str = DEFAULT_CONNECT_VERSION,
        *,
        seed_baseline: bool = True,
    ) -> None:
        self.app_id = _name(app_id, "app_id")
        self.connect_version = _name(connect_version, "connect_version")
        self._clusters: dict[str, Cluster] = {}
        self._capability_permissions: dict[str, frozenset[str]] = {}

        if seed_baseline:
            for cluster_name in BASELINE_CLUSTERS:
                self.register_cluster(cluster_name)

    @property
    def cluster_names(self) -> tuple[str, ...]:
        return tuple(self._clusters)

    @property
    def clusters(self) -> Mapping[str, Cluster]:
        """Return a shallow snapshot of the registered clusters."""
        return dict(self._clusters)

    def register_cluster(self, name: str) -> Cluster:
        name = _name(name, "cluster name")
        if name in self._clusters:
            raise DuplicateRegistrationError(f"cluster already registered: {name}")
        cluster = Cluster(name)
        self._clusters[name] = cluster
        return cluster

    def register_plugin(self, cluster_name: str, name: str) -> Plugin:
        cluster_name = _name(cluster_name, "cluster name")
        name = _name(name, "plugin name")
        try:
            cluster = self._clusters[cluster_name]
        except KeyError as exc:
            raise UnknownRegistrationError(
                f"cannot register plugin in unknown cluster: {cluster_name}"
            ) from exc
        if name in cluster.plugins:
            raise DuplicateRegistrationError(
                f"plugin already registered: {cluster_name}.{name}"
            )
        plugin = Plugin(cluster_name, name)
        cluster.plugins[name] = plugin
        return plugin

    def register_module(
        self,
        cluster_name: str,
        plugin_name: str,
        module_name: str,
        capabilities: Mapping[str, Iterable[str]] | None = None,
    ) -> Module:
        cluster_name = _name(cluster_name, "cluster name")
        plugin_name = _name(plugin_name, "plugin name")
        module_name = _name(module_name, "module name")
        try:
            cluster = self._clusters[cluster_name]
        except KeyError as exc:
            raise UnknownRegistrationError(
                f"cannot register module in unknown cluster: {cluster_name}"
            ) from exc
        try:
            plugin = cluster.plugins[plugin_name]
        except KeyError as exc:
            raise UnknownRegistrationError(
                f"cannot register module in unknown plugin: "
                f"{cluster_name}.{plugin_name}"
            ) from exc
        if module_name in plugin.modules:
            raise DuplicateRegistrationError(
                f"module already registered: {plugin.path}.{module_name}"
            )
        if capabilities is None:
            capabilities = {}
        if not isinstance(capabilities, Mapping):
            raise RegistryError("capabilities must map names to permissions")

        normalized: dict[str, frozenset[str]] = {}
        for capability, permission_values in capabilities.items():
            capability = _name(capability, "capability name")
            if capability in self._capability_permissions:
                raise DuplicateRegistrationError(
                    f"capability already registered: {capability}"
                )
            normalized[capability] = _permissions(permission_values)

        module = Module(cluster_name, plugin_name, module_name, normalized)
        plugin.modules[module_name] = module
        self._capability_permissions.update(normalized)
        return module

    def manifest(self) -> dict[str, object]:
        """Return the local Connect manifest as a JSON-friendly dictionary."""
        return {
            "app_id": self.app_id,
            "connect_version": self.connect_version,
            "capabilities": sorted(self._capability_permissions),
            "permissions": {
                capability: sorted(permissions)
                for capability, permissions in sorted(self._capability_permissions.items())
            },
        }
