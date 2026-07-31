"""Layered configuration loading and validation.

Configuration resolves in three layers, later layers overriding earlier ones
key by key:

1. **Defaults** — the dataclass defaults in this module.
2. **User** — ``~/.orchestratorpro/config.toml``.
3. **Repository** — ``orchestrator.toml`` in the repository root.

Two invariants are enforced here rather than left to review, because both are
security properties and both are silent when violated:

* **Credentials never come from configuration** (NFR-3.5). Any key whose name
  looks like a secret is rejected outright. Credentials are resolved from the
  environment, or from a vendor SDK's own credential chain.
* **A non-local API bind requires an authentication token** (NFR-3.4). The
  server refuses to start otherwise, so a development convenience cannot
  quietly become an open port.

Validation is strict: unknown keys are an error, not a warning. A typo in
``max_concurency`` that silently kept the default would be discovered only by
noticing the run was slow.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import MISSING, dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from orchestrator.core.events import Budget, ConfigError

__all__ = [
    "AgentConfig",
    "ApiConfig",
    "Effort",
    "GatesConfig",
    "GitConfig",
    "OrchestratorConfig",
    "ProviderConfig",
    "RoleConfig",
    "RunConfig",
    "SecurityConfig",
    "ThinkingMode",
    "load_config",
]


#: Substrings that mark a configuration key as carrying a credential.
#: Deliberately broad: a false positive costs one renamed key, a false negative
#: costs a leaked secret committed to version control.
_SECRET_MARKERS: Final = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "credential",
    "private_key",
    "access_key",
    "auth_token",
    "bearer",
)

#: Keys with this suffix name an *environment variable* rather than holding a
#: value, so they are exempt from the credential scan. ``api.auth_token_env =
#: "OP_TOKEN"`` records where to find a token; it is not itself a token.
_ENV_REFERENCE_SUFFIX: Final = "_env"

#: Hosts that do not require an authentication token to bind.
_LOCAL_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})

_USER_CONFIG_PATH: Final = Path.home() / ".orchestratorpro" / "config.toml"
_REPO_CONFIG_NAME: Final = "orchestrator.toml"


class Effort(StrEnum):
    """How much depth a model should spend on a turn.

    A neutral intent. Each provider translates it to its backend's own control
    or declares the capability unsupported; the value is never passed through
    verbatim as a vendor parameter.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ThinkingMode(StrEnum):
    """Whether a model reasons internally before answering."""

    ADAPTIVE = "adaptive"
    DISABLED = "disabled"


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def _walk_keys(data: Mapping[str, Any], path: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """Yield the dotted path of every key in a nested mapping."""
    for key, value in data.items():
        here = (*path, key)
        yield here
        if isinstance(value, Mapping):
            yield from _walk_keys(value, here)


def _reject_secrets(data: Mapping[str, Any]) -> None:
    """Reject any configuration key whose name implies a credential.

    Raises:
        ConfigError: If a secret-looking key is present, naming its location.
    """
    for path in _walk_keys(data):
        name = path[-1].lower()
        if name.endswith(_ENV_REFERENCE_SUFFIX):
            continue
        for marker in _SECRET_MARKERS:
            if marker in name:
                dotted = ".".join(path)
                raise ConfigError(
                    f"configuration key {dotted!r} looks like a credential; "
                    "credentials must come from the environment, never from a "
                    "config file",
                    detail={"key": dotted, "marker": marker},
                )


def _merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto ``base``, returning a new mapping."""
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge(existing, value)
        else:
            merged[key] = value
    return merged


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Return a named table from ``data``, or an empty mapping if absent."""
    value = data.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigError(
            f"section [{name}] must be a table, got {type(value).__name__}",
            detail={"section": name},
        )
    return value


def _reject_unknown(section: str, data: Mapping[str, Any], known: frozenset[str]) -> None:
    """Raise if ``data`` contains keys outside ``known``."""
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"unknown key(s) in section [{section}]: {', '.join(unknown)}",
            detail={"section": section, "unknown": unknown, "known": sorted(known)},
        )


