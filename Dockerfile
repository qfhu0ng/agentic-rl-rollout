# One-task MVP: Codex CLI -> LiteLLM Proxy -> OpenAI, with JSONL logging.

# Base image can be overridden, e.g. to use a CN mirror in restricted networks:
#   docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/node:22-bullseye ...
ARG BASE_IMAGE=node:22-bullseye
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        git \
        build-essential \
        curl \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
# PIP_INDEX_URL is overridable at build time. The default below is the
# Tsinghua mirror, which is fast from mainland China; override with
#   docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple ...
# in unrestricted networks.
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip3 install --default-timeout=180 -i "${PIP_INDEX_URL}" -r /app/requirements.txt

# Codex CLI. If the upstream package name changes, override at build time:
#   docker build --build-arg CODEX_NPM_PKG=@openai/codex ...
ARG CODEX_NPM_PKG=@openai/codex
ARG NPM_REGISTRY=https://registry.npmmirror.com
# The @openai/codex wrapper depends on platform-specific binary subpackages via
# `optionalDependencies` aliased to versioned tags
# (e.g. @openai/codex-linux-arm64: "npm:@openai/codex@<ver>-linux-arm64").
# Global `npm i -g` consistently skips these aliased optionals, so we install
# the wrapper first, then explicitly inject the native binary into the
# wrapper's own node_modules.
# Override CODEX_NATIVE_SUFFIX for other platforms (e.g. linux-x64).
ARG CODEX_NATIVE_SUFFIX=linux-arm64
RUN npm config set registry "${NPM_REGISTRY}" \
 && npm install -g "${CODEX_NPM_PKG}" \
 && grep -m1 '"version"' /usr/local/lib/node_modules/@openai/codex/package.json \
        | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/' \
        > /tmp/codex_ver \
 && cd /usr/local/lib/node_modules/@openai/codex \
 && npm install --no-save --no-package-lock \
        "@openai/codex-${CODEX_NATIVE_SUFFIX}@npm:@openai/codex@$(cat /tmp/codex_ver)-${CODEX_NATIVE_SUFFIX}" \
 && ls node_modules/@openai/

COPY litellm_config.yaml /app/litellm_config.yaml
COPY custom_callbacks.py /app/custom_callbacks.py
COPY run_task.sh        /app/run_task.sh
COPY README.md          /app/README.md

RUN chmod +x /app/run_task.sh \
 && mkdir -p /logs /workspace /root/.codex

ENV PYTHONPATH=/app \
    LOG_ROOT=/logs

CMD ["/app/run_task.sh"]
