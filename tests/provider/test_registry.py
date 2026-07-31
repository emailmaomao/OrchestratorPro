"""Tests for the provider registry — registration, binding, and lifecycle."""

from __future__ import annotations

import pytest

from pathlib import Path

from orchestrator.core.config import OrchestratorConfig, load_config
from orchestrator.provider.base import (
    CapabilitySet,
    Domain,
    ErrorCode,
    HealthReport,
    ProviderContext,
    ProviderError,
)
from orchestrator.provider.claude import ClaudeProvider
from orchestrator.provider.hermes import HermesProvider
from orchestrator.provider.openai_compat import OpenAICompatProvider
from orchestrator.provider.registry import (
    ProviderRegistry,
    build_default_registry,
    settings_for,
)

from tests.provider.conftest import FakeTransport, run


class StubProvider:
    """A minimal provider satisfying the substrate protocol."""

    domain = Domain.MODEL
    version = "1.0"

    def __init__(self, provider_id: str = "model.stub", role: str = "default") -> None:
        self.id = provider_id
        self.role = role
        self.started = 0
        self.stopped = 0

    def capabilities(self) -> CapabilitySet:
        return CapabilitySet()

    async def startup(self, ctx: ProviderContext | None = None) -> None:
        self.started += 1

    async def health(self) -> HealthReport:
        return HealthReport(provider_id=self.id, healthy=self.started > 0)

    async def shutdown(self) -> None:
        self.stopped += 1


class ExplodingProvider(StubProvider):
    """A provider whose shutdown fails."""

    async def shutdown(self) -> None:
        self.stopped += 1
        raise ProviderError(ErrorCode.INTERNAL, "shutdown failed")


@pytest.fixture
def registry() -> ProviderRegistry:
    """An empty registry over default configuration."""
    return ProviderRegistry(config=OrchestratorConfig())


class TestRegistration:
    """Registration is explicit; nothing is discovered by import."""

    def test_register_then_bind_then_get(self, registry: ProviderRegistry) -> None:
        registry.register("stub", Domain.MODEL, lambda cfg, role: StubProvider())
        registry.bind(Domain.MODEL, "stub")
        assert isinstance(registry.get(Domain.MODEL), StubProvider)

    def test_registering_twice_is_a_conflict(self, registry: ProviderRegistry) -> None:
        registry.register("stub", Domain.MODEL, lambda cfg, role: StubProvider())
        with pytest.raises(ProviderError) as excinfo:
            registry.register("stub", Domain.MODEL, lambda cfg, role: StubProvider())
        assert excinfo.value.error_code is ErrorCode.CONFLICT

    def test_the_same_name_may_serve_different_domains(
        self, registry: ProviderRegistry
    ) -> None:
        registry.register("local", Domain.MODEL, lambda cfg, role: StubProvider())
        registry.register("local", Domain.SHELL, lambda cfg, role: StubProvider())
        assert registry.registered(Domain.MODEL) == ("local",)
        assert registry.registered(Domain.SHELL) == ("local",)

    def test_binding_an_unregistered_name_is_refused(
        self, registry: ProviderRegistry
    ) -> None:
        with pytest.raises(ProviderError) as excinfo:
            registry.bind(Domain.MODEL, "nope")
        assert excinfo.value.error_code is ErrorCode.INVALID_REQUEST
        assert "unregistered" in str(excinfo.value)

    def test_getting_an_unbound_domain_is_refused(
        self, registry: ProviderRegistry
    ) -> None:
        registry.register("stub", Domain.MODEL, lambda cfg, role: StubProvider())
        with pytest.raises(ProviderError) as excinfo:
            registry.get(Domain.MODEL)
        assert excinfo.value.error_code is ErrorCode.INVALID_REQUEST
        assert "no provider is bound" in str(excinfo.value)

    def test_registered_lists_names_for_the_domain(
        self, registry: ProviderRegistry
    ) -> None:
        registry.register("b", Domain.MODEL, lambda cfg, role: StubProvider())
        registry.register("a", Domain.MODEL, lambda cfg, role: StubProvider())
        assert registry.registered(Domain.MODEL) == ("a", "b")
        assert registry.registered(Domain.VCS) == ()

    def test_bound_reports_the_current_binding(self, registry: ProviderRegistry) -> None:
        assert registry.bound(Domain.MODEL) is None
        registry.register("stub", Domain.MODEL, lambda cfg, role: StubProvider())
        registry.bind(Domain.MODEL, "stub")
        assert registry.bound(Domain.MODEL) == "stub"

    def test_a_factory_returning_a_non_provider_is_caught(
        self, registry: ProviderRegistry
    ) -> None:
        registry.register("bad", Domain.MODEL, lambda cfg, role: object())  # type: ignore[arg-type,return-value]
        registry.bind(Domain.MODEL, "bad")
        with pytest.raises(ProviderError) as excinfo:
            registry.get(Domain.MODEL)
        assert excinfo.value.error_code is ErrorCode.INTERNAL


