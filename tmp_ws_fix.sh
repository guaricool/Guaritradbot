#!/bin/bash
# Sprint 55 followup: PATCH Coolify app definition to add a dedicated
# WebSocket router (without gzip middleware, which breaks upgrade).
#
# The pre-fix config has the bot on a single router with gzip
# middleware. When the browser sends `Upgrade: websocket`, Traefik
# 403s. The fix: add a SECOND router dedicated to /ws/ paths that
# does NOT apply gzip. Traefik routes by longest-path-prefix match
# first, so /ws/ will go to the new router while everything else
# stays on the gzip-enabled one.
TOKEN="9|yqNYDjMmh0t48pQYgbWOXVpDx4iuyPmG8ElJDL7Zab3c1f67"
APP="wyn2ah6rflg6ufwzpvzk436f"
API="http://localhost:8000/api/v1"

echo "=== current full labels ==="
curl -s -H "Authorization: Bearer ${TOKEN}" -H "Accept: application/json" \
  "${API}/applications/${APP}" \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
dc = d.get('docker_compose_raw') or d.get('docker_compose') or ''
if dc:
    # Find the labels section
    in_labels = False
    for line in dc.split('\n'):
        if 'labels:' in line:
            in_labels = True
            print(line)
        elif in_labels and line.startswith('      - '):
            print(line)
        elif in_labels and line.strip() == '':
            in_labels = False
" 2>&1
echo
echo "=== Coolify app shape ==="
curl -s -H "Authorization: Bearer ${TOKEN}" -H "Accept: application/json" \
  "${API}/applications/${APP}" \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
for k in sorted(d.keys()):
    v = d.get(k)
    if isinstance(v, str) and len(v) > 200:
        print(f'  {k}: <str len={len(v)}>')
    elif isinstance(v, (dict, list)):
        print(f'  {k}: <{type(v).__name__} len={len(v)}>')
    else:
        print(f'  {k}: {v}')
" 2>&1 | head -30
