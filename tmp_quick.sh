#!/bin/bash
# Quick clean test of bot endpoints after Sprint 55 deploy
echo "=== Container status ==="
docker ps --filter name=guaritradbot --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
echo
echo "=== POST /api/auth/login (clean test, no -v) ==="
curl -s -o /tmp/r.out -w "HTTP %{http_code} | time=%{time_total}s\n" \
  -X POST -H "Origin: https://guaritradbot-dash.13.140.181.29.sslip.io" \
  -H "Content-Type: application/json" -d '{"password":"test"}' \
  "https://guaritradbot.13.140.181.29.sslip.io/api/auth/login"
cat /tmp/r.out 2>/dev/null
echo
echo
echo "=== GET /api/audit (no auth) ==="
curl -s -o /tmp/r.out -w "HTTP %{http_code}\n" \
  -H "Origin: https://guaritradbot-dash.13.140.181.29.sslip.io" \
  "https://guaritradbot.13.140.181.29.sslip.io/api/audit?limit=5"
cat /tmp/r.out 2>/dev/null
echo
echo
echo "=== WebSocket headers we get back ==="
python3 -c "
import socket, ssl, base64, os
host = 'guaritradbot.13.140.181.29.sslip.io'
port = 443
key = base64.b64encode(os.urandom(16)).decode()
req = (
    f'GET /ws/live HTTP/1.1\r\n'
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
    data = s.recv(8192).decode(errors='replace')
    print(data)
" 2>&1
