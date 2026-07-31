"""Tests for layered configuration loading and its security invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.core.config import (
    ApiConfig,
    Effort,
    OrchestratorConfig,
    ProviderConfig,
    ThinkingMode,
    load_config,
)
from orchestrator.core.events import ConfigError


@pytest.fixture
def absent_user_config(tmp_dir: Path) -> Path:
    """A path that does not exist, isolating tests from any real user config."""
    return tmp_dir / "no-such-user-config.toml"


def write(path: Path, body: str) -> Path:
    """Write ``body`` to ``path``, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Defaults and layering
# --------------------------------------------------------------------------- #


class TestLayering:
    """Repository config overrides user config, which overrides defaults."""

    def test_defaults_apply_with_no_files(self, absent_user_config: Path) -> None:
        config = load_config(user_config=absent_user_config, env={})
        assert config.run.max_concurrency == 4
        assert config.run.auto_approve is False
        assert config.agent.adapter == "tool_loop"
        assert config.git.integration_branch == "orchestrator/integration"
        assert config.api.host == "127.0.0.1"
        assert config.sources == ()

    def test_repository_layer_is_read(self, tmp_dir: Path, absent_user_config: Path) -> None:
        write(tmp_dir / "orchestrator.toml", "[run]\nmax_concurrency = 9\n")
        config = load_config(tmp_dir, user_config=absent_user_config, env={})
        assert config.run.max_concurrency == 9
        assert config.sources == (tmp_dir / "orchestrator.toml",)

    def test_repository_overrides_user(self, tmp_dir: Path) -> None:
        user = write(tmp_dir / "user" / "config.toml", "[run]\nmax_concurrency = 2\n")
        repo = tmp_dir / "repo"
        write(repo / "orchestrator.toml", "[run]\nmax_concurrency = 7\n")

        config = load_config(repo, user_config=user, env={})
        assert config.run.max_concurrency == 7
        assert config.sources == (user, repo / "orchestrator.toml")

    def test_merge_is_per_key_not_per_section(self, tmp_dir: Path) -> None:
        user = write(
            tmp_dir / "user" / "config.toml",
            "[run]\nmax_concurrency = 2\nauto_approve = true\n",
        )
        repo = tmp_dir / "repo"
        write(repo / "orchestrator.toml", "[run]\nmax_concurrency = 5\n")

        config = load_config(repo, user_config=user, env={})
        assert config.run.max_concurrency == 5
        # auto_approve came from the user layer and must survive the overlay.
        assert config.run.auto_approve is True

    def test_missing_repository_file_is_not_an_error(
        self, tmp_dir: Path, absent_user_config: Path
    ) -> None:
        config = load_config(tmp_dir, user_config=absent_user_config, env={})
        assert config.run.max_concurrency == 4


# --------------------------------------------------------------------------- #
# Security invariants
# --------------------------------------------------------------------------- #


class TestSecurityInvariants:
    """NFR-3.4 and NFR-3.5 are enforced at load time, not at use time."""

    @pytest.mark.parametrize(
        "body",
        [
            '[provider.anthropic]\napi_key = "sk-secret-value"\n',
            '[provider.anthropic]\nsecret = "x"\n',
            '[api]\nauth_token = "abc"\n',
            '[git]\npassword = "hunter2"\n',
            '[provider.anthropic]\nACCESS_KEY = "x"\n',
        ],
    )
    def test_credential_shaped_keys_are_rejected(
        self, tmp_dir: Path, absent_user_config: Path, body: str
    ) -> None:
        write(tmp_dir / "orchestrator.toml", body)
        with pytest.raises(ConfigError, match="credential"):
            load_config(tmp_dir, user_config=absent_user_config, env={})

    def test_non_local_bind_requires_a_token_env_var(self) -> None:
        with pytest.raises(ConfigError, match="loopback"):
            OrchestratorConfig.from_mapping({"api": {"host": "0.0.0.0"}}, env={})

    def test_non_local_bind_requires_the_variable_to_be_set(self) -> None:
        data = {"api": {"host": "0.0.0.0", "auth_token_env": "OP_TOKEN"}}
        with pytest.raises(ConfigError, match="unset or empty"):
            OrchestratorConfig.from_mapping(data, env={})

    def test_non_local_bind_succeeds_when_the_variable_is_present(self) -> None:
        data = {"api": {"host": "0.0.0.0", "auth_token_env": "OP_TOKEN"}}
        config = OrchestratorConfig.from_mapping(data, env={"OP_TOKEN": "s3cret"})
        assert config.api.host == "0.0.0.0"
        assert config.api.auth_token_env == "OP_TOKEN"

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
    def test_loopback_binds_need_no_token(self, host: str) -> None:
        config = OrchestratorConfig.from_mapping({"api": {"host": host}}, env={})
        assert config.api.is_local is True

    def test_the_token_itself_is_never_stored_in_config(self) -> None:
        assert not hasattr(ApiConfig(), "auth_token")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