def _field_names(cls: type) -> frozenset[str]:
    """Return the declared field names of a dataclass."""
    return frozenset(f.name for f in fields(cls))


def _defaults(cls: type) -> dict[str, Any]:
    """Return the declared default value of each field that has one.

    Note:
        A ``slots=True`` dataclass does **not** expose its defaults as class
        attributes — ``ApiConfig.port`` yields the slot descriptor, not ``8765``.
        Defaults must be read from the field metadata, which is what this does.
    """
    return {f.name: f.default for f in fields(cls) if f.default is not MISSING}


def _typed(section: str, data: Mapping[str, Any], key: str, expected: type, default: Any) -> Any:
    """Fetch ``key`` from ``data``, checking its type and applying a default."""
    if key not in data:
        return default
    value = data[key]
    # bool is a subclass of int; accepting one for the other hides real typos.
    if expected is not bool and isinstance(value, bool):
        raise ConfigError(
            f"{section}.{key} must be {expected.__name__}, got bool",
            detail={"section": section, "key": key},
        )
    if expected is float and isinstance(value, int):
        return float(value)
    if not isinstance(value, expected):
        raise ConfigError(
            f"{section}.{key} must be {expected.__name__}, got {type(value).__name__}",
            detail={"section": section, "key": key},
        )
    return value


def _enum(section: str, data: Mapping[str, Any], key: str, enum: type[StrEnum], default: Any) -> Any:
    """Fetch ``key`` from ``data`` and coerce it to ``enum``."""
    raw = _typed(section, data, key, str, None)
    if raw is None:
        return default
    try:
        return enum(raw)
    except ValueError as exc:
        allowed = ", ".join(m.value for m in enum)
        raise ConfigError(
            f"{section}.{key} must be one of: {allowed}; got {raw!r}",
            detail={"section": section, "key": key, "allowed": allowed},
        ) from exc


def _positive(section: str, key: str, value: float) -> None:
    """Raise unless ``value`` is strictly positive."""
    if value <= 0:
        raise ConfigError(
            f"{section}.{key} must be positive, got {value}",
            detail={"section": section, "key": key, "value": value},
        )


# --------------------------------------------------------------------------- #
# Configuration sections
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Run-wide execution policy."""

    max_concurrency: int = 4
    auto_approve: bool = False

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> RunConfig:
        """Build a :class:`RunConfig` from the ``[run]`` table."""
        _reject_unknown("run", data, _field_names(cls))
        defaults = _defaults(cls)
        max_concurrency = _typed(
            "run", data, "max_concurrency", int, defaults["max_concurrency"]
        )
        if max_concurrency < 1:
            raise ConfigError(
                f"run.max_concurrency must be at least 1, got {max_concurrency}",
                detail={"value": max_concurrency},
            )
        return cls(
            max_concurrency=max_concurrency,
            auto_approve=_typed("run", data, "auto_approve", bool, defaults["auto_approve"]),
        )


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Settings for one model provider.

    ``executable`` and ``timeout_s`` exist for CLI-backed providers
    (``claude_cli``); API-backed providers ignore them. They live here rather
    than in a separate section because validation is strict — a
    ``[provider.claude_cli]`` block with an ``executable`` key must parse, and
    an unknown key is an error by design.
    """

    model: str = "claude-opus-5"
    effort: Effort = Effort.XHIGH
    max_tokens: int = 64_000
    thinking: ThinkingMode = ThinkingMode.ADAPTIVE
    executable: str = ""
    timeout_s: float = 900.0

    def __post_init__(self) -> None:
        if not self.model:
            raise ConfigError("provider.model must not be empty")
        _positive("provider", "max_tokens", self.max_tokens)
        _positive("provider", "timeout_s", self.timeout_s)
        # Disabling reasoning is only meaningful at moderate depth; asking for
        # maximum depth with reasoning off is contradictory, and on at least one
        # current backend it is rejected outright. Catching it here turns a
        # mid-run HTTP 400 into a startup error.
        if self.thinking is ThinkingMode.DISABLED and self.effort in (
            Effort.XHIGH,
            Effort.MAX,
        ):
            raise ConfigError(
                f"provider.thinking = 'disabled' is incompatible with "
                f"provider.effort = {self.effort.value!r}; disable thinking only "
                "at effort 'high' or below",
                detail={"effort": self.effort.value, "thinking": self.thinking.value},
            )

    @classmethod
    def parse(cls, name: str, data: Mapping[str, Any]) -> ProviderConfig:
        """Build a :class:`ProviderConfig` from a ``[provider.<name>]`` table."""
        section = f"provider.{name}"
        _reject_unknown(section, data, _field_names(cls))
        defaults = _defaults(cls)
        return cls(
            model=_typed(section, data, "model", str, defaults["model"]),
            effort=_enum(section, data, "effort", Effort, defaults["effort"]),
            max_tokens=_typed(section, data, "max_tokens", int, defaults["max_tokens"]),
            thinking=_enum(section, data, "thinking", ThinkingMode, defaults["thinking"]),
            executable=_typed(section, data, "executable", str, defaults["executable"]),
            timeout_s=_typed(section, data, "timeout_s", float, defaults["timeout_s"]),
        )


