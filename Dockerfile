# OrchestratorPro.
#
# Two stages: one that builds a wheel, one that runs it. The runtime image
# carries no compiler, no source tree, and no package index credentials —
# only the installed distribution and its dependencies.
#
# Three things this image does that are worth stating, because each is a
# decision rather than a default:
#
#   * It runs as a non-root user. An orchestrator executes commands on behalf
#     of a model; running that as root inside a container that mounts a
#     repository is a bad trade for a saved chmod.
#   * It binds to 127.0.0.1 by default. Publishing the port is the operator's
#     explicit act, made in compose or on the command line, not something the
#     image assumes on their behalf. This build has no authentication.
#   * Its state lives on a volume. The event log is the system's memory; a
#     container that loses it on recreate has lost every run it ever did.

# --- build -------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

RUN python -m pip install --upgrade "pip>=24" "build>=1.2"

# Metadata first, so a source-only change does not re-resolve dependencies.
COPY pyproject.toml README.md LICENSE ./
COPY orchestrator ./orchestrator

RUN python -m build --wheel --outdir /dist

# --- runtime -----------------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="OrchestratorPro" \
      org.opencontainers.image.description="A self-hosted control plane for fleets of AI coding agents." \
      org.opencontainers.image.source="https://github.com/emailmaomao/OrchestratorPro" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    ORCHESTRATORPRO_HOME=/var/lib/orchestratorpro

# git is a runtime dependency, not a build one: every attempt gets a worktree.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin orchestrator \
    && mkdir -p "${ORCHESTRATORPRO_HOME}" /workspace \
    && chown -R orchestrator:orchestrator "${ORCHESTRATORPRO_HOME}" /workspace

COPY --from=build /dist/*.whl /tmp/
RUN python -m pip install /tmp/*.whl && rm -f /tmp/*.whl

USER orchestrator
WORKDIR /workspace

# The database and any backups live here. Declared so that `docker run` without
# a compose file still keeps the log across a recreate.
VOLUME ["/var/lib/orchestratorpro"]

EXPOSE 8765

# Uses the CLI's own check rather than curl, which is not installed and should
# not be: a healthcheck that needs an extra package is a healthcheck that
# quietly stops working when the base image slims down.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys,os; \
port=os.environ.get('ORCHESTRATORPRO__API__PORT','8765'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=4).status==200 else 1)"

# Global options precede the subcommand: `--database` belongs to the program,
# `--workspace` to `serve`. Getting this backwards makes the container exit
# with a usage error, which is how it was found.
ENTRYPOINT ["orchestratorpro"]
CMD ["--database", "/var/lib/orchestratorpro/runs.db", "serve", "--workspace", "/workspace"]