class TestValidation:
    """Bad configuration fails at startup with a located, actionable message."""

    def test_unknown_top_level_section_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="unknown top-level section"):
            OrchestratorConfig.from_mapping({"nonsense": {}}, env={})

    def test_unknown_key_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="max_concurency"):
            OrchestratorConfig.from_mapping(
                {"run": {"max_concurency": 4}}, env={}
            )

    def test_wrong_type_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="must be int"):
            OrchestratorConfig.from_mapping({"run": {"max_concurrency": "four"}}, env={})

    def test_bool_is_not_accepted_as_int(self) -> None:
        with pytest.raises(ConfigError, match="got bool"):
            OrchestratorConfig.from_mapping({"run": {"max_concurrency": True}}, env={})

    def test_concurrency_must_be_at_least_one(self) -> None:
        with pytest.raises(ConfigError, match="at least 1"):
            OrchestratorConfig.from_mapping({"run": {"max_concurrency": 0}}, env={})

    def test_invalid_effort_lists_the_allowed_values(self) -> None:
        with pytest.raises(ConfigError, match="xhigh"):
            OrchestratorConfig.from_mapping(
                {"provider": {"anthropic": {"effort": "turbo"}}}, env={}
            )

    def test_port_range_is_enforced(self) -> None:
        with pytest.raises(ConfigError, match="between 1 and 65535"):
            OrchestratorConfig.from_mapping({"api": {"port": 70_000}}, env={})

    def test_malformed_toml_is_reported_with_its_path(
        self, tmp_dir: Path, absent_user_config: Path
    ) -> None:
        write(tmp_dir / "orchestrator.toml", "[run\nmax_concurrency = 1\n")
        with pytest.raises(ConfigError, match="not valid TOML"):
            load_config(tmp_dir, user_config=absent_user_config, env={})

    @pytest.mark.parametrize(
        "section",
        [
            {"agent": {"budget_seconds": 0}},
            {"agent": {"budget_tokens": 0}},
            {"agent": {"budget_tool_calls": -1}},
            {"gates": {"timeout_s": 0}},
        ],
    )
    def test_non_positive_limits_are_rejected(self, section: dict[str, object]) -> None:
        with pytest.raises(ConfigError, match="must be positive"):
            OrchestratorConfig.from_mapping(section, env={})


class TestGateEnvironment:
    """`[gates] env` — the variables a project's suite needs to run at all.

    Without it, a suite that needs (say) PYTHONPATH reports a red suite that
    is really a misconfigured harness: the failed-versus-errored confusion
    this codebase refuses everywhere else, arriving through the back door.
    """

    def test_a_table_is_parsed(self) -> None:
        """The ordinary TOML inline-table form."""
        config = OrchestratorConfig.from_mapping(
            {"gates": {"env": {"PYTHONPATH": "src", "QT_QPA_PLATFORM": "offscreen"}}},
            env={},
        )
        assert config.gates.env == {"PYTHONPATH": "src", "QT_QPA_PLATFORM": "offscreen"}

    def test_a_comma_separated_string_is_parsed(self) -> None:
        """An environment-only deployment cannot express a TOML table."""
        config = OrchestratorConfig.from_mapping(
            {"gates": {"env": "PYTHONPATH=src, QT_QPA_PLATFORM=offscreen"}}, env={}
        )
        assert config.gates.env == {"PYTHONPATH": "src", "QT_QPA_PLATFORM": "offscreen"}

    def test_it_defaults_to_empty(self) -> None:
        """No configuration means no additions to the inherited environment."""
        assert OrchestratorConfig.from_mapping({}, env={}).gates.env == {}

    def test_a_malformed_entry_is_refused(self) -> None:
        """A typo must fail at load, not produce a silently empty variable."""
        with pytest.raises(ConfigError, match="KEY=VALUE"):
            OrchestratorConfig.from_mapping({"gates": {"env": "PYTHONPATH"}}, env={})

    def test_a_non_string_table_is_refused(self) -> None:
        """Environment values are strings; an int would surprise the child."""
        with pytest.raises(ConfigError, match="table of strings"):
            OrchestratorConfig.from_mapping({"gates": {"env": {"N": 1}}}, env={})


# --------------------------------------------------------------------------- #
# Provider settings
# --------------------------------------------------------------------------- #


