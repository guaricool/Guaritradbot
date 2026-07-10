#!/bin/bash
# Deploy Guaritradbot via Coolify local API.
#
# Token: read from COOLIFY_TOKEN environment variable. NEVER hardcode it
# in this file (the repo is public on GitHub and tokens get scraped within
# hours of being committed). See .env.example for the env var name.
#
# To deploy from a fresh machine:
#   1. Set COOLIFY_TOKEN in your shell: `export COOLIFY_TOKEN=...`
#   2. Run: `bash deploy.sh`
set -e

# Abort with a clear error if the env var is missing.
: "${COOLIFY_TOKEN:?COOLIFY_TOKEN env var is required. Set it in your shell or .env (NOT in this script).}"

COOLIFY_URL="http://localhost:8080"
UUID="wyn2ah6rflg6ufwzpvzk436f"

echo "[deploy] POST ${COOLIFY_URL}/api/v1/deploy uuid=${UUID}"
curl -sS -X POST "${COOLIFY_URL}/api/v1/deploy" \
  -H "Authorization: Bearer ${COOLIFY_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"uuid\":\"${UUID}\",\"force\":true,\"instant_deploy\":true}"
echo
echo "[deploy] done"
