"""Tests for the command-line entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.cli import (
    APP_VERSION,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_UNSAFE,
    EXIT_USAGE,
    build_parser,
    main,
)
from orchestrator.ops.backup import list_backups
from tests.ops.conftest import populated_database


def run_cli(*args: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """Invoke the CLI and return its exit code and output."""
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestParser:
    """The surface itself."""

    def test_a_command_is_required(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_every_subcommand_has_a_handler(self) -> None:
        """A subcommand with no handler would crash instead of running."""
        parser = build_parser()
        for command in (
            ["version"],
            ["config", "show"],
            ["config", "check"],
            ["config", "migrate"],
            ["backup", "create", "/tmp/b"],
            ["backup", "list", "/tmp/b"],
            ["backup", "verify", "/tmp/x.db"],
            ["backup", "restore", "/tmp/x.db"],
            ["backup", "prune", "/tmp/b"],
            ["bench"],
            ["serve"],
        ):
            args = parser.parse_args(command)
            assert callable(args.handler), command

    def test_an_unknown_command_exits_with_usage(self) -> None:
        with pytest.raises(SystemExit) as caught:
            build_parser().parse_args(["nonsense"])
        assert caught.value.code == EXIT_USAGE

    def test_a_bare_group_requires_a_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["backup"])


class TestVersion:
    """`version`."""

    def test_it_prints_the_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, _ = run_cli("version", capsys=capsys)

        assert code == EXIT_OK
        assert APP_VERSION in out

    def test_json_carries_the_environment(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, _ = run_cli("--json", "version", capsys=capsys)
        payload = json.loads(out)

        assert code == EXIT_OK
        assert payload["version"] == APP_VERSION
        assert payload["python"]


class TestConfigCommands:
    """`config show`, `check`, and `migrate`."""

    def test_show_prints_the_effective_configuration(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, _ = run_cli(
            "--config", "/nonexistent", "--no-env", "config", "show", capsys=capsys
        )
        payload = json.loads(out)

        assert code == EXIT_OK
        assert payload["run"]["max_concurrency"] == 4
        assert payload["config_version"] == 1

    def test_check_accepts_a_loopback_default(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, _ = run_cli(
            "--config", "/nonexistent", "--no-env", "config", "check", capsys=capsys
        )

        assert code == EXIT_OK
        assert "valid" in out

    def test_check_reports_warnings(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_dir / "orchestrator.toml").write_text(
            "[run]\nauto_approve = true\n", encoding="utf-8"
        )
        code, out, _ = run_cli(
            "--config", "/nonexistent", "--no-env", "--repo", str(tmp_dir),
            "config", "check", capsys=capsys,
        )

        assert code == EXIT_OK
        assert "auto_approve" in out

    def test_check_refuses_an_unsafe_deployment(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Exit 3 so a deployment script can branch on it."""
        (tmp_dir / "orchestrator.toml").write_text(
            "[api]\nhost = '0.0.0.0'\n", encoding="utf-8"
        )
        code, _, err = run_cli(
            "--config", "/nonexistent", "--no-env", "--repo", str(tmp_dir),
            "config", "check", capsys=capsys,
        )

        assert code == EXIT_UNSAFE
        assert "auth_token_env" in err

    def test_check_reports_json_when_asked(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, _ = run_cli(
            "--config", "/nonexistent", "--no-env", "--json", "config", "check", capsys=capsys
        )
        payload = json.loads(out)

        assert code == EXIT_OK
        assert payload["ok"] is True
        assert payload["loopback"] is True

    def test_migrate_previews_by_default(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_dir / "config.toml"
        path.write_text("[run]\nmax_concurrency = 3\n", encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        code, out, _ = run_cli("config", "migrate", str(path), capsys=capsys)

        assert code == EXIT_OK
        assert "preview only" in out
        assert path.read_text(encoding="utf-8") == before

    def test_migrate_writes_when_told(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_dir / "config.toml"
        path.write_text("[run]\nmax_concurrency = 3\n", encoding="utf-8")

        code, _, _ = run_cli("config", "migrate", str(path), "--write", capsys=capsys)

        assert code == EXIT_OK
        assert "config_version" in path.read_text(encoding="utf-8")

    def test_migrate_reports_a_missing_file(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, _, err = run_cli(
            "config", "migrate", str(tmp_dir / "absent.toml"), capsys=capsys
        )

        assert code == EXIT_FAILED
        assert "no configuration file" in err


class TestBackupCommands:
    """`backup create`, `list`, `verify`, `restore`, `prune`."""

    def _database(self, tmp_dir: Path) -> Path:
        db = populated_database(tmp_dir / "runs.db")
        db.close()
        return tmp_dir / "runs.db"

    def test_create_takes_a_snapshot(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = self._database(tmp_dir)
        destination = tmp_dir / "backups"

        code, out, _ = run_cli(
            "--database", str(source), "backup", "create", str(destination), capsys=capsys
        )

        assert code == EXIT_OK
        assert "6 event(s)" in out
        assert len(list_backups(destination)) == 1

    def test_create_reports_a_missing_database(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, _, err = run_cli(
            "--database", str(tmp_dir / "absent.db"),
            "backup", "create", str(tmp_dir / "b"), capsys=capsys,
        )

        assert code == EXIT_FAILED
        assert "no database" in err

    def test_list_reports_an_empty_directory(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, _ = run_cli("backup", "list", str(tmp_dir), capsys=capsys)

        assert code == EXIT_OK
        assert "no backups" in out

    def test_list_reports_snapshots(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = self._database(tmp_dir)
        destination = tmp_dir / "backups"
        run_cli("--database", str(source), "backup", "create", str(destination), capsys=capsys)

        code, out, _ = run_cli("backup", "list", str(destination), capsys=capsys)

        assert code == EXIT_OK
        assert "1 snapshot(s)" in out

    def test_verify_accepts_an_intact_snapshot(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = self._database(tmp_dir)
        destination = tmp_dir / "backups"
        run_cli("--database", str(source), "backup", "create", str(destination), capsys=capsys)
        snapshot = list_backups(destination)[0].path

        code, out, _ = run_cli("backup", "verify", snapshot, capsys=capsys)

        assert code == EXIT_OK
        assert "intact" in out

    def test_verify_rejects_a_tampered_snapshot(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = self._database(tmp_dir)
        destination = tmp_dir / "backups"
        run_cli("--database", str(source), "backup", "create", str(destination), capsys=capsys)
        snapshot = Path(list_backups(destination)[0].path)
        snapshot.write_bytes(snapshot.read_bytes() + b"x")

        code, _, err = run_cli("backup", "verify", str(snapshot), capsys=capsys)

        assert code == EXIT_FAILED
        assert "does not match" in err

    def test_restore_requires_confirmation(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Nothing destructive happens without saying so first."""
        source = self._database(tmp_dir)
        destination = tmp_dir / "backups"
        run_cli("--database", str(source), "backup", "create", str(destination), capsys=capsys)
        snapshot = list_backups(destination)[0].path

        code, _, err = run_cli(
            "--database", str(tmp_dir / "target.db"), "backup", "restore", snapshot, capsys=capsys
        )

        assert code == EXIT_USAGE
        assert "--yes" in err
        assert not (tmp_dir / "target.db").exists()

    def test_restore_proceeds_when_confirmed(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = self._database(tmp_dir)
        destination = tmp_dir / "backups"
        run_cli("--database", str(source), "backup", "create", str(destination), capsys=capsys)
        snapshot = list_backups(destination)[0].path
        target = tmp_dir / "target.db"

        code, out, _ = run_cli(
            "--database", str(target), "backup", "restore", snapshot, "--yes", capsys=capsys
        )

        assert code == EXIT_OK
        assert target.is_file()
        assert "6 event(s)" in out

    def test_prune_previews_by_default(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = self._database(tmp_dir)
        destination = tmp_dir / "backups"
        for _ in range(3):
            run_cli(
                "--database", str(source), "backup", "create", str(destination), capsys=capsys
            )

        code, out, _ = run_cli(
            "backup", "prune", str(destination), "--keep", "1", capsys=capsys
        )

        assert code == EXIT_OK
        assert "would remove 2" in out
        assert len(list_backups(destination)) == 3

    def test_prune_deletes_when_confirmed(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = self._database(tmp_dir)
        destination = tmp_dir / "backups"
        for _ in range(3):
            run_cli(
                "--database", str(source), "backup", "create", str(destination), capsys=capsys
            )

        code, _, _ = run_cli(
            "backup", "prune", str(destination), "--keep", "1", "--yes", capsys=capsys
        )

        assert code == EXIT_OK
        assert len(list_backups(destination)) == 1

    def test_prune_refuses_to_empty_the_directory(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, _, err = run_cli(
            "backup", "prune", str(tmp_dir), "--keep", "0", "--yes", capsys=capsys
        )

        assert code == EXIT_FAILED
        assert "at least 1" in err


class TestBench:
    """`bench`."""

    def test_it_runs_and_reports(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, _ = run_cli("bench", "graph", "--scale", "0.02", capsys=capsys)

        assert code == EXIT_OK
        assert "graph.build" in out

    def test_json_output_is_machine_readable(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, _ = run_cli("--json", "bench", "graph", "--scale", "0.02", capsys=capsys)
        payload = json.loads(out)

        assert code == EXIT_OK
        assert payload["results"][0]["name"] == "graph.build"

    def test_an_unknown_benchmark_is_a_usage_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, _, err = run_cli("bench", "ghost", capsys=capsys)

        assert code == EXIT_USAGE
        assert "unknown benchmark" in err

    def test_checking_budgets_passes_on_a_healthy_machine(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, _, _ = run_cli("bench", "graph", "--scale", "0.02", "--check", capsys=capsys)
        assert code == EXIT_OK


class TestServe:
    """`serve`, as far as it can be exercised without binding a port."""

    def test_a_dry_run_reports_the_plan(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, _ = run_cli(
            "--config", "/nonexistent", "--no-env",
            "--database", str(tmp_dir / "runs.db"),
            "serve", "--dry-run", capsys=capsys,
        )
        payload = json.loads(out)

        assert code == EXIT_OK
        assert payload["bind"] == "127.0.0.1:8765"
        assert payload["endpoints"] > 20

    def test_a_dry_run_creates_the_database(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_cli(
            "--config", "/nonexistent", "--no-env",
            "--database", str(tmp_dir / "nested" / "runs.db"),
            "serve", "--dry-run", capsys=capsys,
        )
        assert (tmp_dir / "nested" / "runs.db").is_file()

    def test_the_bind_can_be_overridden(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, _ = run_cli(
            "--config", "/nonexistent", "--no-env",
            "--database", str(tmp_dir / "runs.db"),
            "serve", "--port", "9001", "--dry-run", capsys=capsys,
        )

        assert code == EXIT_OK
        assert json.loads(out)["bind"] == "127.0.0.1:9001"

    def test_it_refuses_an_unsafe_bind(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The last line of defence before an open, unauthenticated port."""
        code, _, err = run_cli(
            "--config", "/nonexistent", "--no-env",
            "--database", str(tmp_dir / "runs.db"),
            "serve", "--host", "0.0.0.0", "--dry-run", capsys=capsys,
        )

        assert code == EXIT_UNSAFE
        assert "refusing to start" in err

    def test_warnings_reach_the_report(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_dir / "orchestrator.toml").write_text(
            "[run]\nauto_approve = true\n", encoding="utf-8"
        )
        code, out, _ = run_cli(
            "--config", "/nonexistent", "--no-env", "--repo", str(tmp_dir),
            "--database", str(tmp_dir / "runs.db"),
            "serve", "--dry-run", capsys=capsys,
        )

        assert code == EXIT_OK
        assert any("auto_approve" in warning for warning in json.loads(out)["warnings"])

    def test_dry_run_reports_execution_available(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """execution_available must be present and boolean in every dry-run report."""
        code, out, _ = run_cli(
            "--config", "/nonexistent", "--no-env",
            "--database", str(tmp_dir / "runs.db"),
            "serve", "--dry-run", capsys=capsys,
        )
        payload = json.loads(out)

        assert code == EXIT_OK
        assert "execution_available" in payload
        assert isinstance(payload["execution_available"], bool)


class TestErrorHandling:
    """What happens when something goes wrong at the top level."""

    def test_a_configuration_error_exits_unsafe(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_dir / "orchestrator.toml").write_text(
            "[run]\nmax_concurrency = 'many'\n", encoding="utf-8"
        )
        code, _, err = run_cli(
            "--config", "/nonexistent", "--no-env", "--repo", str(tmp_dir),
            "config", "show", capsys=capsys,
        )

        assert code == EXIT_UNSAFE
        assert "configuration error" in err

    def test_an_unexpected_failure_is_reported_not_traced(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A traceback is for a bug report, not for an operator at 3am."""
        parser_args = build_parser().parse_args(["version"])
        assert parser_args.handler is not None

        def explode(_: object) -> int:
            raise RuntimeError("something broke")

        import orchestrator.cli as cli

        original = cli.cmd_version
        cli.cmd_version = explode  # type: ignore[assignment]
        try:
            code, _, err = run_cli("version", capsys=capsys)
        finally:
            cli.cmd_version = original  # type: ignore[assignment]

        assert code == EXIT_FAILED
        assert "something broke" in err
        assert "Traceback" not in err


class TestGateEnvironmentWiring:
    """`[gates] env` must reach the SuiteSpec the executor gates with.

    Config parsing is tested in tests/core; this is the wiring — the step
    that was missing, and the reason one project's PYTHONPATH=src suite could not be
    gated on at all.
    """

    def test_configured_env_reaches_the_suite_spec(self, tmp_dir: Path) -> None:
        import logging

        from orchestrator.cli import _build_services
        from orchestrator.core.config import OrchestratorConfig
        from tests.e2e.conftest import make_repository

        repo_root = make_repository(tmp_dir / "repo")
        config = OrchestratorConfig.from_mapping(
            {
                "gates": {
                    "test_command": "python -m pytest -q",
                    "env": {"PYTHONPATH": "src"},
                }
            },
            env={},
        )

        _services, gate_specs, mode = _build_services(
            config,
            repo_root=repo_root,
            work_root=tmp_dir / "work",
            transcripts_root=tmp_dir / "transcripts",
            log=logging.getLogger("test"),
        )
        assert mode == "full"
        assert gate_specs["tests"].env == {"PYTHONPATH": "src"}
        assert gate_specs["tests"].command == "python -m pytest -q"
