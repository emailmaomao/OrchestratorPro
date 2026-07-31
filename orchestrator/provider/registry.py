"""The provider registry — config-driven resolution of ``(domain, role)``.

The single point where a provider name becomes a provider instance. Callers ask
for a *role* — ``planner``, ``worker``, ``summarizer`` — and receive whatever
the configuration bound to it. Nothing above this module names Claude, Hermes,
or an OpenAI-compatible endpoint.

Two rules from `docs/030_PROVIDER_INTERFACE.md` §6 are enforced here rather than
left to convention:

* **Registration is by explicit factory.** No import-side-effect discovery, no
  plugin auto-scan. Discovery-by-import makes startup order load-bearing and
  turns a missing dependency into a mystery.
* **:meth:`Registry.startup_all` runs before any task starts** and fails the run
  on an unhealthy required provider — the "fail fast at startup" obligation of
  §2.1, applied across the whole set at once.

Instances are cached per ``(domain, role)``, so two callers asking for the same
role share one provider and one connection, while different roles stay
independent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from orchestrator.core.config import OrchestratorConfig, ProviderConfig
from orchestrator.provider.base import (
    Domain,
    ErrorCode,
    HealthReport,
    Provider,
    ProviderContext,
    ProviderError,
)

__all__ = [
    "ProviderFactory",
    "ProviderRegistry",
    "build_default_registry",
    "settings_for",
]

#: Binding preference when several known model providers are configured.
#: Deterministic on purpose: the old rule was "first configured name in TOML
#: order", which bound two identical-content files differently depending on
#: table order.
_MODEL_PREFERENCE = ("anthropic", "claude", "hermes", "openai_compat", "claude_cli")

#: Registry names whose configuration block may live under another name.
#: "claude" predates the documented config surface — the spec, the install
#: guide, and .env.example have only ever said ``[provider.anthropic]`` /
#: ``ORCHESTRATORPRO__PROVIDER__ANTHROPIC__*`` — so "anthropic" is canonical
#: and "claude" is kept working as an alias in both directions.
_CONFIG_ALIASES = {"claude": "anthropic", "anthropic": "claude"}


def settings_for(
    config: OrchestratorConfig, name: str, role: str = "default"
) -> ProviderConfig:
    """Resolve the effective settings for a *registry* provider name.

    This is the one place the registry-name → config-block mapping lives.
    Before it existed, the Anthropic factory read the ``anthropic`` block while
    the registry bound the name ``claude``, so a configured model and effort
    could silently fail to reach the runtime — the exact "silent degradation"
    failure ``docs/030_PROVIDER_INTERFACE.md`` §4 warns about.

    Args:
        config: The resolved configuration.
        name: The name the provider is registered (and bound) under.
        role: The role whose overrides apply.

    Returns:
        The provider settings, read from the block named ``name``, or from its
        alias when ``name`` has no block of its own.
    """
    key = name if name in config.providers else _CONFIG_ALIASES.get(name, name)
    return config.provider_for(role, provider=key)

#: Builds a provider instance. Given the resolved configuration and the role
#: being served, so one factory can serve several roles at different settings.
ProviderFactory = Callable[[OrchestratorConfig, str], Provider]

_DEFAULT_ROLE = "default"


@dataclass(frozen=True, slots=True)
class _Registration:
    """One registered factory."""

    name: str
    domain: Domain
    factory: ProviderFactory


@dataclass(slots=True)
class ProviderRegistry:
    """Resolves and owns provider instances for one configuration."""

    config: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    _factories: dict[tuple[Domain, str], _Registration] = field(default_factory=dict)
    _bindings: dict[Domain, str] = field(default_factory=dict)
    _instances: dict[tuple[Domain, str], Provider] = field(default_factory=dict)

    # --------------------------------------------------------- registration

    def register(self, name: str, domain: Domain, factory: ProviderFactory) -> None:
        """Register a factory under ``(domain, name)``.

        Args:
            name: The provider's configuration name, e.g. ``"claude"``.
            domain: Which port it satisfies.
            factory: Builds an instance for a given config and role.

        Raises:
            ProviderError: With :attr:`ErrorCode.CONFLICT` if already registered.
        """
        key = (domain, name)
        if key in self._factories:
            raise ProviderError(
                ErrorCode.CONFLICT,
                f"a provider named {name!r} is already registered for domain "
                f"{domain.value!r}",
                provider_id=f"{domain.value}.{name}",
                domain=domain,
            )
        self._factories[key] = _Registration(name=name, domain=domain, factory=factory)

    def bind(self, domain: Domain, name: str) -> None:
        """Choose which registered provider serves a domain.

        Raises:
            ProviderError: With :attr:`ErrorCode.INVALID_REQUEST` if unregistered.
        """
        if (domain, name) not in self._factories:
            raise ProviderError(
                ErrorCode.INVALID_REQUEST,
                f"cannot bind domain {domain.value!r} to unregistered provider "
                f"{name!r}; known: {sorted(self.registered(domain)) or ['(none)']}",
                domain=domain,
            )
        self._bindings[domain] = name

    def registered(self, domain: Domain) -> tuple[str, ...]:
        """Return every provider name registered for a domain."""
        return tuple(
            sorted(name for (d, name) in self._factories if d == domain)
        )

    def bound(self, domain: Domain) -> str | None:
        """Return the provider name currently bound to a domain, if any."""
        return self._bindings.get(domain)

    # ------------------------------------------------------------ resolution

    def get(self, domain: Domain, role: str = _DEFAULT_ROLE) -> Provider:
        """Return the provider serving ``role`` in ``domain``.

        Instances are cached per ``(domain, role)``: the same role always gets
        the same instance, and different roles stay isolated.

        Raises:
            ProviderError: With :attr:`ErrorCode.INVALID_REQUEST` if the domain
                has no binding, or with :attr:`ErrorCode.INTERNAL` if a factory
                returns something that is not a provider.
        """
        cached = self._instances.get((domain, role))
        if cached is not None:
            return cached

        name = self._bindings.get(domain)
        if name is None:
            raise ProviderError(
                ErrorCode.INVALID_REQUEST,
                f"no provider is bound for domain {domain.value!r}; "
                f"registered: {sorted(self.registered(domain)) or ['(none)']}",
                domain=domain,
            )

        instance = self._factories[(domain, name)].factory(self.config, role)
        if not isinstance(instance, Provider):
            raise ProviderError(
                ErrorCode.INTERNAL,
                f"the factory for {domain.value}.{name} returned "
                f"{type(instance).__name__}, which does not satisfy the provider "
                "protocol",
                domain=domain,
            )
        self._instances[(domain, role)] = instance
        return instance

    def instances(self) -> tuple[Provider, ...]:
        """Return every provider instantiated so far."""
        return tuple(self._instances.values())

    # ------------------------------------------------------------- lifecycle

    async def startup_all(self, ctx: ProviderContext | None = None) -> tuple[HealthReport, ...]:
        """Start every instantiated provider and collect its health.

        Raises:
            ProviderError: Propagated from any provider that cannot start. A run
                must not begin with a backend that was never reachable.
        """
        reports: list[HealthReport] = []
        for provider in self._instances.values():
            await provider.startup(ctx)
            reports.append(await provider.health())
        return tuple(reports)

    async def health_all(self) -> tuple[HealthReport, ...]:
        """Collect health from every instantiated provider."""
        return tuple([await provider.health() for provider in self._instances.values()])

    async def shutdown_all(self) -> None:
        """Shut every instantiated provider down.

        Every provider is given the chance to shut down even if an earlier one
        raised; the first failure is re-raised afterwards so it is not lost.
        """
        first: BaseException | None = None
        for provider in self._instances.values():
            try:
                await provider.shutdown()
            except BaseException as exc:  # noqa: BLE001 - one failure must not
                first = first or exc  # strand the remaining providers
        self._instances.clear()
        if first is not None:
            raise first


def build_default_registry(
    config: OrchestratorConfig | None = None,
    *,
    transports: Mapping[str, object] | None = None,
) -> ProviderRegistry:
    """Build a registry with the built-in model providers registered.

    The model domain is bound to whichever provider the configuration names,
    defaulting to ``claude``. No provider is instantiated here — that happens on
    first :meth:`ProviderRegistry.get`, so registering a provider costs nothing
    until something asks for it.

    Args:
        config: The resolved configuration. Defaults apply when omitted.
        transports: Optional per-provider transports, keyed by provider name.
            Supplying them keeps construction entirely offline, which is how the
            test suite runs.

    Returns:
        A registry with ``anthropic`` (alias ``claude``), ``hermes``,
        ``openai_compat``, and ``claude_cli`` registered, and the model domain
        bound.
    """
    from orchestrator.provider.claude import ClaudeConfig, ClaudeProvider
    from orchestrator.provider.hermes import HermesConfig, HermesProvider
    from orchestrator.provider.openai_compat import (
        OpenAICompatConfig,
        OpenAICompatProvider,
    )

    resolved = config or OrchestratorConfig()
    supplied = dict(transports or {})
    registry = ProviderRegistry(config=resolved)

    def transport_for(name: str) -> Any:
        """An injected transport under the given name, or its alias's."""
        alias = _CONFIG_ALIASES.get(name, name)
        return supplied.get(name, supplied.get(alias))

    def anthropic_factory_named(name: str) -> ProviderFactory:
        """The Anthropic-backed factory, reading the block matching ``name``.

        A factory per registered name, so the bound name and the config block
        it reads can never drift apart again.
        """

        def factory(cfg: OrchestratorConfig, role: str) -> Provider:
            settings = settings_for(cfg, name, role)
            return ClaudeProvider(
                ClaudeConfig(model=settings.model),
                transport=transport_for(name),
            )

        return factory

    def hermes_factory(cfg: OrchestratorConfig, role: str) -> Provider:
        settings = settings_for(cfg, "hermes", role)
        return HermesProvider(
            HermesConfig(model=settings.model),
            transport=transport_for("hermes"),
        )

    def openai_factory(cfg: OrchestratorConfig, role: str) -> Provider:
        settings = settings_for(cfg, "openai_compat", role)
        return OpenAICompatProvider(
            OpenAICompatConfig(model=settings.model),
            transport=transport_for("openai_compat"),
        )

    def claude_cli_factory(cfg: OrchestratorConfig, role: str) -> Provider:
        from orchestrator.provider.claude_cli import ClaudeCliConfig, ClaudeCliProvider

        settings = settings_for(cfg, "claude_cli", role)
        return ClaudeCliProvider(
            ClaudeCliConfig(
                executable=settings.executable or "claude",
                timeout_s=settings.timeout_s,
                # Deliberately NOT settings.model: the block's default is the
                # API provider's model id, and forwarding it would silently
                # override the model the operator configured in the CLI
                # itself. --model is opt-in via direct construction only,
                # until config can distinguish "set" from "defaulted".
                model="",
            ),
            runner=transport_for("claude_cli"),
        )

    registry.register("anthropic", Domain.MODEL, anthropic_factory_named("anthropic"))
    registry.register("claude", Domain.MODEL, anthropic_factory_named("claude"))
    registry.register("hermes", Domain.MODEL, hermes_factory)
    registry.register("openai_compat", Domain.MODEL, openai_factory)
    registry.register("claude_cli", Domain.MODEL, claude_cli_factory)

    configured = [
        name for name in _MODEL_PREFERENCE if name in resolved.providers
    ]
    registry.bind(Domain.MODEL, configured[0] if configured else "anthropic")
    return registry