class TestRoleResolution:
    """Roles let one domain serve several configured instances."""

    def test_instances_are_cached_per_role(self, registry: ProviderRegistry) -> None:
        registry.register(
            "stub", Domain.MODEL, lambda cfg, role: StubProvider(role=role)
        )
        registry.bind(Domain.MODEL, "stub")

        first = registry.get(Domain.MODEL, "worker")
        assert registry.get(Domain.MODEL, "worker") is first

    def test_different_roles_get_different_instances(
        self, registry: ProviderRegistry
    ) -> None:
        registry.register(
            "stub", Domain.MODEL, lambda cfg, role: StubProvider(role=role)
        )
        registry.bind(Domain.MODEL, "stub")

        worker = registry.get(Domain.MODEL, "worker")
        planner = registry.get(Domain.MODEL, "planner")
        assert worker is not planner
        assert worker.role == "worker"  # type: ignore[attr-defined]
        assert planner.role == "planner"  # type: ignore[attr-defined]

    def test_the_role_reaches_the_factory(self, registry: ProviderRegistry) -> None:
        seen: list[str] = []

        def factory(cfg: OrchestratorConfig, role: str) -> StubProvider:
            seen.append(role)
            return StubProvider(role=role)

        registry.register("stub", Domain.MODEL, factory)
        registry.bind(Domain.MODEL, "stub")
        registry.get(Domain.MODEL, "summarizer")
        assert seen == ["summarizer"]

    def test_role_settings_come_from_configuration(self) -> None:
        config = OrchestratorConfig.from_mapping(
            {
                "provider": {
                    "anthropic": {"model": "base-model"},
                    "roles": {"summarizer": {"model": "cheap-model"}},
                }
            },
            env={},
        )
        assert config.provider_for("summarizer").model == "cheap-model"
        assert config.provider_for("worker").model == "base-model"


class TestLifecycle:
    """Startup happens across the whole set, before any task begins."""

    def test_startup_all_starts_every_instance(self, registry: ProviderRegistry) -> None:
        registry.register("stub", Domain.MODEL, lambda cfg, role: StubProvider())
        registry.bind(Domain.MODEL, "stub")
        first = registry.get(Domain.MODEL, "worker")
        second = registry.get(Domain.MODEL, "planner")

        reports = run(registry.startup_all())
        assert len(reports) == 2
        assert all(report.healthy for report in reports)
        assert first.started == 1  # type: ignore[attr-defined]
        assert second.started == 1  # type: ignore[attr-defined]

    def test_startup_failure_propagates(self, registry: ProviderRegistry) -> None:
        """A run must not begin with a backend that was never reachable."""

        class Unstartable(StubProvider):
            async def startup(self, ctx: ProviderContext | None = None) -> None:
                raise ProviderError(ErrorCode.UNAVAILABLE, "endpoint down")

        registry.register("bad", Domain.MODEL, lambda cfg, role: Unstartable())
        registry.bind(Domain.MODEL, "bad")
        registry.get(Domain.MODEL)

        with pytest.raises(ProviderError) as excinfo:
            run(registry.startup_all())
        assert excinfo.value.error_code is ErrorCode.UNAVAILABLE

    def test_health_all_reports_every_instance(self, registry: ProviderRegistry) -> None:
        registry.register("stub", Domain.MODEL, lambda cfg, role: StubProvider())
        registry.bind(Domain.MODEL, "stub")
        registry.get(Domain.MODEL)
        assert len(run(registry.health_all())) == 1

    def test_shutdown_all_clears_instances(self, registry: ProviderRegistry) -> None:
        registry.register("stub", Domain.MODEL, lambda cfg, role: StubProvider())
        registry.bind(Domain.MODEL, "stub")
        instance = registry.get(Domain.MODEL)

        run(registry.shutdown_all())
        assert instance.stopped == 1  # type: ignore[attr-defined]
        assert registry.instances() == ()

    def test_one_failing_shutdown_does_not_strand_the_others(
        self, registry: ProviderRegistry
    ) -> None:
        survivors: list[StubProvider] = []

        def factory(cfg: OrchestratorConfig, role: str) -> StubProvider:
            provider = ExplodingProvider() if role == "bad" else StubProvider()
            survivors.append(provider)
            return provider

        registry.register("stub", Domain.MODEL, factory)
        registry.bind(Domain.MODEL, "stub")
        registry.get(Domain.MODEL, "bad")
        registry.get(Domain.MODEL, "good")

        with pytest.raises(ProviderError):
            run(registry.shutdown_all())
        # Every provider was given the chance to shut down.
        assert all(provider.stopped == 1 for provider in survivors)


