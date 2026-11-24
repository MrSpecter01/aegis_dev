#!/usr/bin/env bash
# Aegis — Ollama Model Pull Script
# Pulls the required LLM models for Aegis agents.
#
# Usage:
#   Dockerized Ollama:  docker exec aegis-ollama bash /scripts/pull_models.sh
#   Local Ollama:       bash deploy/ollama/pull_models.sh
#
# This script waits for the Ollama API to be ready, then pulls the
# default model (llama3.1:8b) used by the investigator agents.

set -euo pipefail

OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
MODEL="${1:-llama3.1:8b}"
MAX_RETRIES=30
RETRY_INTERVAL=5

echo "=== Aegis — Ollama Model Pull ==="
echo "Host: ${OLLAMA_HOST}"
echo "Model: ${MODEL}"
echo ""

# --- Wait for Ollama API to be ready ---
echo "Waiting for Ollama API..."
retries=0
until curl -sf "${OLLAMA_HOST}/api/tags" > /dev/null 2>&1; do
    retries=$((retries + 1))
    if [ "${retries}" -ge "${MAX_RETRIES}" ]; then
        echo "ERROR: Ollama API not ready after $((MAX_RETRIES * RETRY_INTERVAL))s"
        echo "Ensure Ollama is running at ${OLLAMA_HOST}"
        exit 1
    fi
    echo "  Attempt ${retries}/${MAX_RETRIES} — retrying in ${RETRY_INTERVAL}s..."
    sleep "${RETRY_INTERVAL}"
done
echo "Ollama API is ready."
echo ""

# --- Pull model ---
echo "Pulling model: ${MODEL}..."
ollama pull "${MODEL}"
echo ""

# --- Verify model is available ---
echo "Verifying model is listed..."
if ollama list | grep -q "${MODEL}"; then
    echo "SUCCESS: ${MODEL} is available."
else
    echo "WARNING: Model pull completed but '${MODEL}' not found in 'ollama list'."
    echo "Available models:"
    ollama list
    exit 1
fi

echo ""
echo "=== Model pull complete ==="