@dataclass(frozen=True, slots=True)
class RoleConfig:
    """A per-role override of the default provider settings.

    Roles let one run mix depths: an expensive planner, a cheap summarizer.
    Unset fields inherit from the provider's own configuration.
    """

    model: str | None = None
    effort: Effort | None = None

    @classmethod
    def parse(cls, role: str, data: Mapping[str, Any]) -> RoleConfig:
        """Build a :class:`RoleConfig` from a ``[provider.roles.<role>]`` table."""
        section = f"provider.roles.{role}"
        _reject_unknown(section, data, _field_names(cls))
        return cls(
            model=_typed(section, data, "model", str, None),
            effort=_enum(section, data, "effort", Effort, None),
        )

    def apply_to(self, base: ProviderConfig) -> ProviderConfig:
        """Return ``base`` with this role's overrides applied."""
        from dataclasses import replace

        return replace(
            base,
            model=self.model if self.model is not None else base.model,
            effort=self.effort if self.effort is not None else base.effort,
        )


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Per-attempt agent limits and backend selection."""

    budget_seconds: float = 1800.0
    budget_tokens: int = 2_000_000
    budget_tool_calls: int = 200
    adapter: str = "tool_loop"
    #: Which model the agent runs on, for adapters that can choose.
    #:
    #: This is the **agent's** model, which is why it lives beside ``adapter``
    #: rather than in a ``[provider]`` block: those carry API model identifiers
    #: (``claude-opus-5``) whose default would be wrong to forward to a CLI, and
    #: ``ProviderConfig`` still cannot distinguish "set" from "defaulted".
    #:
    #: An alias rather than a pinned identifier, deliberately. Under a
    #: subscription the CLI maps ``sonnet`` to whatever Sonnet it currently
    #: serves, which is the right level of specificity — pinning a dated id here
    #: would go stale silently and override a decision that is the CLI's to make.
    #:
    #: Empty means "pass no ``--model`` at all", which restores the pre-2026-07-28
    #: behaviour: the CLI's own configured default decides.
    model: str = "sonnet"

    def __post_init__(self) -> None:
        _positive("agent", "budget_seconds", self.budget_seconds)
        _positive("agent", "budget_tokens", self.budget_tokens)
        _positive("agent", "budget_tool_calls", self.budget_tool_calls)
        if not self.adapter:
            raise ConfigError("agent.adapter must not be empty")

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> AgentConfig:
        """Build an :class:`AgentConfig` from the ``[agent]`` table."""
        _reject_unknown("agent", data, _field_names(cls))
        defaults = _defaults(cls)
        return cls(
            budget_seconds=_typed(
                "agent", data, "budget_seconds", float, defaults["budget_seconds"]
            ),
            budget_tokens=_typed("agent", data, "budget_tokens", int, defaults["budget_tokens"]),
            budget_tool_calls=_typed(
                "agent", data, "budget_tool_calls", int, defaults["budget_tool_calls"]
            ),
            adapter=_typed("agent", data, "adapter", str, defaults["adapter"]),
            model=_typed("agent", data, "model", str, defaults["model"]),
        )

    def to_budget(self) -> Budget:
        """Render these limits as a :class:`Budget` for a single attempt."""
        return Budget(
            seconds=self.budget_seconds,
            tokens=self.budget_tokens,
            tool_calls=self.budget_tool_calls,
        )


@dataclass(frozen=True, slots=True)
class GitConfig:
    """Version-control policy for a run."""

    integration_branch: str = "orchestrator/integration"
    retain_failed_worktrees: bool = True

    def __post_init__(self) -> None:
        if not self.integration_branch:
            raise ConfigError("git.integration_branch must not be empty")

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> GitConfig:
        """Build a :class:`GitConfig` from the ``[git]`` table."""
        _reject_unknown("git", data, _field_names(cls))
        defaults = _defaults(cls)
        return cls(
            integration_branch=_typed(
                "git", data, "integration_branch", str, defaults["integration_branch"]
            ),
            retain_failed_worktrees=_typed(
                "git", data, "retain_failed_worktrees", bool, defaults["retain_failed_worktrees"]
            ),
        )


@dataclass(frozen=True, slots=True)
class GatesConfig:
    """How an attempt's work is verified before it is accepted.

    ``env`` exists because a project's suite often needs one: one project's needs
    ``PYTHONPATH=src``, and without it the gate reports a red suite that is
    really a misconfigured harness — the ``failed`` versus ``errored``
    confusion this codebase refuses everywhere else, arriving through the
    back door. Values are *added* to the inherited environment, not a
    replacement for it.
    """

    test_command: str = "pytest -q"
    parser: str = "pytest"
    timeout_s: float = 900.0
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.test_command:
            raise ConfigError("gates.test_command must not be empty")
        _positive("gates", "timeout_s", self.timeout_s)

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> GatesConfig:
        """Build a :class:`GatesConfig` from the ``[gates]`` table."""
        _reject_unknown("gates", data, _field_names(cls))
        defaults = _defaults(cls)
        return cls(
            test_command=_typed("gates", data, "test_command", str, defaults["test_command"]),
            parser=_typed("gates", data, "parser", str, defaults["parser"]),
            timeout_s=_typed("gates", data, "timeout_s", float, defaults["timeout_s"]),
            env=_string_map("gates", data, "env"),
        )


@dataclass(frozen=True, slots=True)
class ApiConfig:
    """Control-plane bind settings.

    ``auth_token_env`` names an *environment variable*, never a token. The token
    itself is never written to configuration (NFR-3.5).
    """

    host: str = "127.0.0.1"
    port: int = 8765
    auth_token_env: str | None = None

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> ApiConfig:
        """Build an :class:`ApiConfig` from the ``[api]`` table."""
        _reject_unknown("api", data, _field_names(cls))
        defaults = _defaults(cls)
        port = _typed("api", data, "port", int, defaults["port"])
        if not 1 <= port <= 65535:
            raise ConfigError(
                f"api.port must be between 1 and 65535, got {port}",
                detail={"port": port},
            )
        return cls(
            host=_typed("api", data, "host", str, defaults["host"]),
            port=port,
            auth_token_env=_typed("api", data, "auth_token_env", str, None),
        )

    @property
    def is_local(self) -> bool:
        """Whether the configured host is loopback-only."""
        return self.host in _LOCAL_HOSTS

    def validate_binding(self, env: Mapping[str, str]) -> None:
        """Enforce NFR-3.4: a non-local bind requires a resolvable token.

        Args:
            env: The environment to resolve ``auth_token_env`` against.

        Raises:
            ConfigError: If the bind is non-local and no token is available.
        """
        if self.is_local:
            return
        if not self.auth_token_env:
            raise ConfigError(
                f"api.host = {self.host!r} is not loopback; set api.auth_token_env "
                "to the name of an environment variable holding the auth token",
                detail={"host": self.host},
            )
        if not env.get(self.auth_token_env):
            raise ConfigError(
                f"api.auth_token_env names {self.auth_token_env!r}, but that "
                "environment variable is unset or empty",
                detail={"host": self.host, "env_var": self.auth_token_env},
            )


@dataclass(frozen=True, slots=True)
class SecurityConfig:
    """Hardening policy for a deployed control plane.

    The defaults are the ones that are safe to be wrong about. A misconfigured
    installation should fail closed — refusing a request it should have served
    is recoverable in a way that serving one it should have refused is not.

    Attributes:
        allowed_hosts: ``Host`` header values this server answers to. Empty
            means any, which is only tolerable on loopback. A deployment
            reachable from a network must name its hosts, or a request routed
            to it by a rebinding attack looks identical to a real one.
        max_body_bytes: Largest request body accepted. A workflow definition is
            kilobytes; anything much larger is a mistake or an attempt.
        rate_limit_per_minute: Sustained request budget per client. ``0``
            disables rate limiting entirely.
        rate_limit_burst: How far above the sustained rate a client may spike.
        cors_origins: Browser origins allowed to call the API. Empty means
            none, and none is right: the dashboard is served from this same
            origin and needs no exemption.
        security_headers: Whether to send the hardening response headers.
        request_log: Whether to emit one structured line per request.
        shutdown_grace_s: How long in-flight work gets when stopping.
    """

    allowed_hosts: tuple[str, ...] = ()
    max_body_bytes: int = 1_048_576
    rate_limit_per_minute: int = 600
    rate_limit_burst: int = 120
    cors_origins: tuple[str, ...] = ()
    security_headers: bool = True
    request_log: bool = True
    shutdown_grace_s: float = 20.0

    def __post_init__(self) -> None:
        if self.max_body_bytes < 1024:
            raise ConfigError(
                f"security.max_body_bytes must be at least 1024, got "
                f"{self.max_body_bytes}",
                detail={"value": self.max_body_bytes},
            )
        if self.rate_limit_per_minute < 0:
            raise ConfigError(
                "security.rate_limit_per_minute must not be negative, got "
                f"{self.rate_limit_per_minute}"
            )
        if self.rate_limit_per_minute and self.rate_limit_burst < 1:
            raise ConfigError(
                "security.rate_limit_burst must be at least 1 when rate limiting "
                f"is enabled, got {self.rate_limit_burst}"
            )
        _positive("security", "shutdown_grace_s", self.shutdown_grace_s)
        for origin in self.cors_origins:
            if origin == "*":
                # A wildcard origin on an unauthenticated API lets any page on
                # the internet drive this operator's agents.
                raise ConfigError(
                    "security.cors_origins must not contain '*'; name the "
                    "origins that are allowed",
                    detail={"origins": list(self.cors_origins)},
                )

    @property
    def rate_limiting(self) -> bool:
        """Whether requests are rate limited."""
        return self.rate_limit_per_minute > 0

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> SecurityConfig:
        """Build a :class:`SecurityConfig` from the ``[security]`` table."""
        _reject_unknown("security", data, _field_names(cls))
        defaults = _defaults(cls)
        return cls(
            allowed_hosts=_string_list("security", data, "allowed_hosts"),
            max_body_bytes=_typed(
                "security", data, "max_body_bytes", int, defaults["max_body_bytes"]
            ),
            rate_limit_per_minute=_typed(
                "security",
                data,
                "rate_limit_per_minute",
                int,
                defaults["rate_limit_per_minute"],
            ),
            rate_limit_burst=_typed(
                "security", data, "rate_limit_burst", int, defaults["rate_limit_burst"]
            ),
            cors_origins=_string_list("security", data, "cors_origins"),
            security_headers=_typed(
                "security", data, "security_headers", bool, defaults["security_headers"]
            ),
            request_log=_typed(
                "security", data, "request_log", bool, defaults["request_log"]
            ),
            shutdown_grace_s=_typed(
                "security", data, "shutdown_grace_s", float, defaults["shutdown_grace_s"]
            ),
        )


def _string_map(
    section: str, data: Mapping[str, Any], key: str
) -> Mapping[str, str]:
    """Read a table of string-to-string pairs.

    Accepts a TOML inline table (``env = {PYTHONPATH = "src"}``) or a
    comma-separated ``KEY=VALUE`` string, because an environment-only
    deployment cannot express a table — the same reasoning as
    :func:`_string_list`.
    """
    if key not in data:
        return {}
    value = data[key]
    if isinstance(value, str):
        pairs: dict[str, str] = {}
        for item in value.split(","):
            entry = item.strip()
            if not entry:
                continue
            name, separator, raw = entry.partition("=")
            if not separator or not name.strip():
                raise ConfigError(
                    f"{section}.{key} entries must be KEY=VALUE, got {entry!r}",
                    detail={"section": section, "key": key, "entry": entry},
                )
            pairs[name.strip()] = raw.strip()
        return pairs
    if isinstance(value, Mapping) and all(
        isinstance(name, str) and isinstance(item, str) for name, item in value.items()
    ):
        return {str(name): str(item) for name, item in value.items()}
    raise ConfigError(
        f"{section}.{key} must be a table of strings or a comma-separated "
        "KEY=VALUE string",
        detail={"section": section, "key": key},
    )


def _string_list(section: str, data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """Read a list of strings, accepting a comma-separated string as well.

    The string form exists because environment variables cannot hold a list,
    and a deployment configured entirely through the environment is the normal
    case in a container.
    """
    if key not in data:
        return ()
    value = data[key]
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(item.strip() for item in value if item.strip())
    raise ConfigError(
        f"{section}.{key} must be a list of strings or a comma-separated string",
        detail={"section": section, "key": key},
    )


# --------------------------------------------------------------------------- #
# Root
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    """The fully-resolved configuration for one installation."""

    run: RunConfig = field(default_factory=RunConfig)
    providers: Mapping[str, ProviderConfig] = field(default_factory=dict)
    roles: Mapping[str, RoleConfig] = field(default_factory=dict)
    agent: AgentConfig = field(default_factory=AgentConfig)
    git: GitConfig = field(default_factory=GitConfig)
    gates: GatesConfig = field(default_factory=GatesConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    config_version: int = 1
    sources: tuple[Path, ...] = ()

    _KNOWN_SECTIONS = frozenset(
        {"run", "provider", "agent", "git", "gates", "api", "security"}
    )

    #: Top-level keys that are not sections. ``config_version`` records the
    #: shape a file was written for; see :mod:`orchestrator.ops.migrate`.
    _KNOWN_KEYS = frozenset({"config_version"})

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        sources: tuple[Path, ...] = (),
        env: Mapping[str, str] | None = None,
    ) -> OrchestratorConfig:
        """Validate a merged mapping into a configuration object.

        Args:
            data: The merged TOML mapping.
            sources: Files that contributed, for diagnostics.
            env: Environment used to resolve token references. Defaults to
                :data:`os.environ`.

        Returns:
            The validated configuration.

        Raises:
            ConfigError: On any unknown key, bad type, or violated invariant.
        """
        _reject_secrets(data)
        unknown = sorted(set(data) - cls._KNOWN_SECTIONS - cls._KNOWN_KEYS)
        if unknown:
            raise ConfigError(
                f"unknown top-level section(s): {', '.join(unknown)}",
                detail={"unknown": unknown, "known": sorted(cls._KNOWN_SECTIONS)},
            )

        provider_table = dict(_section(data, "provider"))
        role_table = provider_table.pop("roles", {})
        if not isinstance(role_table, Mapping):
            raise ConfigError("section [provider.roles] must be a table")

        providers = {
            name: ProviderConfig.parse(name, _section(provider_table, name))
            for name in provider_table
        }
        roles = {
            role: RoleConfig.parse(role, _section(role_table, role)) for role in role_table
        }

        api = ApiConfig.parse(_section(data, "api"))
        api.validate_binding(os.environ if env is None else env)

        return cls(
            run=RunConfig.parse(_section(data, "run")),
            providers=providers,
            roles=roles,
            agent=AgentConfig.parse(_section(data, "agent")),
            git=GitConfig.parse(_section(data, "git")),
            gates=GatesConfig.parse(_section(data, "gates")),
            api=api,
            security=SecurityConfig.parse(_section(data, "security")),
            config_version=_typed("", data, "config_version", int, 1),
            sources=sources,
        )

    def provider_for(self, role: str, *, provider: str = "anthropic") -> ProviderConfig:
        """Resolve the effective provider settings for a role.

        Args:
            role: A role name such as ``"planner"`` or ``"worker"``.
            provider: Which provider block to start from.

        Returns:
            The provider settings with any role overrides applied. Falls back to
            built-in defaults when the provider block is absent.
        """
        base = self.providers.get(provider, ProviderConfig())
        override = self.roles.get(role)
        return override.apply_to(base) if override is not None else base


def _read_toml(path: Path) -> Mapping[str, Any]:
    """Read and parse one TOML file.

    Raises:
        ConfigError: If the file cannot be read or is not valid TOML.
    """
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid TOML: {exc}", detail={"path": str(path)}
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"could not read {path}: {exc}", detail={"path": str(path)}
        ) from exc


#: The prefix an environment variable must carry to be read as configuration.
ENV_PREFIX: Final = "ORCHESTRATORPRO"

#: Separates path segments in an environment variable name. Two underscores,
#: because one would be ambiguous against keys that contain underscores —
#: ``ORCHESTRATORPRO_API_AUTH_TOKEN_ENV`` could mean four different things.
ENV_SEPARATOR: Final = "__"

_SECTION_TYPES: Final[Mapping[str, type]] = {
    "run": RunConfig,
    "agent": AgentConfig,
    "git": GitConfig,
    "gates": GatesConfig,
    "api": ApiConfig,
    "security": SecurityConfig,
}


def _annotation_of(path: tuple[str, ...]) -> str:
    """Return the declared annotation of the field a dotted path names.

    Note:
        This module uses ``from __future__ import annotations``, so a field's
        ``type`` is the annotation *as a string*. Comparing against the object
        would silently match nothing, which is exactly the sort of quiet
        failure that turns an integer into a string and a port into an error
        three layers away.
    """
    if len(path) == 2 and path[0] in _SECTION_TYPES:
        owner: type | None = _SECTION_TYPES[path[0]]
    elif len(path) == 3 and path[0] == "provider" and path[1] != "roles":
        owner = ProviderConfig
    elif len(path) == 4 and path[:2] == ("provider", "roles"):
        owner = RoleConfig
    else:
        return ""

    for declared in fields(owner):  # type: ignore[arg-type]
        if declared.name == path[-1]:
            annotation = declared.type
            return annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    return ""


def _coerce_env(path: tuple[str, ...], raw: str) -> Any:
    """Convert an environment string to the type its field expects.

    An environment variable is always a string. Coercing by the declared type
    rather than by guessing means ``ORCHESTRATORPRO__API__HOST=0.0.0.0`` stays a
    string and ``__PORT=8080`` becomes an integer, without either having to be
    special-cased. A list-valued field stays a string too: its parser already
    accepts the comma-separated form, which is the only form an environment
    variable can carry.
    """
    annotation = _annotation_of(path)
    expected = {"bool": bool, "int": int, "float": float}.get(annotation)
    text = raw.strip()

    if expected is bool:
        lowered = text.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(
            f"{'.'.join(path)} must be a boolean, got {raw!r}",
            detail={"key": ".".join(path), "value": raw},
        )
    if expected is int:
        try:
            return int(text)
        except ValueError:
            raise ConfigError(
                f"{'.'.join(path)} must be an integer, got {raw!r}",
                detail={"key": ".".join(path), "value": raw},
            ) from None
    if expected is float:
        try:
            return float(text)
        except ValueError:
            raise ConfigError(
                f"{'.'.join(path)} must be a number, got {raw!r}",
                detail={"key": ".".join(path), "value": raw},
            ) from None
    return text


def env_overlay(env: Mapping[str, str], *, prefix: str = ENV_PREFIX) -> dict[str, Any]:
    """Read configuration out of the environment.

    ``ORCHESTRATORPRO__API__PORT=8080`` becomes ``{"api": {"port": 8080}}``.
    This is how a container is configured: a deployment that must ship a file
    into an image to change a port is a deployment nobody enjoys operating.

    Args:
        env: The environment to read.
        prefix: The variable prefix to look for.

    Returns:
        A nested mapping suitable for merging under the file layers.

    Raises:
        ConfigError: If a value cannot be coerced to its field's type.
    """
    head = f"{prefix}{ENV_SEPARATOR}"
    overlay: dict[str, Any] = {}

    for name in sorted(env):
        if not name.startswith(head):
            continue
        raw = env[name]
        if raw == "":
            # An empty variable means "not set". Docker Compose materializes
            # every declared variable, so treating empty as a value would make
            # a blank line in .env override a real configuration file.
            continue

        path = tuple(part.lower() for part in name[len(head) :].split(ENV_SEPARATOR) if part)
        if not path:
            continue

        cursor = overlay
        for segment in path[:-1]:
            existing = cursor.get(segment)
            if not isinstance(existing, dict):
                existing = {}
                cursor[segment] = existing
            cursor = existing
        cursor[path[-1]] = _coerce_env(path, raw)

    return overlay


def load_config(
    repo_root: Path | None = None,
    *,
    user_config: Path | None = None,
    env: Mapping[str, str] | None = None,
    use_env: bool = True,
) -> OrchestratorConfig:
    """Load configuration from the user, repository, and environment layers.

    Missing files are not an error — an installation with no configuration at
    all is valid and runs on defaults.

    Layers apply in order, later overriding earlier: user file, repository file,
    environment. The environment wins because it is the layer a deployment
    controls without rebuilding anything.

    Args:
        repo_root: Directory containing ``orchestrator.toml``. Omit to skip the
            repository layer.
        user_config: Path to the user-level file. Defaults to
            ``~/.orchestratorpro/config.toml``.
        env: Environment used for overrides and to resolve token references.
            Defaults to :data:`os.environ`.
        use_env: Whether to read ``ORCHESTRATORPRO__*`` overrides at all.

    Returns:
        The merged, validated configuration.

    Raises:
        ConfigError: If any contributing layer is unreadable, malformed, or
            violates an invariant.
    """
    environment = os.environ if env is None else env
    layers: list[Mapping[str, Any]] = []
    sources: list[Path] = []

    user_path = _USER_CONFIG_PATH if user_config is None else user_config
    if user_path.is_file():
        layers.append(_read_toml(user_path))
        sources.append(user_path)

    if repo_root is not None:
        repo_path = repo_root / _REPO_CONFIG_NAME
        if repo_path.is_file():
            layers.append(_read_toml(repo_path))
            sources.append(repo_path)

    if use_env:
        overlay = env_overlay(environment)
        if overlay:
            layers.append(overlay)
            sources.append(Path(f"<environment:{ENV_PREFIX}{ENV_SEPARATOR}*>"))

    merged: Mapping[str, Any] = {}
    for layer in layers:
        merged = _merge(merged, layer)

    return OrchestratorConfig.from_mapping(merged, sources=tuple(sources), env=environment)
