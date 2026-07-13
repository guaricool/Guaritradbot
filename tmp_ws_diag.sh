#!/bin/bash
# Sprint 55 followup: deeper WebSocket diagnosis
echo "=== POST /api/auth/login (clean) ==="
curl -s -o /tmp/r.out -w "HTTP %{http_code}\n" \
  -X POST -H "Origin: https://guaritradbot-dash.13.140.181.29.sslip.io" \
  -H "Content-Type: application/json" -d '{"password":"test"}' \
  "https://guaritradbot.13.140.181.29.sslip.io/api/auth/login"
echo "Response body:"
head -c 200 /tmp/r.out
echo
echo
echo "=== WebSocket test with full headers (HTTP/1.1 fallback) ==="
python3 -c "
import socket, ssl, base64, os
host = 'guaritradbot.13.140.181.29.sslip.io'
port = 443
key = base64.b64encode(os.urandom(16)).decode()
req = (
    f'GET /ws/live?token=test HTTP/1.1\r\n'
    f'Host: {host}\r\n'
    f'Upgrade: websocket\r\n'
    f'Connection: Upgrade\r\n'
    f'Sec-WebSocket-Key: {key}\r\n'
    f'Sec-WebSocket-Version: 13\r\n'
    f'\r\n'
)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
with socket.create_connection((host, port), timeout=5) as s:
    s = ctx.wrap_socket(s, server_hostname=host)
    s.sendall(req.encode())
    data = s.recv(4096).decode(errors='replace')
    print('--- full response ---')
    print(data[:2000])
" 2>&1
echo
echo "=== Look for Traefik config in Coolify's app definition ==="
# Coolify stores per-app config in its DB; the easiest way to inspect
# is via the API: GET /api/v1/applications/{uuid} returns the docker compose
# Coolify generated, which may include labels that enable WebSocket routing.
TOKEN="9|yqNYDjMmh0t48pQYgbWOXVpDx4iuyPmG8ElJDL7Zab3c1f67"
APP="wyn2ah6rflg6ufwzpvzk436f"
curl -s -H "Authorization: Bearer ${TOKEN}" -H "Accept: application/json" \
  "http://localhost:8000/api/v1/applications/${APP}" \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
# Print only the fields most likely to contain the WebSocket config
for key in ('docker_compose_raw', 'docker_compose', 'labels', 'settings', 'config'):
    v = d.get(key)
    if v:
        s = json.dumps(v) if not isinstance(v, str) else v
        if 'websocket' in s.lower() or 'ws://' in s.lower() or 'upgrade' in s.lower() or 'traefik' in s.lower():
            print(f'=== {key} (WebSocket-relevant) ===')
            print(s[:3000])
            print()
print('=== full app keys ===')
print(sorted(d.keys()))
" 2>&1 | head -60
