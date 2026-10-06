# image/Dockerfile: the sandbox image recipe for amap-deploy-openshell.
#
# The operator builds this image; nothing here is built or pushed by this
# repository. The recipe holds no credentials. OpenShell ships no Claude Code
# image, so this is the recipe for one. The model it follows is
# OpenShell main@acbac9c:examples/bring-your-own-container/Dockerfile
#
# Identity. With no `process.run_as_user` in the policy, the Docker and Podman
# drivers use the image's `USER`, and the image's `WORKDIR` /sandbox is the
# workspace (OpenShell main@acbac9c:
# docs/how-it-works/sandboxes/runtimes.mdx:265-275). With a numeric
# `run_as_user`, HOME is /sandbox (OpenShell main@acbac9c:
# crates/openshell-sandbox/src/process.rs:217-241), so Claude Code's session
# files are under /sandbox/.claude. The account is one uid:gid, the same as
# the router container's, and the build refuses an empty, non-numeric or zero
# value.
#
# There is no CMD and no ENTRYPOINT: the main process is the command after
# `--` on `openshell sandbox create` (OpenShell main@acbac9c:
# docs/how-it-works/sandboxes/overview.mdx:23). The payload arrives by mount,
# so there is no COPY or ADD either.
#
# Build, with the Claude Code version the operator has measured and recorded:
#
#   docker build --build-arg CLAUDE_CODE_VERSION=<version> \
#     --build-arg SANDBOX_UID="$(id -u)" --build-arg SANDBOX_GID="$(id -g)" \
#     -t amap-openshell-agent image/
FROM node:22-bookworm-slim
ARG CLAUDE_CODE_VERSION
ARG SANDBOX_UID
ARG SANDBOX_GID
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 bash \
    && rm -rf /var/lib/apt/lists/*
RUN : "${CLAUDE_CODE_VERSION:?build with --build-arg CLAUDE_CODE_VERSION=<version>}" \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"
RUN set -eu; \
    case "${SANDBOX_UID:-}" in ''|*[!0-9]*) echo "image/Dockerfile: SANDBOX_UID must be a number" >&2; exit 1;; esac; \
    case "${SANDBOX_GID:-}" in ''|*[!0-9]*) echo "image/Dockerfile: SANDBOX_GID must be a number" >&2; exit 1;; esac; \
    if [ "$SANDBOX_UID" -eq 0 ] || [ "$SANDBOX_GID" -eq 0 ]; then echo "image/Dockerfile: the sandbox user must not be root" >&2; exit 1; fi; \
    userdel node 2>/dev/null || true; groupdel node 2>/dev/null || true; \
    getent group "$SANDBOX_GID" >/dev/null || groupadd --gid "$SANDBOX_GID" agent; \
    useradd --uid "$SANDBOX_UID" --gid "$SANDBOX_GID" --no-create-home --home-dir /sandbox --shell /bin/bash agent; \
    install -d -o "$SANDBOX_UID" -g "$SANDBOX_GID" /sandbox
WORKDIR /sandbox
USER agent
