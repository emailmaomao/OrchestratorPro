"""Tests for configuration migration and the environment layer."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from orchestrator.core.config import (
    ConfigError,
    OrchestratorConfig,
    env_overlay,
    load_config,
)
from orchestrator.ops.migrate import (
    CONFIG_VERSION,
    VERSION_KEY,
    MigrationError,
    migrate_config,
    migrate_file,
    needs_migration,
    render_toml,
    steps_between,
    version_of,
)


class TestVersionDetection:
    """Which shape a file was written for."""

    def test_an_unmarked_file_is_version_one(self) -> None:
        """Every file written before migration existed."""
        assert version_of({"run": {"max_concurrency": 2}}) == 1

    def test_a_marked_file_reports_its_version(self) -> None:
        assert version_of({VERSION_KEY: 2}) == 2

    def test_a_non_integer_version_is_refused(self) -> None:
        with pytest.raises(MigrationError, match="must be an integer"):
            version_of({VERSION_KEY: "two"})

    def test_a_boolean_version_is_refused(self) -> None:
        with pytest.raises(MigrationError, match="must be an integer"):
            version_of({VERSION_KEY: True})

    def test_a_zero_version_is_refused(self) -> None:
        with pytest.raises(MigrationError, match="at least 1"):
            version_of({VERSION_KEY: 0})

    def test_an_old_file_needs_migration(self) -> None:
        assert needs_migration({}) is True
        assert needs_migration({VERSION_KEY: CONFIG_VERSION}) is False


class TestSteps:
    """The chain between versions."""

    def test_a_chain_is_found(self) -> None:
        chain = steps_between(1, 2)

        assert len(chain) == 1
        assert chain[0].from_version == 1

    def test_no_steps_are_needed_to_stay_put(self) -> None:
        assert steps_between(2, 2) == ()

    def test_an_unreachable_target_is_refused(self) -> None:
        """Better to say so than to leave a file half-migrated."""
        with pytest.raises(MigrationError, match="no migration step"):
            steps_between(1, 99)

    def test_every_step_declares_what_it_does(self) -> None:
        from orchestrator.ops.migrate import STEPS

        for step in STEPS:
            assert step.summary
            assert step.to_version > step.from_version


class TestMigrateConfig:
    """The transformation itself."""

    def test_an_old_file_is_brought_up_to_date(self) -> None:
        result = migrate_config({"run": {"max_concurrency": 2}})

        assert result.from_version == 1
        assert result.to_version == CONFIG_VERSION
        assert result.changed
        assert result.config[VERSION_KEY] == CONFIG_VERSION

    def test_the_original_is_not_mutated(self) -> None:
        """Migration is a pure function, so an upgrade can be previewed."""
        original = {"run": {"max_concurrency": 2}}
        migrate_config(original)

        assert VERSION_KEY not in original

    def test_settings_survive(self) -> None:
        result = migrate_config({"run": {"max_concurrency": 7}})
        assert result.config["run"]["max_concurrency"] == 7

    def test_relocated_keys_are_moved_not_dropped(self) -> None:
        result = migrate_config({"api": {"host": "0.0.0.0", "allowed_hosts": ["a"]}})

        assert result.config["security"]["allowed_hosts"] == ["a"]
        assert "allowed_hosts" not in result.config["api"]
        assert result.config["api"]["host"] == "0.0.0.0"

    def test_a_relocation_is_reported(self) -> None:
        """An operator who set something should be told where it went."""
        result = migrate_config({"api": {"allowed_hosts": ["a"]}})

        assert any("moved api.allowed_hosts" in note for note in result.notes)

    def test_an_existing_security_section_wins(self) -> None:
        result = migrate_config(
            {"api": {"allowed_hosts": ["old"]}, "security": {"allowed_hosts": ["new"]}}
        )
        assert result.config["security"]["allowed_hosts"] == ["new"]

    def test_a_current_file_is_left_alone(self) -> None:
        result = migrate_config({VERSION_KEY: CONFIG_VERSION, "run": {}})

        assert not result.changed
        assert "nothing to do" in result.summary()

    def test_a_newer_file_is_refused(self) -> None:
        """Downgrading a file is not an upgrade path."""
        with pytest.raises(MigrationError, match="Upgrade OrchestratorPro"):
            migrate_config({VERSION_KEY: 99})

    def test_the_result_summarizes_itself(self) -> None:
        result = migrate_config({})
        assert "1 -> 2" in result.summary()

    def test_the_migrated_mapping_still_validates(self) -> None:
        """The point: what comes out must load."""
        result = migrate_config({"api": {"host": "127.0.0.1", "allowed_hosts": ["a"]}})
        config = OrchestratorConfig.from_mapping(result.config, env={})

        assert config.security.allowed_hosts == ("a",)
        assert config.config_version == CONFIG_VERSION

    def test_an_unmigrated_old_file_would_not_load(self) -> None:
        """Which is why the migration exists rather than a compatibility shim."""
        with pytest.raises(ConfigError, match="unknown key"):
            OrchestratorConfig.from_mapping({"api": {"allowed_hosts": ["a"]}}, env={})


class TestMigrateFile:
    """Migration on disk."""

    def _write(self, path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_file_is_previewed_without_being_written(self, tmp_dir: Path) -> None:
        path = self._write(tmp_dir / "config.toml", "[run]\nmax_concurrency = 3\n")
        before = path.read_text(encoding="utf-8")

        result = migrate_file(path)

        assert result.changed
        assert path.read_text(encoding="utf-8") == before

    def test_writing_applies_it(self, tmp_dir: Path) -> None:
        path = self._write(tmp_dir / "config.toml", "[run]\nmax_concurrency = 3\n")

        migrate_file(path, write=True)

        rewritten = tomllib.loads(path.read_text(encoding="utf-8"))
        assert rewritten[VERSION_KEY] == CONFIG_VERSION
        assert rewritten["run"]["max_concurrency"] == 3

    def test_the_original_is_kept(self, tmp_dir: Path) -> None:
        path = self._write(tmp_dir / "config.toml", "[api]\nallowed_hosts = ['a']\n")

        migrate_file(path, write=True)

        assert (tmp_dir / "config.toml.v1").is_file()

    def test_the_backup_can_be_declined(self, tmp_dir: Path) -> None:
        path = self._write(tmp_dir / "config.toml", "[run]\nmax_concurrency = 3\n")

        migrate_file(path, write=True, backup=False)

        assert not (tmp_dir / "config.toml.v1").exists()

    def test_a_current_file_is_not_rewritten(self, tmp_dir: Path) -> None:
        path = self._write(tmp_dir / "config.toml", f"{VERSION_KEY} = {CONFIG_VERSION}\n")
        before = path.read_text(encoding="utf-8")

        migrate_file(path, write=True)

        assert path.read_text(encoding="utf-8") == before

    def test_a_missing_file_is_refused(self, tmp_dir: Path) -> None:
        with pytest.raises(MigrationError, match="no configuration file"):
            migrate_file(tmp_dir / "absent.toml")

    def test_malformed_toml_is_refused(self, tmp_dir: Path) -> None:
        path = self._write(tmp_dir / "config.toml", "this is not = = toml")

        with pytest.raises(MigrationError, match="not valid TOML"):
            migrate_file(path)

    def test_a_migrated_file_loads(self, tmp_dir: Path) -> None:
        """End to end: an old file becomes one this build accepts."""
        path = self._write(
            tmp_dir / "orchestrator.toml",
            "[api]\nhost = '127.0.0.1'\nallowed_hosts = ['a.example']\n",
        )
        migrate_file(path, write=True)

        config = load_config(repo_root=tmp_dir, user_config=tmp_dir / "absent.toml", env={})
        assert config.security.allowed_hosts == ("a.example",)


class TestRenderToml:
    """The writer, since the standard library only reads."""

    def test_scalars_round_trip(self) -> None:
        rendered = render_toml({"config_version": 2, "run": {"max_concurrency": 4}})
        assert tomllib.loads(rendered) == {"config_version": 2, "run": {"max_concurrency": 4}}

    def test_booleans_are_lowercase(self) -> None:
        assert "auto_approve = true" in render_toml({"run": {"auto_approve": True}})

    def test_strings_are_quoted_and_escaped(self) -> None:
        rendered = render_toml({"gates": {"test_command": 'pytest -k "x"'}})
        assert tomllib.loads(rendered)["gates"]["test_command"] == 'pytest -k "x"'

    def test_lists_round_trip(self) -> None:
        rendered = render_toml({"security": {"allowed_hosts": ["a", "b"]}})
        assert tomllib.loads(rendered)["security"]["allowed_hosts"] == ["a", "b"]

    def test_nested_tables_round_trip(self) -> None:
        rendered = render_toml({"provider": {"anthropic": {"model": "claude-opus-5"}}})
        assert tomllib.loads(rendered)["provider"]["anthropic"]["model"] == "claude-opus-5"

    def test_output_is_deterministic(self) -> None:
        config = {"run": {"b": 1, "a": 2}, "api": {"port": 1}}
        assert render_toml(config) == render_toml(config)

    def test_an_unsupported_value_is_refused_rather_than_mangled(self) -> None:
        with pytest.raises(MigrationError, match="not supported"):
            render_toml({"run": {"when": object()}})

    def test_a_too_deep_table_is_refused(self) -> None:
        with pytest.raises(MigrationError, match="nests deeper"):
            render_toml({"a": {"b": {"c": {"d": {"e": 1}}}}})


class TestEnvironmentLayer:
    """Configuration from the environment, which is how a container is set up."""

    def test_a_variable_becomes_a_key(self) -> None:
        assert env_overlay({"ORCHESTRATORPRO__API__PORT": "8080"}) == {"api": {"port": 8080}}

    def test_types_come_from_the_field(self) -> None:
        """So a host stays a string and a port becomes an integer."""
        overlay = env_overlay(
            {
                "ORCHESTRATORPRO__API__HOST": "0.0.0.0",
                "ORCHESTRATORPRO__API__PORT": "8080",
                "ORCHESTRATORPRO__RUN__AUTO_APPROVE": "true",
                "ORCHESTRATORPRO__AGENT__BUDGET_SECONDS": "12.5",
            }
        )
        assert overlay["api"]["host"] == "0.0.0.0"
        assert overlay["api"]["port"] == 8080
        assert overlay["run"]["auto_approve"] is True
        assert overlay["agent"]["budget_seconds"] == 12.5

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE"])
    def test_truthy_spellings(self, raw: str) -> None:
        assert env_overlay({"ORCHESTRATORPRO__RUN__AUTO_APPROVE": raw})["run"]["auto_approve"]

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
    def test_falsy_spellings(self, raw: str) -> None:
        overlay = env_overlay({"ORCHESTRATORPRO__RUN__AUTO_APPROVE": raw})
        assert overlay["run"]["auto_approve"] is False

    def test_a_bad_boolean_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="must be a boolean"):
            env_overlay({"ORCHESTRATORPRO__RUN__AUTO_APPROVE": "maybe"})

    def test_a_bad_integer_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="must be an integer"):
            env_overlay({"ORCHESTRATORPRO__API__PORT": "eight"})

    def test_nested_provider_settings_work(self) -> None:
        overlay = env_overlay({"ORCHESTRATORPRO__PROVIDER__ANTHROPIC__MODEL": "m"})
        assert overlay["provider"]["anthropic"]["model"] == "m"

    def test_role_overrides_work(self) -> None:
        overlay = env_overlay({"ORCHESTRATORPRO__PROVIDER__ROLES__PLANNER__EFFORT": "max"})
        assert overlay["provider"]["roles"]["planner"]["effort"] == "max"

    def test_unprefixed_variables_are_ignored(self) -> None:
        assert env_overlay({"PATH": "/usr/bin", "PORT": "80"}) == {}

    def test_an_empty_variable_is_not_a_value(self) -> None:
        """Compose materializes every declared variable; blank must not override."""
        assert env_overlay({"ORCHESTRATORPRO__API__HOST": ""}) == {}

    def test_a_list_stays_a_string_for_its_parser(self) -> None:
        overlay = env_overlay({"ORCHESTRATORPRO__SECURITY__ALLOWED_HOSTS": "a,b"})
        assert overlay["security"]["allowed_hosts"] == "a,b"

    def test_the_environment_overrides_the_files(self) -> None:
        assert (
            load_config(
                user_config=Path("/nonexistent"),
                env={"ORCHESTRATORPRO__RUN__MAX_CONCURRENCY": "9"},
            ).run.max_concurrency
            == 9
        )

    def test_the_environment_layer_is_recorded_as_a_source(self) -> None:
        config = load_config(
            user_config=Path("/nonexistent"),
            env={"ORCHESTRATORPRO__RUN__MAX_CONCURRENCY": "9"},
        )
        assert any("environment" in str(source) for source in config.sources)

    def test_it_can_be_turned_off(self) -> None:
        config = load_config(
            user_config=Path("/nonexistent"),
            env={"ORCHESTRATORPRO__RUN__MAX_CONCURRENCY": "9"},
            use_env=False,
        )
        assert config.run.max_concurrency == 4

    def test_a_credential_in_the_environment_layer_is_still_refused(self) -> None:
        """The secret scan applies to every layer, not just the files."""
        with pytest.raises(ConfigError, match="looks like a credential"):
            load_config(
                user_config=Path("/nonexistent"),
                env={"ORCHESTRATORPRO__PROVIDER__ANTHROPIC__API_KEY": "sk-secret"},
            )
