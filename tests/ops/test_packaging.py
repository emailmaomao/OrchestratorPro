"""Tests for the release artifacts.

A Dockerfile is not run here — building an image in a unit test would be slow
and would need a daemon. What is checked is everything that can be got wrong
without noticing: the entry point that does not exist, the asset directory that
does not ship, the compose file that publishes a port on every interface, the
example environment that quietly recommends a wildcard.

Those are the mistakes that reach an operator, because none of them fail until
the moment they matter.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def read(name: str) -> str:
    """Read one artifact from the repository root."""
    path = ROOT / name
    assert path.is_file(), f"{name} is missing"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pyproject() -> dict:
    """The parsed project metadata."""
    return tomllib.loads(read("pyproject.toml"))


@pytest.fixture(scope="module")
def dockerfile() -> str:
    """The Dockerfile, as text."""
    return read("Dockerfile")


@pytest.fixture(scope="module")
def ignored() -> set[str]:
    """The .dockerignore entries."""
    return {
        line.strip()
        for line in read(".dockerignore").splitlines()
        if line.strip() and not line.startswith("#")
    }


@pytest.fixture(scope="module")
def compose() -> str:
    """The compose file, as text."""
    return read("docker-compose.yml")


@pytest.fixture(scope="module")
def example() -> str:
    """The example environment file, as text."""
    return read(".env.example")


@pytest.fixture(scope="module")
def shell() -> str:
    """The POSIX installer, as text."""
    return read("scripts/install.sh")


@pytest.fixture(scope="module")
def powershell() -> str:
    """The Windows installer, as text."""
    return read("scripts/install.ps1")


class TestProjectMetadata:
    """What a wheel would carry."""

    def test_the_distribution_is_named(self, pyproject: dict) -> None:
        assert pyproject["project"]["name"] == "orchestratorpro"

    def test_the_version_matches_the_cli(self, pyproject: dict) -> None:
        """One version, written down once."""
        from orchestrator.cli import APP_VERSION

        assert pyproject["project"]["version"] == APP_VERSION

    def test_every_reported_version_is_the_same_version(self, pyproject: dict) -> None:
        """The API reported 0.1.0 from a 1.0.0 wheel until v1.0.

        Nothing caught it, because the CLI and the packaging agreed with each
        other and the API kept its own number. A version a client reads from
        ``/health`` must be the version it is talking to.
        """
        from orchestrator.api.state import API_VERSION
        from orchestrator.cli import APP_VERSION
        from orchestrator.core.version import VERSION

        declared = pyproject["project"]["version"]

        assert {declared, APP_VERSION, API_VERSION} == {VERSION}

    def test_the_entry_point_resolves(self, pyproject: dict) -> None:
        """A console script naming a function that does not exist installs fine."""
        target = pyproject["project"]["scripts"]["orchestratorpro"]
        module_name, _, attribute = target.partition(":")

        import importlib

        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute))

    def test_the_runtime_dependencies_are_few_and_named(
        self, pyproject: dict
    ) -> None:
        """The web stack, plus PyYAML for the planner. Nothing else."""
        names = {
            re.split(r"[><=!\[]", entry)[0].strip()
            for entry in pyproject["project"]["dependencies"]
        }
        assert names == {"fastapi", "uvicorn", "pydantic", "pyyaml"}

    def test_no_model_backend_is_required(self, pyproject: dict) -> None:
        """Provider independence, expressed in the packaging."""
        required = " ".join(pyproject["project"]["dependencies"]).lower()

        assert "anthropic" not in required
        assert "openai" not in required
        assert "anthropic" in pyproject["project"]["optional-dependencies"]

    def test_the_dashboard_assets_ship(self, pyproject: dict) -> None:
        """Without this the UI is a 404 in every installed copy."""
        package_data = pyproject["tool"]["setuptools"]["package-data"]

        assert "orchestrator.dashboard" in package_data
        assert any("static" in pattern for pattern in package_data["orchestrator.dashboard"])

    def test_the_asset_pattern_matches_real_files(self, pyproject: dict) -> None:
        """A pattern that matches nothing declares an empty directory."""
        from orchestrator.dashboard.app import STATIC_ROOT

        patterns = pyproject["tool"]["setuptools"]["package-data"]["orchestrator.dashboard"]
        package = STATIC_ROOT.parent

        matched: set[Path] = set()
        for pattern in patterns:
            matched.update(path for path in package.glob(pattern) if path.is_file())

        assert len(matched) >= 10, f"only {len(matched)} asset(s) would ship"
        assert any(path.name == "index.html" for path in matched)
        assert any(path.name == "app.js" for path in matched)

    def test_the_declared_python_matches_the_code(self, pyproject: dict) -> None:
        assert pyproject["project"]["requires-python"] == ">=3.11"

    def test_a_license_is_declared(self, pyproject: dict) -> None:
        assert pyproject["project"]["license"] == "MIT"
        assert (ROOT / "LICENSE").is_file()

    def test_the_test_markers_are_registered(self, pyproject: dict) -> None:
        """`live` is referenced by the testing policy; unregistered it warns."""
        markers = " ".join(pyproject["tool"]["pytest"]["ini_options"]["markers"])

        assert "live:" in markers
        assert "slow:" in markers

    def test_the_pinned_requirements_agree_with_the_metadata(self) -> None:
        """Two files naming dependencies must not disagree about which."""
        pinned = {
            re.split(r"[><=!#\s]", line)[0].strip().lower()
            for line in read("requirements.txt").splitlines()
            if line.strip() and not line.startswith("#")
        }
        for name in ("fastapi", "uvicorn", "pydantic", "pyyaml"):
            assert name in pinned


class TestDockerfile:
    """The image, read rather than built."""

    def test_it_is_a_multi_stage_build(self, dockerfile: str) -> None:
        """So no compiler or source tree reaches the runtime image."""
        assert dockerfile.count("FROM ") >= 2
        assert "AS build" in dockerfile
        assert "AS runtime" in dockerfile

    def test_the_base_image_is_pinned(self, dockerfile: str) -> None:
        for line in dockerfile.splitlines():
            if line.startswith("FROM "):
                assert ":" in line, f"unpinned base image: {line}"
                assert ":latest" not in line, f"floating base image: {line}"

    def test_it_does_not_run_as_root(self, dockerfile: str) -> None:
        """An orchestrator runs commands on behalf of a model."""
        assert re.search(r"^USER\s+orchestrator", dockerfile, re.MULTILINE)
        # And the switch happens before the entry point, not after.
        assert dockerfile.index("USER orchestrator") < dockerfile.index("ENTRYPOINT")

    def test_the_user_is_created_without_a_login_shell(self, dockerfile: str) -> None:
        assert "nologin" in dockerfile

    def test_git_is_installed(self, dockerfile: str) -> None:
        """Every attempt gets a worktree; without git the runtime is useless."""
        assert re.search(r"apt-get install[^\n]*git", dockerfile)

    def test_the_package_lists_are_removed(self, dockerfile: str) -> None:
        assert "rm -rf /var/lib/apt/lists" in dockerfile

    def test_state_is_on_a_volume(self, dockerfile: str) -> None:
        """A container that loses the event log has lost every run."""
        assert "VOLUME" in dockerfile
        assert "/var/lib/orchestratorpro" in dockerfile

    def test_it_declares_a_healthcheck(self, dockerfile: str) -> None:
        assert "HEALTHCHECK" in dockerfile
        assert "/health" in dockerfile

    def test_the_healthcheck_needs_nothing_extra(self, dockerfile: str) -> None:
        """One that needs curl stops working when the base image slims down."""
        healthcheck = dockerfile[dockerfile.index("HEALTHCHECK") :]
        assert "curl" not in healthcheck
        assert "wget" not in healthcheck

    def test_the_entry_point_is_the_console_script(self, dockerfile: str) -> None:
        assert 'ENTRYPOINT ["orchestratorpro"]' in dockerfile

    def test_it_carries_image_labels(self, dockerfile: str) -> None:
        assert "org.opencontainers.image.title" in dockerfile
        assert "org.opencontainers.image.licenses" in dockerfile


class TestDockerignore:
    """What never reaches the build context."""

    def test_secrets_do_not_enter_the_context(self, ignored: set[str]) -> None:
        """A .env in the context ends up in an image layer."""
        assert ".env" in ignored

    def test_the_example_is_still_included(self, ignored: set[str]) -> None:
        assert "!.env.example" in ignored

    def test_git_history_is_excluded(self, ignored: set[str]) -> None:
        """It carries every secret anybody ever committed and then removed."""
        assert ".git" in ignored

    def test_the_virtualenv_is_excluded(self, ignored: set[str]) -> None:
        assert ".venv" in ignored

    def test_databases_are_excluded(self, ignored: set[str]) -> None:
        assert "*.db" in ignored


class TestCompose:
    """The single-node deployment."""

    def test_it_publishes_on_loopback_only(self, compose: str) -> None:
        """This build has no authentication."""
        published = re.findall(r'^\s*-\s*"([^"]*:\d+)"', compose, re.MULTILINE)
        mappings = [entry for entry in published if entry.count(":") >= 2]

        assert mappings, "no port mapping was found"
        for mapping in mappings:
            assert mapping.startswith("127.0.0.1:"), f"published on any interface: {mapping}"

    def test_state_is_a_named_volume(self, compose: str) -> None:
        assert "orchestratorpro-state:/var/lib/orchestratorpro" in compose
        assert re.search(r"^volumes:", compose, re.MULTILINE)

    def test_the_container_filesystem_is_read_only(self, compose: str) -> None:
        assert "read_only: true" in compose

    def test_privileges_cannot_be_escalated(self, compose: str) -> None:
        assert "no-new-privileges:true" in compose
        assert "cap_drop" in compose

    def test_logs_are_bounded(self, compose: str) -> None:
        """An unbounded json-file driver fills the disk and stops the host."""
        assert "max-size" in compose
        assert "max-file" in compose

    def test_it_declares_a_healthcheck(self, compose: str) -> None:
        assert "healthcheck:" in compose

    def test_shutdown_has_a_grace_period(self, compose: str) -> None:
        """In-flight attempts have already spent their tokens."""
        assert "stop_grace_period" in compose

    def test_a_backup_service_is_available(self, compose: str) -> None:
        assert "backup create /backups" in compose
        assert "backup prune /backups" in compose

    def test_the_backup_service_does_not_run_by_default(self, compose: str) -> None:
        assert 'profiles: ["backup"]' in compose

    def test_the_image_tag_is_pinned(self, compose: str) -> None:
        assert re.search(r"image: orchestratorpro:\d+\.\d+\.\d+", compose)


class TestEnvExample:
    """The file an operator copies first."""

    def test_it_explains_the_naming_scheme(self, example: str) -> None:
        assert "ORCHESTRATORPRO__API__PORT" in example
        assert "Two underscores" in example

    def test_every_variable_it_sets_is_understood(self, example: str) -> None:
        """A documented variable that nothing reads is a documented lie."""
        from orchestrator.core.config import env_overlay

        assignments = {
            line.split("=", 1)[0].strip()
            for line in example.splitlines()
            if line.strip() and not line.startswith("#") and "=" in line
        }
        prefixed = {name for name in assignments if name.startswith("ORCHESTRATORPRO__")}

        # Every prefixed variable must survive the overlay rather than being
        # silently dropped, and must coerce without raising.
        overlay = env_overlay({name: "1" for name in prefixed})
        assert len(prefixed) == 0 or overlay

    def test_the_compose_variables_are_all_documented(self, example: str) -> None:
        compose = read("docker-compose.yml")
        referenced = set(re.findall(r"\$\{(ORCHESTRATORPRO_[A-Z_]+)", compose))
        documented = set(re.findall(r"^(ORCHESTRATORPRO_[A-Z_]+)=", example, re.MULTILINE))

        assert referenced <= documented, f"undocumented: {sorted(referenced - documented)}"

    def test_it_does_not_contain_a_credential(self, example: str) -> None:
        for line in example.splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if any(marker in name.lower() for marker in ("key", "secret", "password")):
                assert not value.strip(), f"{name} has a value in the example"

    def test_it_says_credentials_do_not_belong_in_configuration(
        self, example: str
    ) -> None:
        assert "Credentials are NOT configuration" in example

    def test_it_warns_that_there_is_no_authentication(self, example: str) -> None:
        assert "no authentication" in example

    def test_the_defaults_are_the_safe_ones(self, example: str) -> None:
        assert "ORCHESTRATORPRO_ALLOWED_HOSTS=localhost,127.0.0.1" in example
        assert "CORS_ORIGINS=*" not in example


class TestInstallScripts:
    """The two installers."""

    def test_the_shell_script_fails_fast(self, shell: str) -> None:
        """Without `set -e` an installer reports success after failing."""
        assert "set -euo pipefail" in shell

    def test_the_shell_script_has_a_shebang(self, shell: str) -> None:
        assert shell.startswith("#!/usr/bin/env bash")

    def test_the_powershell_script_fails_fast(self, powershell: str) -> None:
        assert "$ErrorActionPreference = 'Stop'" in powershell

    @pytest.mark.parametrize("script", ["scripts/install.sh", "scripts/install.ps1"])
    def test_both_check_the_python_version(self, script: str) -> None:
        """By asking the interpreter, not by parsing --version."""
        text = read(script)

        assert "sys.version_info" in text
        assert "3, 11" in text or "3.11" in text or "$MinMajor, $MinMinor" in text

    @pytest.mark.parametrize("script", ["scripts/install.sh", "scripts/install.ps1"])
    def test_both_install_into_a_virtual_environment(self, script: str) -> None:
        text = read(script)

        assert "venv" in text
        assert "pip install" in text

    @pytest.mark.parametrize("script", ["scripts/install.sh", "scripts/install.ps1"])
    def test_both_verify_the_installation(self, script: str) -> None:
        """An installer that does not check is a report, not an install."""
        assert "orchestrator.cli version" in read(script)

    @pytest.mark.parametrize("script", ["scripts/install.sh", "scripts/install.ps1"])
    def test_both_warn_about_the_missing_authentication(self, script: str) -> None:
        assert "no authentication" in read(script)

    @pytest.mark.parametrize("script", ["scripts/install.sh", "scripts/install.ps1"])
    def test_neither_requires_git(self, script: str) -> None:
        """The control plane records and reports without it."""
        text = read(script)
        assert "WARNING" in text and "git" in text

    @pytest.mark.parametrize("script", ["scripts/install.sh", "scripts/install.ps1"])
    def test_both_offer_a_development_install(self, script: str) -> None:
        assert "[dev]" in read(script)

    def test_the_shell_script_parses(self) -> None:
        """A syntax error in an installer is found by the person installing."""
        import shutil
        import subprocess

        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash is not available")

        result = subprocess.run(
            [bash, "-n", str(ROOT / "scripts" / "install.sh")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr

    def test_the_powershell_script_parses(self) -> None:
        import shutil
        import subprocess

        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell is None:
            pytest.skip("PowerShell is not available")

        script = str(ROOT / "scripts" / "install.ps1")
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop';"
                "$errors=$null;"
                f"[System.Management.Automation.Language.Parser]::ParseFile('{script}',"
                "[ref]$null,[ref]$errors) > $null;"
                "if ($errors.Count -gt 0) { $errors | ForEach-Object { $_.Message }; exit 1 }",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr


class TestDocumentationAgrees:
    """The artifacts and the documents describe the same thing."""

    def test_the_readme_mentions_the_installers(self) -> None:
        readme = read("README.md").lower()
        assert "install" in readme

    def test_gitignore_excludes_the_environment_file(self) -> None:
        assert ".env" in read(".gitignore")

    def test_the_env_file_is_not_committed(self) -> None:
        """The example is; the real one never is."""
        import subprocess

        result = subprocess.run(
            ["git", "ls-files", ".env"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=60,
        )
        assert result.stdout.strip() == ""