class TestProviderConfig:
    """Provider settings encode constraints that would otherwise fail mid-run."""

    def test_defaults_match_the_specification(self) -> None:
        provider = ProviderConfig()
        assert provider.model == "claude-opus-5"
        assert provider.effort is Effort.XHIGH
        assert provider.thinking is ThinkingMode.ADAPTIVE

    @pytest.mark.parametrize("effort", [Effort.XHIGH, Effort.MAX])
    def test_disabled_thinking_conflicts_with_deep_effort(self, effort: Effort) -> None:
        with pytest.raises(ConfigError, match="incompatible"):
            ProviderConfig(effort=effort, thinking=ThinkingMode.DISABLED)

    @pytest.mark.parametrize("effort", [Effort.LOW, Effort.MEDIUM, Effort.HIGH])
    def test_disabled_thinking_is_allowed_at_moderate_effort(self, effort: Effort) -> None:
        provider = ProviderConfig(effort=effort, thinking=ThinkingMode.DISABLED)
        assert provider.thinking is ThinkingMode.DISABLED

    def test_conflict_is_caught_when_loading_a_file(
        self, tmp_dir: Path, absent_user_config: Path
    ) -> None:
        write(
            tmp_dir / "orchestrator.toml",
            '[provider.anthropic]\neffort = "max"\nthinking = "disabled"\n',
        )
        with pytest.raises(ConfigError, match="incompatible"):
            load_config(tmp_dir, user_config=absent_user_config, env={})

    def test_role_overrides_apply_over_the_provider_block(self) -> None:
        data = {
            "provider": {
                "anthropic": {"model": "base-model", "effort": "high"},
                "roles": {"summarizer": {"model": "cheap-model", "effort": "low"}},
            }
        }
        config = OrchestratorConfig.from_mapping(data, env={})

        summarizer = config.provider_for("summarizer")
        assert summarizer.model == "cheap-model"
        assert summarizer.effort is Effort.LOW

        worker = config.provider_for("worker")
        assert worker.model == "base-model"
        assert worker.effort is Effort.HIGH

    def test_partial_role_override_inherits_the_rest(self) -> None:
        data = {
            "provider": {
                "anthropic": {"model": "base-model", "effort": "high"},
                "roles": {"planner": {"effort": "max"}},
            }
        }
        planner = OrchestratorConfig.from_mapping(data, env={}).provider_for("planner")
        assert planner.model == "base-model"
        assert planner.effort is Effort.MAX

    def test_unknown_role_falls_back_to_the_provider_block(self) -> None:
        data = {"provider": {"anthropic": {"model": "base-model"}}}
        config = OrchestratorConfig.from_mapping(data, env={})
        assert config.provider_for("nonexistent").model == "base-model"

    def test_absent_provider_block_falls_back_to_defaults(self) -> None:
        config = OrchestratorConfig.from_mapping({}, env={})
        assert config.provider_for("worker").model == "claude-opus-5"


# --------------------------------------------------------------------------- #
# Budget bridge
# --------------------------------------------------------------------------- #


def test_agent_config_renders_a_budget() -> None:
    """The agent's configured limits become a per-attempt Budget."""
    config = OrchestratorConfig.from_mapping(
        {"agent": {"budget_seconds": 60, "budget_tokens": 500, "budget_tool_calls": 5}},
        env={},
    )
    budget = config.agent.to_budget()
    assert budget.seconds == 60.0
    assert budget.tokens == 500
    assert budget.tool_calls == 5


def test_the_agent_model_defaults_to_sonnet() -> None:
    """A named default, so the engine's behaviour does not depend on the CLI's."""
    config = OrchestratorConfig.from_mapping({}, env={})
    assert config.agent.model == "sonnet"


def test_the_agent_model_can_be_set_in_config() -> None:
    """Per-run override for a genuinely hard step."""
    config = OrchestratorConfig.from_mapping({"agent": {"model": "opus"}}, env={})
    assert config.agent.model == "opus"


def test_the_agent_model_can_be_set_from_the_environment() -> None:
    """The one-line override, without editing a file.

    `from_mapping(env=...)` resolves token references, not `ORCHESTRATORPRO__*`
    overlays — those come through `env_overlay`, which is the seam the loader
    and the container both use. Testing the documented path, not a nearby one.
    """
    from orchestrator.core.config import env_overlay

    overlay = env_overlay({"ORCHESTRATORPRO__AGENT__MODEL": "opus"})
    assert overlay == {"agent": {"model": "opus"}}
    assert OrchestratorConfig.from_mapping(overlay, env={}).agent.model == "opus"


def test_an_empty_agent_model_is_allowed() -> None:
    """Empty means "pass no --model": the pre-2026-07-28 inherit behaviour.

    Deliberately not rejected. It is a meaningful setting, not a missing one.
    """
    config = OrchestratorConfig.from_mapping({"agent": {"model": ""}}, env={})
    assert config.agent.model == ""
