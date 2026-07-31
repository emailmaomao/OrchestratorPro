"""The command-line entry point.

``orchestratorpro <command>``. Built on :mod:`argparse` rather than a CLI
framework: the whole surface is a dozen subcommands over functions that already
exist, and a dependency for that is a dependency an operator has to install
before they can find out whether their configuration is valid.

Conventions, because a CLI that is inconsistent about them is a CLI people
script around rather than with:

* **Exit codes mean something.** ``0`` success, ``1`` the operation failed,
  ``2`` the invocation was wrong, ``3`` the configuration or deployment is
  unsafe. A script can branch on that.
* **Machine-readable output is opt-in.** ``--json`` on any command that has
  something to report.
* **Nothing destructive happens without saying so first.** ``restore`` moves
  the existing database aside; ``prune`` refuses to empty the directory.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orchestrator.core.config import OrchestratorConfig, load_config
from orchestrator.core.events import ConfigError
from orchestrator.core.logging import configure_logging
from orchestrator.core.version import VERSION

__all__ = ["APP_VERSION", "build_parser", "main"]

#: The distribution's version, from ``core.version``. Kept as a name here
#: because the packaging test and several call sites already refer to it.
APP_VERSION = VERSION

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_UNSAFE = 3

#: Where a deployment keeps its state unless told otherwise.
DEFAULT_HOME = Path.home() / ".orchestratorpro"


def _emit(payload: Any, *, as_json: bool, stream: Any = None) -> None:
    """Write a result, as JSON or as text."""
    out = stream or sys.stdout
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str), file=out)
    elif isinstance(payload, str):
        print(payload, file=out)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str), file=out)


def _load(args: argparse.Namespace) -> OrchestratorConfig:
    """Resolve the configuration a command should run against."""
    return load_config(
        repo_root=args.repo,
        user_config=args.config,
        use_env=not getattr(args, "no_env", False),
    )


def _database_path(args: argparse.Namespace) -> Path:
    """Resolve where the run database lives."""
    if args.database:
        return Path(args.database)
    return DEFAULT_HOME / "runs.db"


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_version(args: argparse.Namespace) -> int:
    """Print the version."""
    import platform

    _emit(
        {
            "version": APP_VERSION,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        }
        if args.json
        else f"orchestratorpro {APP_VERSION}",
        as_json=args.json,
    )
    return EXIT_OK


def cmd_config_show(args: argparse.Namespace) -> int:
    """Print the effective configuration."""
    from orchestrator.api.schemas import ConfigResponse

    config = _load(args)
    payload = ConfigResponse.from_config(config).model_dump()
    payload["config_version"] = config.config_version
    _emit(payload, as_json=True)
    return EXIT_OK


def cmd_config_check(args: argparse.Namespace) -> int:
    """Validate the configuration and report how safe the deployment is."""
    from orchestrator.ops.hardening import verify_deployment

    config = _load(args)
    try:
        warnings = verify_deployment(config)
    except ConfigError as error:
        _emit(
            {"ok": False, "error": {"code": error.code, "message": str(error)}}
            if args.json
            else f"unsafe: {error}",
            as_json=args.json,
            stream=sys.stderr,
        )
        return EXIT_UNSAFE

    payload = {
        "ok": True,
        "bind": f"{config.api.host}:{config.api.port}",
        "loopback": config.api.is_local,
        "config_version": config.config_version,
        "sources": [str(source) for source in config.sources],
        "warnings": [
            {"code": w.code, "severity": w.severity, "message": w.message} for w in warnings
        ],
    }
    if args.json:
        _emit(payload, as_json=True)
    else:
        _emit(f"configuration is valid ({len(warnings)} warning(s))", as_json=False)
        for warning in warnings:
            _emit(f"  {warning}", as_json=False)
    return EXIT_OK


def cmd_config_migrate(args: argparse.Namespace) -> int:
    """Bring a configuration file up to the current shape."""
    from orchestrator.ops.migrate import MigrationError, migrate_file

    path = args.file or (DEFAULT_HOME / "config.toml")
    try:
        result = migrate_file(Path(path), write=args.write, backup=not args.no_backup)
    except MigrationError as error:
        _emit(str(error), as_json=False, stream=sys.stderr)
        return EXIT_FAILED

    payload = {
        "path": str(path),
        "from_version": result.from_version,
        "to_version": result.to_version,
        "changed": result.changed,
        "written": bool(args.write and result.changed),
        "applied": list(result.applied),
        "notes": list(result.notes),
    }
    if args.json:
        _emit(payload, as_json=True)
    else:
        _emit(result.summary(), as_json=False)
        for line in (*result.applied, *result.notes):
            _emit(f"  {line}", as_json=False)
        if result.changed and not args.write:
            _emit("  (preview only; pass --write to apply)", as_json=False)
    return EXIT_OK


def cmd_backup_create(args: argparse.Namespace) -> int:
    """Take a snapshot of the run database."""
    from orchestrator.ops.backup import BackupError, create_backup

    try:
        manifest = create_backup(
            _database_path(args), Path(args.destination), label=args.label
        )
    except BackupError as error:
        _emit(str(error), as_json=False, stream=sys.stderr)
        return EXIT_FAILED

    _emit(manifest.to_json() if args.json else manifest.summary(), as_json=False)
    return EXIT_OK


def cmd_backup_list(args: argparse.Namespace) -> int:
    """List the snapshots in a directory."""
    from orchestrator.ops.backup import list_backups, total_size

    manifests = list_backups(Path(args.destination))
    if args.json:
        _emit([json.loads(m.to_json()) for m in manifests], as_json=True)
        return EXIT_OK

    if not manifests:
        _emit(f"no backups in {args.destination}", as_json=False)
        return EXIT_OK
    for manifest in manifests:
        _emit(manifest.summary(), as_json=False)
    _emit(
        f"{len(manifests)} snapshot(s), {total_size(manifests) / 1_048_576:.1f} MiB total",
        as_json=False,
    )
    return EXIT_OK


def cmd_backup_verify(args: argparse.Namespace) -> int:
    """Check that a snapshot is intact."""
    from orchestrator.ops.backup import BackupError, verify_backup

    try:
        manifest = verify_backup(Path(args.snapshot))
    except BackupError as error:
        _emit(str(error), as_json=False, stream=sys.stderr)
        return EXIT_FAILED

    _emit(f"intact — {manifest.summary()}", as_json=False)
    return EXIT_OK


def cmd_backup_restore(args: argparse.Namespace) -> int:
    """Restore a snapshot over the run database."""
    from orchestrator.ops.backup import BackupError, restore_backup

    destination = _database_path(args)
    if not args.yes:
        _emit(
            f"this would replace {destination} with {args.snapshot}\n"
            "the existing database is moved aside, not deleted\n"
            "pass --yes to proceed",
            as_json=False,
            stream=sys.stderr,
        )
        return EXIT_USAGE

    try:
        manifest = restore_backup(Path(args.snapshot), destination)
    except BackupError as error:
        _emit(str(error), as_json=False, stream=sys.stderr)
        return EXIT_FAILED

    _emit(f"restored {manifest.events} event(s) to {destination}", as_json=False)
    return EXIT_OK


def cmd_backup_prune(args: argparse.Namespace) -> int:
    """Delete all but the newest snapshots."""
    from orchestrator.ops.backup import BackupError, prune_backups

    try:
        removed = prune_backups(
            Path(args.destination), keep=args.keep, dry_run=not args.yes
        )
    except BackupError as error:
        _emit(str(error), as_json=False, stream=sys.stderr)
        return EXIT_FAILED

    verb = "would remove" if not args.yes else "removed"
    _emit(f"{verb} {len(removed)} snapshot(s), keeping {args.keep}", as_json=False)
    for path in removed:
        _emit(f"  {Path(path).name}", as_json=False)
    return EXIT_OK


def cmd_bench(args: argparse.Namespace) -> int:
    """Run the performance benchmarks."""
    from orchestrator.ops.bench import Budgets, run_benchmarks

    try:
        suite = run_benchmarks(args.only or None, scale=args.scale)
    except ValueError as error:
        _emit(str(error), as_json=False, stream=sys.stderr)
        return EXIT_USAGE

    if args.json:
        _emit(suite.to_dict(), as_json=True)
    else:
        _emit(suite.summary(), as_json=False)

    failures = Budgets().check(suite) if args.check else ()
    for failure in failures:
        _emit(f"BELOW BUDGET  {failure}", as_json=False, stream=sys.stderr)
    return EXIT_FAILED if failures else EXIT_OK


def _build_services(
    config: OrchestratorConfig,
    *,
    repo_root: Path | None,
    work_root: Path,
    transcripts_root: Path,
    log: logging.Logger,
) -> tuple[Any, dict[str, Any], str]:
    """Assemble the executor's collaborators, as fully as the setup allows.

    Returns:
        ``(services, gate_specs, mode)`` where ``mode`` is ``"full"`` — isolated
        worktrees, commits, gates, and merges against ``repo_root`` — or
        ``"fallback"`` — a plain shared directory in which nothing is verified
        and nothing is committed. Fallback is a genuine dry-run mode, but it
        must be *entered knowingly*: every downgrade is logged with its cause.
    """
    from orchestrator.workflow.executor import ExecutionServices

    if repo_root is not None:
        try:
            import asyncio

            from orchestrator.git_manager.commit import CommitManager
            from orchestrator.git_manager.merge import MergeManager
            from orchestrator.git_manager.repo import GitRepository
            from orchestrator.git_manager.workspace import WorkspaceManager
            from orchestrator.test_runner.base import SuiteSpec
            from orchestrator.test_runner.runner import TestRunner

            repo = GitRepository(repo_root)
            if not asyncio.run(repo.is_repository()):
                raise ValueError(f"{repo_root} is not inside a Git repository")

            services = ExecutionServices(
                workspaces=WorkspaceManager(repo, root=work_root / "worktrees"),
                commits=CommitManager(repo),
                merges=MergeManager(repo),
                gates=TestRunner(),
                transcripts=transcripts_root,
            )
            # One gate is configured today, under the name the workflow schema
            # defaults to. A registry mapping arbitrary gate names to runners is
            # OP-007; until then a step asking for an unknown gate is skipped
            # with a recorded reason by the executor, never silently passed.
            gate_specs: dict[str, Any] = {
                "tests": SuiteSpec(
                    command=config.gates.test_command,
                    parser=config.gates.parser,
                    timeout_s=config.gates.timeout_s,
                    name="tests",
                    env=config.gates.env,
                )
            }
            return services, gate_specs, "full"
        except Exception as exc:  # degrade, but say why
            log.warning(
                "git-backed execution is unavailable (%s); falling back to a "
                "plain directory in which attempts share a workspace, nothing "
                "is committed, and no gate verifies the work",
                exc,
            )

    return (
        ExecutionServices(fallback_root=work_root, transcripts=transcripts_root),
        {},
        "fallback",
    )


def _build_execution(
    config: OrchestratorConfig,
    *,
    repo_root: Path | None,
    work_root: Path,
    transcripts_root: Path,
    tools: Any,
    log: logging.Logger,
) -> tuple[Any, str]:
    """Assemble the execution backend `serve` wires into the application.

    Returns:
        ``(executor_factory, mode)``. The factory is ``None`` and the mode
        ``"unavailable"`` when no backend could be built at all — the server
        then records and reports but refuses to execute (503), which is still
        more useful than refusing to start.
    """
    # `agent.adapter` has been a config key since M1 and has never selected
    # anything. "claude_code" is the harness adapter: Claude Code runs *inside*
    # the attempt's worktree and edits files itself, while the engine keeps
    # isolation, gating, commit, and merge. It needs no model provider, because
    # it *is* the model — so it is built before, and instead of, the runtime.
    if config.agent.adapter == "claude_code":
        try:
            from orchestrator.adapter.claude_code import (
                ClaudeCodeHarness,
                HarnessConfig,
            )
            from orchestrator.workflow.executor import StepExecutor

            harness = ClaudeCodeHarness(
                HarnessConfig(
                    executable=config.provider_for("worker").executable or "claude",
                    timeout_s=config.agent.budget_seconds,
                    # `[agent] model`, not a provider block: this is the agent's
                    # model, and a provider block's default is an API model
                    # identifier that would be wrong to hand a CLI. Empty is
                    # honoured — it means "let the CLI decide", which is what
                    # the engine did before this key existed.
                    model=config.agent.model,
                )
            )
        except Exception as exc:  # startup resilience
            log.warning(
                "the claude_code harness could not be initialised (%s); "
                "the server will record and report runs but not execute them",
                exc,
            )
            return None, "unavailable"

        work_root.mkdir(parents=True, exist_ok=True)
        services, gate_specs, mode = _build_services(
            config,
            repo_root=repo_root,
            work_root=work_root,
            transcripts_root=transcripts_root,
            log=log,
        )

        def _harness_factory(plan: Any, run_id: Any, emitter: Any) -> Any:
            return StepExecutor(
                harness,
                plan,
                run_id=run_id,
                services=services,
                emitter=emitter,
                gate_specs=gate_specs,
            )

        log.info("execution backend ready: claude_code harness (%s mode)", mode)
        return _harness_factory, mode

    try:
        from orchestrator.agent.runtime import AgentRuntime, RuntimeConfig
        from orchestrator.provider.base import Domain, ModelPort
        from orchestrator.provider.registry import build_default_registry, settings_for
        from orchestrator.workflow.executor import StepExecutor

        provider_registry = build_default_registry(config)
        # Settings are read for the provider that is actually BOUND. Reading a
        # fixed block here is how a configured [provider.claude] model was once
        # silently discarded in favour of [provider.anthropic] defaults.
        bound = provider_registry.bound(Domain.MODEL) or "anthropic"
        provider_settings = settings_for(config, bound)
        model_provider = provider_registry.get(Domain.MODEL)
        if not isinstance(model_provider, ModelPort):
            raise TypeError(
                f"provider {bound!r} satisfies the substrate but not the model "
                "port; it cannot drive an agent"
            )

        runtime = AgentRuntime(
            model_provider,
            config=RuntimeConfig(
                model=provider_settings.model,
                effort=provider_settings.effort,
                max_output_tokens=provider_settings.max_tokens,
                reasoning=provider_settings.thinking,
            ),
            registry=tools,
        )

        # Start the provider now, not at the first model call. A provider that
        # was never started has no transport and fails every attempt with
        # "call startup() first" — a run accepted and then failed uniformly is
        # strictly worse than a server that says up front it cannot execute.
        # startup() is construction and local health, never a network call, so
        # this cannot hang a start; a missing SDK lands in the except below and
        # the server degrades to the unavailable mode with the cause logged.
        import asyncio

        reports = asyncio.run(provider_registry.startup_all())
        unhealthy = [report for report in reports if not report.healthy]
        if unhealthy:
            raise RuntimeError(
                f"provider {unhealthy[0].provider_id} reports unhealthy: "
                f"{unhealthy[0].detail}"
            )
    except Exception as exc:  # startup resilience
        log.warning(
            "execution backend could not be initialised (%s); "
            "the server will record and report runs but cannot execute them",
            exc,
        )
        return None, "unavailable"

    work_root.mkdir(parents=True, exist_ok=True)
    services, gate_specs, mode = _build_services(
        config,
        repo_root=repo_root,
        work_root=work_root,
        transcripts_root=transcripts_root,
        log=log,
    )

    def _executor_factory(plan: Any, run_id: Any, emitter: Any) -> Any:
        return StepExecutor(
            runtime,
            plan,
            run_id=run_id,
            services=services,
            emitter=emitter,
            gate_specs=gate_specs,
        )

    log.info(
        "execution backend ready: %s via %r (%s mode)",
        provider_settings.model,
        bound,
        mode,
    )
    return _executor_factory, mode


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the API and dashboard under Uvicorn."""
    from orchestrator.api.state import AppState
    from orchestrator.core.run_store import RunStore
    from orchestrator.core.storage import Database
    from orchestrator.dashboard.app import create_app
    from orchestrator.ops.hardening import harden, verify_deployment

    config = _load(args)
    if args.host:
        from dataclasses import replace

        config = replace(config, api=replace(config.api, host=args.host))
    if args.port:
        from dataclasses import replace

        config = replace(config, api=replace(config.api, port=args.port))

    try:
        warnings = verify_deployment(config)
    except ConfigError as error:
        _emit(f"refusing to start: {error}", as_json=False, stream=sys.stderr)
        return EXIT_UNSAFE

    database_path = _database_path(args)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database = Database(database_path)
    database.migrate()

    state = AppState(store=RunStore(database), config=config)
    if args.workspace:
        state.workspace_root = Path(args.workspace)

    from orchestrator.auth.service import AuthService
    from orchestrator.auth.store import AuthStore
    from orchestrator.auth.tokens import TokenService

    state.auth = AuthService(AuthStore(database), TokenService(_secret()))

    # Wire the execution backend. Degradation is graceful on purpose — a server
    # that cannot reach a model, or has no repository, is still useful for
    # recording, reporting, and approvals — but every downgrade is logged with
    # its cause and reported as an execution_mode, never entered silently.
    log = logging.getLogger("orchestrator.cli")
    work_root = state.workspace_root or (database_path.parent / "workspace")
    executor_factory, execution_mode = _build_execution(
        config,
        repo_root=args.repo,
        work_root=work_root,
        # Beside the database: with the default database this is exactly the
        # ~/.orchestratorpro/transcripts/<run>/<task>/<attempt>.jsonl layout
        # docs/020 §5 documents, and a relocated database keeps its transcripts.
        transcripts_root=database_path.parent / "transcripts",
        tools=state.tools,
        log=log,
    )
    if executor_factory is not None:
        state.executor_factory = executor_factory
    if execution_mode == "fallback" and config.run.max_concurrency > 1:
        # Without worktrees every attempt runs in the same directory, and
        # concurrent attempts in one directory is the contamination this system
        # exists to prevent. Clamping beats refusing: fallback is a legitimate
        # dry-run mode, but it must not be a parallel one.
        from dataclasses import replace

        log.warning(
            "fallback mode has no isolated worktrees; clamping "
            "run.max_concurrency from %d to 1 so concurrent attempts cannot "
            "share a working directory",
            config.run.max_concurrency,
        )
        config = replace(config, run=replace(config.run, max_concurrency=1))
        state.config = config
    if getattr(args, "verify", False) and execution_mode == "unavailable":
        # The opt-in strict mode: the default serve degrades gracefully so that
        # recording, reporting, and approvals survive a broken backend, but an
        # operator who asked for verification wants a refusal, not a 503 later.
        _emit(
            "refusing to serve: --verify is set and the execution backend "
            "could not be initialised (the cause is in the log above)",
            as_json=False,
            stream=sys.stderr,
        )
        database.close()
        return EXIT_FAILED

    app = harden(create_app(state=state), config.security)

    for warning in warnings:
        log.warning("deployment warning: %s", warning)
    if state.auth.has_accounts:
        log.info(
            "authentication is required; %s account(s)",
            state.auth.store.count_users(),
        )
    else:
        log.warning(
            "no accounts exist, so every route is served as an operator; run "
            "`orchestratorpro auth bootstrap` to require credentials"
        )

    if args.dry_run:
        _emit(
            {
                "bind": f"{config.api.host}:{config.api.port}",
                "database": str(database_path),
                "warnings": [str(w) for w in warnings],
                "endpoints": len(app.openapi().get("paths", {})),
                "authentication": (
                    "required" if state.auth.has_accounts else "open"
                ),
                "execution_available": state.can_execute,
                "execution_mode": execution_mode,
            },
            as_json=True,
        )
        database.close()
        return EXIT_OK

    import uvicorn  # imported here so --dry-run works without it

    uvicorn.run(
        app,
        host=config.api.host,
        port=config.api.port,
        log_config=None,
        timeout_graceful_shutdown=int(config.security.shutdown_grace_s),
    )
    return EXIT_OK



