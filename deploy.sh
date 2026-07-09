#!/bin/bash
# Deploy Guaritradbot via Coolify local API.
# Token: coolify service token for the app.
set -e
COOLIFY_URL="http://localhost:8080"
TOKEN="9|yqNYDjMmh0t48pQYgbWOXVpDx4iuyPmG8ElJDL7Zab3c1f67"
UUID="wyn2ah6rflg6ufwzpvzk436f"

echo "[deploy] POST ${COOLIFY_URL}/api/v1/deploy uuid=${UUID}"
curl -sS -X POST "${COOLIFY_URL}/api/v1/deploy" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"uuid\":\"${UUID}\",\"force\":true,\"instant_deploy\":true}"
echo
echo "[deploy] done"