class TestDefaultRegistry:
    """The built-in registry wires the three model providers."""

    def test_every_builtin_name_is_registered(self) -> None:
        registry = build_default_registry()
        assert set(registry.registered(Domain.MODEL)) == {
            "anthropic",
            "claude",
            "claude_cli",
            "hermes",
            "openai_compat",
        }

    def test_anthropic_is_the_default_binding(self) -> None:
        """The canonical key matches the documented config surface.

        The spec, the install guide, and .env.example have only ever said
        ``[provider.anthropic]``; the registry once said ``claude``, and the
        mismatch let configured settings silently miss the runtime.
        """
        assert build_default_registry().bound(Domain.MODEL) == "anthropic"

    def test_configuration_selects_the_provider(self) -> None:
        config = OrchestratorConfig.from_mapping(
            {"provider": {"hermes": {"model": "hermes-4"}}}, env={}
        )
        assert build_default_registry(config).bound(Domain.MODEL) == "hermes"

    def test_a_configured_anthropic_block_reaches_the_provider(self) -> None:
        """The regression this rename exists to prevent: settings must arrive."""
        config = OrchestratorConfig.from_mapping(
            {"provider": {"anthropic": {"model": "configured-model"}}}, env={}
        )
        registry = build_default_registry(config, transports={"anthropic": FakeTransport()})

        assert registry.bound(Domain.MODEL) == "anthropic"
        provider = registry.get(Domain.MODEL)
        assert isinstance(provider, ClaudeProvider)
        assert provider.config.model == "configured-model"
        assert settings_for(config, "anthropic").model == "configured-model"

    def test_a_legacy_claude_block_still_works(self) -> None:
        """``[provider.claude]`` predates the docs and must keep working."""
        config = OrchestratorConfig.from_mapping(
            {"provider": {"claude": {"model": "legacy-model"}}}, env={}
        )
        registry = build_default_registry(config, transports={"claude": FakeTransport()})

        assert registry.bound(Domain.MODEL) == "claude"
        provider = registry.get(Domain.MODEL)
        assert isinstance(provider, ClaudeProvider)
        assert provider.config.model == "legacy-model"
        assert settings_for(config, "claude").model == "legacy-model"

    def test_the_env_layer_reaches_the_provider(self) -> None:
        """ORCHESTRATORPRO__PROVIDER__ANTHROPIC__MODEL must change the model.

        This walks the exact path a deployment uses: environment override →
        config → registry → provider instance, with no file involved.
        """
        config = load_config(
            user_config=Path("does-not-exist/config.toml"),
            env={"ORCHESTRATORPRO__PROVIDER__ANTHROPIC__MODEL": "env-model"},
        )
        registry = build_default_registry(config, transports={"anthropic": FakeTransport()})

        assert registry.bound(Domain.MODEL) == "anthropic"
        provider = registry.get(Domain.MODEL)
        assert isinstance(provider, ClaudeProvider)
        assert provider.config.model == "env-model"
        assert settings_for(config, registry.bound(Domain.MODEL) or "").model == "env-model"

    def test_settings_for_falls_back_across_the_alias(self) -> None:
        """A name with no block of its own reads its alias's block."""
        config = OrchestratorConfig.from_mapping(
            {"provider": {"anthropic": {"model": "canonical"}}}, env={}
        )
        assert settings_for(config, "claude").model == "canonical"

    def test_an_injected_claude_transport_serves_the_anthropic_binding(self) -> None:
        """Transports injected under the legacy key keep working."""
        registry = build_default_registry(transports={"claude": FakeTransport()})
        assert registry.bound(Domain.MODEL) == "anthropic"
        provider = registry.get(Domain.MODEL)
        run(registry.startup_all())
        assert run(provider.health()).healthy

    def test_transports_can_be_injected_for_offline_use(self) -> None:
        """The whole layer must be usable with no network at all."""
        transport = FakeTransport(
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )
        registry = build_default_registry(transports={"claude": transport})
        provider = registry.get(Domain.MODEL)
        run(registry.startup_all())
        assert run(provider.health()).healthy

    def test_each_provider_type_resolves(self) -> None:
        for name, expected in (
            ("claude", ClaudeProvider),
            ("hermes", HermesProvider),
            ("openai_compat", OpenAICompatProvider),
        ):
            registry = build_default_registry(
                transports={name: FakeTransport()},
            )
            registry.bind(Domain.MODEL, name)
            assert isinstance(registry.get(Domain.MODEL), expected)

    def test_nothing_is_instantiated_until_requested(self) -> None:
        """Registering a provider costs nothing until something asks for it."""
        assert build_default_registry().instances() == ()


def test_no_module_above_the_provider_layer_names_a_vendor() -> None:
    """docs/030 §0: the rule the whole layer exists to enforce."""
    import ast
    from pathlib import Path

    import orchestrator

    root = Path(orchestrator.__path__[0])
    vendor_terms = ("anthropic", "openai", "ollama")
    offenders: list[str] = []

    for path in root.rglob("*.py"):
        if "provider" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(term in name.lower() for term in vendor_terms):
                    offenders.append(f"{path.name}: {name}")

    assert offenders == [], f"vendor imports outside the provider layer: {offenders}"