def _auth_service(args: argparse.Namespace) -> Any:
    """Open the auth service against the configured database."""
    from orchestrator.auth.service import AuthService
    from orchestrator.auth.store import AuthStore
    from orchestrator.auth.tokens import TokenService
    from orchestrator.core.storage import Database

    path = _database_path(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    database = Database(path)
    database.migrate()
    return AuthService(AuthStore(database), TokenService(_secret()))


def _secret() -> str:
    """Resolve the token-signing secret.

    From the environment, or generated once and written beside the database
    with owner-only permissions. Generating it is the lesser evil: the
    alternative is an installation that will not start until somebody reads a
    docstring, and the usual response to that is a secret pasted into a config
    file, where it gets committed.
    """
    import os
    import stat

    from orchestrator.auth.tokens import TokenService

    existing = os.environ.get("ORCHESTRATORPRO_SECRET_KEY", "")
    if existing:
        return existing

    path = DEFAULT_HOME / "secret.key"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()

    path.parent.mkdir(parents=True, exist_ok=True)
    secret = TokenService.generate_secret()
    path.write_text(secret, encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - not every filesystem allows it
        pass
    return secret


def cmd_auth_bootstrap(args: argparse.Namespace) -> int:
    """Create the first administrator."""
    from orchestrator.auth.models import AuthError

    service = _auth_service(args)
    try:
        user, password = service.store.bootstrap(args.username)
    except AuthError as error:
        _emit(str(error), as_json=False, stream=sys.stderr)
        return EXIT_FAILED

    if args.json:
        _emit({"username": user.username, "password": password}, as_json=True)
    else:
        _emit(f"created administrator {user.username}", as_json=False)
        _emit(f"  password: {password}", as_json=False)
        _emit("", as_json=False)
        _emit("  This is shown once. Store it now.", as_json=False)
        _emit("  Every route requires a credential from this moment on.", as_json=False)
    return EXIT_OK


def cmd_auth_users(args: argparse.Namespace) -> int:
    """List accounts."""
    service = _auth_service(args)
    users = [user.to_public() for user in service.store.list_users()]

    if args.json:
        _emit(users, as_json=True)
    elif not users:
        _emit("no accounts; run `orchestratorpro auth bootstrap`", as_json=False)
    else:
        for user in users:
            state = "" if user["active"] else " (disabled)"
            _emit(f"{user['username']:<24} {user['role']:<10}{state}", as_json=False)
    return EXIT_OK


def cmd_auth_add(args: argparse.Namespace) -> int:
    """Create an account, generating a password unless one is given."""
    import secrets as _secrets

    from orchestrator.auth.models import AuthError, Role

    service = _auth_service(args)
    password = args.password or _secrets.token_urlsafe(18)
    try:
        user = service.store.create_user(args.username, password, Role.parse(args.role))
    except AuthError as error:
        _emit(str(error), as_json=False, stream=sys.stderr)
        return EXIT_FAILED

    service.store.audit(actor="cli", action="user.create", target=user.username)
    if args.json:
        _emit(
            {"username": user.username, "role": user.role.value, "password": password},
            as_json=True,
        )
    else:
        _emit(f"created {user.username} ({user.role.value})", as_json=False)
        if not args.password:
            _emit(f"  password: {password}", as_json=False)
    return EXIT_OK


def cmd_auth_audit(args: argparse.Namespace) -> int:
    """Read the audit trail."""
    service = _auth_service(args)
    entries = [entry.to_public() for entry in service.store.read_audit(limit=args.limit)]

    if args.json:
        _emit(entries, as_json=True)
    elif not entries:
        _emit("the audit trail is empty", as_json=False)
    else:
        for entry in entries:
            _emit(
                f"{entry['at']}  {entry['actor']:<20} {entry['action']:<20} "
                f"{entry['outcome']:<8} {entry['target']}",
                as_json=False,
            )
    return EXIT_OK


def cmd_retention_plan(args: argparse.Namespace) -> int:
    """Report what a retention policy would remove."""
    from orchestrator.core.run_store import RunStore
    from orchestrator.core.storage import Database
    from orchestrator.ops.retention import RetentionPolicy, plan_retention

    database = Database(_database_path(args))
    database.migrate()
    policy = RetentionPolicy(keep_runs=args.keep_runs, keep_days=args.keep_days)
    plan = plan_retention(RunStore(database), policy)

    if args.json:
        _emit(
            {
                "policy": policy.describe(),
                "eligible": [str(run_id) for run_id in plan.eligible],
                "kept": len(plan.kept),
            },
            as_json=True,
        )
    else:
        _emit(policy.describe(), as_json=False)
        _emit(plan.summary(), as_json=False)
        for run_id in plan.eligible:
            _emit(f"  {run_id}", as_json=False)
    database.close()
    return EXIT_OK


def cmd_retention_apply(args: argparse.Namespace) -> int:
    """Archive and prune according to a policy."""
    from orchestrator.core.run_store import RunStore
    from orchestrator.core.storage import Database
    from orchestrator.ops.retention import RetentionPolicy, apply_retention

    database = Database(_database_path(args))
    database.migrate()
    policy = RetentionPolicy(
        keep_runs=args.keep_runs, keep_days=args.keep_days, archive=not args.no_archive
    )
    report = apply_retention(
        RunStore(database),
        policy,
        directory=Path(args.archives),
        dry_run=not args.yes,
    )
    database.close()

    if args.json:
        _emit(
            {
                "archived": list(report.archived),
                "pruned": list(report.pruned),
                "failures": list(report.failures),
                "dry_run": not args.yes,
            },
            as_json=True,
        )
    else:
        _emit(("would " if not args.yes else "") + report.summary(), as_json=False)
        for failure in report.failures:
            _emit(f"  FAILED {failure}", as_json=False, stream=sys.stderr)
    return EXIT_OK if report.ok else EXIT_FAILED


def cmd_retention_verify(args: argparse.Namespace) -> int:
    """Verify every archive in a directory."""
    from orchestrator.ops.retention import verify_all

    good, bad = verify_all(Path(args.archives))
    if args.json:
        _emit({"intact": list(good), "damaged": list(bad)}, as_json=True)
    else:
        _emit(f"{len(good)} intact, {len(bad)} damaged", as_json=False)
        for entry in bad:
            _emit(f"  {entry}", as_json=False, stream=sys.stderr)
    return EXIT_FAILED if bad else EXIT_OK


def cmd_workflow_check(args: argparse.Namespace) -> int:
    """Validate a YAML workflow without running it."""
    from orchestrator.planner.loader import check_workflow

    path = Path(args.file)
    if not path.is_file():
        _emit(f"there is no file at {path}", as_json=False, stream=sys.stderr)
        return EXIT_FAILED

    report = check_workflow(path.read_text(encoding="utf-8"), source=str(path))
    if args.json:
        _emit(
            {
                "ok": report.ok,
                "issues": [{"path": i.path, "message": i.message} for i in report.issues],
            },
            as_json=True,
        )
    elif report.ok:
        _emit(f"{path.name} is valid", as_json=False)
    else:
        _emit(f"{path.name}: {report.summary()}", as_json=False, stream=sys.stderr)
        for issue in report.issues:
            _emit(f"  - {issue}", as_json=False, stream=sys.stderr)
    return EXIT_OK if report.ok else EXIT_FAILED


def cmd_run(args: argparse.Namespace) -> int:
    """Execute a workflow file to completion, without serving anything.

    The same composition root as ``serve`` minus the HTTP layer: load the
    workflow, build the execution backend, drive the engine, report. The exit
    code is the point - ``0`` only when every step succeeded - so a scheduler
    or a CI job can branch on it without parsing anything.

    Args:
        args: Parsed arguments.

    Returns:
        The exit code.
    """
    import asyncio

    from orchestrator.agent.tools import default_registry
    from orchestrator.core.run_store import RunStore
    from orchestrator.core.storage import Database
    from orchestrator.planner.loader import load_workflow_file
    from orchestrator.workflow.engine import EngineConfig, WorkflowEngine

    path = Path(args.file)
    if not path.is_file():
        _emit(f"there is no file at {path}", as_json=False, stream=sys.stderr)
        return EXIT_USAGE

    config = _load(args)
    definition = load_workflow_file(path)

    database_path = _database_path(args)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database = Database(database_path)
    database.migrate()

    log = logging.getLogger("orchestrator.cli")
    work_root = Path(args.workspace) if args.workspace else database_path.parent / "workspace"
    factory, mode = _build_execution(
        config,
        repo_root=args.repo,
        work_root=work_root,
        transcripts_root=database_path.parent / "transcripts",
        tools=default_registry(),
        log=log,
    )

    if factory is None:
        database.close()
        _emit(
            "refusing to run: no execution backend could be built; the cause "
            "is in the log above",
            as_json=False,
            stream=sys.stderr,
        )
        return EXIT_UNSAFE

    # A one-shot command is exactly where fallback mode is most dangerous. A
    # served instance in fallback is a visible dry-run an operator is watching;
    # an unattended `run` that quietly produced nothing verified, nothing
    # committed, and no branch is a lie told by an exit code.
    if mode != "full" and not args.allow_unverified:
        database.close()
        _emit(
            f"refusing to run: execution mode is {mode!r}, not 'full'. Without "
            "a Git repository there are no worktrees, no gates, and no commits, "
            "so nothing this run produced would be verified. Pass --repo BEFORE "
            "the subcommand, or --allow-unverified to proceed anyway.",
            as_json=False,
            stream=sys.stderr,
        )
        return EXIT_UNSAFE

    engine = WorkflowEngine(
        config=EngineConfig(
            max_concurrency=args.max_concurrency or config.run.max_concurrency,
            repo_path=str(args.repo) if args.repo else "",
        ),
        store=RunStore(database),
    )
    try:
        report = asyncio.run(engine.run(definition, factory))
    finally:
        database.close()

    branch = f"orchestrator/{report.run_id}/integration"
    succeeded = len(report.succeeded_steps)
    total = succeeded + len(report.unfinished_steps)

    if args.json:
        _emit(
            {
                "run_id": str(report.run_id),
                "workflow": report.workflow,
                "outcome": report.outcome,
                "complete": report.complete,
                "execution_mode": mode,
                "succeeded": list(report.succeeded_steps),
                "unfinished": list(report.unfinished_steps),
                "integration_branch": branch if mode == "full" else None,
            },
            as_json=True,
        )
    else:
        _emit(f"{report.workflow}: {report.outcome}", as_json=False)
        _emit(f"  run     {report.run_id}", as_json=False)
        _emit(f"  steps   {succeeded}/{total} succeeded", as_json=False)
        for step in report.unfinished_steps:
            _emit(f"            unfinished: {step}", as_json=False)
        if mode == "full":
            _emit(f"  branch  {branch}", as_json=False)
            _emit("  review the diff before merging; nothing reached main", as_json=False)

    return EXIT_OK if report.complete else EXIT_FAILED


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="orchestratorpro",
        description="A self-hosted control plane for fleets of AI coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"orchestratorpro {APP_VERSION}")
    parser.add_argument(
        "--config", type=Path, default=None, help="path to the user configuration file"
    )
    parser.add_argument(
        "--repo", type=Path, default=None, help="repository root holding orchestrator.toml"
    )
    parser.add_argument(
        "--database", type=Path, default=None, help="path to the run database"
    )
    parser.add_argument(
        "--no-env",
        action="store_true",
        help="ignore ORCHESTRATORPRO__* environment overrides",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--log-level", default="warning", help="logging level (debug, info, warning, error)"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    version = sub.add_parser("version", help="print the version")
    version.set_defaults(handler=cmd_version)

    config = sub.add_parser("config", help="inspect, check, and migrate configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)

    show = config_sub.add_parser("show", help="print the effective configuration")
    show.set_defaults(handler=cmd_config_show)

    check = config_sub.add_parser("check", help="validate it and report deployment risks")
    check.set_defaults(handler=cmd_config_check)

    migrate = config_sub.add_parser("migrate", help="bring a configuration file up to date")
    migrate.add_argument("file", nargs="?", type=Path, help="the file to migrate")
    migrate.add_argument("--write", action="store_true", help="apply, rather than preview")
    migrate.add_argument("--no-backup", action="store_true", help="do not keep the original")
    migrate.set_defaults(handler=cmd_config_migrate)

    backup = sub.add_parser("backup", help="snapshot, verify, and restore the database")
    backup_sub = backup.add_subparsers(dest="backup_command", required=True)

    create = backup_sub.add_parser("create", help="take a snapshot")
    create.add_argument("destination", type=Path, help="directory to write into")
    create.add_argument("--label", default="", help="append a label to the filename")
    create.set_defaults(handler=cmd_backup_create)

    listing = backup_sub.add_parser("list", help="list snapshots")
    listing.add_argument("destination", type=Path, help="the backup directory")
    listing.set_defaults(handler=cmd_backup_list)

    verify = backup_sub.add_parser("verify", help="check a snapshot is intact")
    verify.add_argument("snapshot", type=Path, help="the .db file")
    verify.set_defaults(handler=cmd_backup_verify)

    restore = backup_sub.add_parser("restore", help="restore a snapshot")
    restore.add_argument("snapshot", type=Path, help="the .db file")
    restore.add_argument("--yes", action="store_true", help="proceed without confirmation")
    restore.set_defaults(handler=cmd_backup_restore)

    prune = backup_sub.add_parser("prune", help="delete all but the newest snapshots")
    prune.add_argument("destination", type=Path, help="the backup directory")
    prune.add_argument("--keep", type=int, default=7, help="how many to retain")
    prune.add_argument("--yes", action="store_true", help="delete, rather than preview")
    prune.set_defaults(handler=cmd_backup_prune)

    bench = sub.add_parser("bench", help="run the performance benchmarks")
    bench.add_argument("only", nargs="*", help="benchmarks to run; all when omitted")
    bench.add_argument("--scale", type=float, default=1.0, help="scale each benchmark's size")
    bench.add_argument("--check", action="store_true", help="fail if a budget is missed")
    bench.set_defaults(handler=cmd_bench)

    auth = sub.add_parser("auth", help="accounts, keys, and the audit trail")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    bootstrap = auth_sub.add_parser("bootstrap", help="create the first administrator")
    bootstrap.add_argument("username", nargs="?", default="admin")
    bootstrap.set_defaults(handler=cmd_auth_bootstrap)

    users = auth_sub.add_parser("users", help="list accounts")
    users.set_defaults(handler=cmd_auth_users)

    add_user = auth_sub.add_parser("add", help="create an account")
    add_user.add_argument("username")
    add_user.add_argument("--role", default="viewer", help="viewer, operator, or admin")
    add_user.add_argument("--password", default="", help="omit to generate one")
    add_user.set_defaults(handler=cmd_auth_add)

    audit_cmd = auth_sub.add_parser("audit", help="read the audit trail")
    audit_cmd.add_argument("--limit", type=int, default=50)
    audit_cmd.set_defaults(handler=cmd_auth_audit)

    retention = sub.add_parser("retention", help="archive and prune old runs")
    retention_sub = retention.add_subparsers(dest="retention_command", required=True)

    retention_plan = retention_sub.add_parser("plan", help="report what would be removed")
    retention_plan.add_argument("--keep-runs", type=int, default=100, dest="keep_runs")
    retention_plan.add_argument("--keep-days", type=int, default=30, dest="keep_days")
    retention_plan.set_defaults(handler=cmd_retention_plan)

    retention_apply = retention_sub.add_parser("apply", help="archive and prune")
    retention_apply.add_argument("archives", type=Path, help="where archives are written")
    retention_apply.add_argument("--keep-runs", type=int, default=100, dest="keep_runs")
    retention_apply.add_argument("--keep-days", type=int, default=30, dest="keep_days")
    retention_apply.add_argument(
        "--no-archive", action="store_true", help="delete without archiving"
    )
    retention_apply.add_argument("--yes", action="store_true", help="apply, rather than preview")
    retention_apply.set_defaults(handler=cmd_retention_apply)

    retention_verify = retention_sub.add_parser("verify", help="verify every archive")
    retention_verify.add_argument("archives", type=Path)
    retention_verify.set_defaults(handler=cmd_retention_verify)

    workflow = sub.add_parser("workflow", help="work with YAML workflow files")
    workflow_sub = workflow.add_subparsers(dest="workflow_command", required=True)

    workflow_check = workflow_sub.add_parser("check", help="validate a workflow file")
    workflow_check.add_argument("file", type=Path)
    workflow_check.set_defaults(handler=cmd_workflow_check)

    run_cmd = sub.add_parser(
        "run", help="execute a workflow file to completion, without serving"
    )
    run_cmd.add_argument("file", type=Path, help="the workflow YAML to execute")
    run_cmd.add_argument(
        "--workspace", type=Path, default=None, help="where worktrees are created"
    )
    run_cmd.add_argument(
        "--max-concurrency",
        type=int,
        default=None,
        dest="max_concurrency",
        help="override the configured step concurrency",
    )
    run_cmd.add_argument(
        "--allow-unverified",
        action="store_true",
        dest="allow_unverified",
        help="proceed without a repository, where nothing is gated or committed",
    )
    run_cmd.set_defaults(handler=cmd_run)

    serve = sub.add_parser("serve", help="run the API and dashboard")
    serve.add_argument("--host", default=None, help="override the configured bind address")
    serve.add_argument("--port", type=int, default=None, help="override the configured port")
    serve.add_argument("--workspace", type=Path, default=None, help="confine paths to this root")
    serve.add_argument(
        "--dry-run", action="store_true", help="check the deployment and exit"
    )
    serve.add_argument(
        "--verify",
        action="store_true",
        help=(
            "refuse to serve unless the execution backend initialises and "
            "reports healthy; verifies construction and health, not a live "
            "completion"
        ),
    )
    serve.set_defaults(handler=cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Arguments, excluding the program name. Defaults to ``sys.argv``.

    Returns:
        The exit code.
    """
    # Windows consoles default to a codepage that cannot encode the characters
    # in a summary line. Reconfiguring beats mangling the output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover - not always possible
                pass

    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(
        level=getattr(logging, str(args.log_level).upper(), logging.WARNING),
        json_output=True,
    )

    try:
        return int(args.handler(args))
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return EXIT_UNSAFE
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("interrupted", file=sys.stderr)
        return EXIT_FAILED
    except Exception as error:  # noqa: BLE001 - the CLI is the last boundary
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
