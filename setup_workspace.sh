#!/usr/bin/env bash
# Run this once after cloning the repo to set up the flask workspace.
set -euo pipefail

BASE_COMMIT="182ce3dd15dfa3537391c3efaf9c3ff407d134d4"

echo "[setup] Cloning pallets/flask at base_commit ${BASE_COMMIT}..."
mkdir -p workspace
git clone https://github.com/pallets/flask.git workspace/flask
git -C workspace/flask checkout "${BASE_COMMIT}"
echo "[setup] Done. workspace/flask is ready."
echo ""
echo "Next: docker build -t one-task-litellm-codex-logger:latest ."
echo "Then: see README.md for docker run command."
