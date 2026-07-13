#!/bin/bash
# Sprint 55 followup: verify all endpoints + WebSocket after Sprint 55 deploy
sleep 20  # give Coolify Traefik time to mark container as healthy
echo "=== /api/auth/login (POST) ==="
curl -sv -X POST -H "Origin: https://guaritradbot-dash.13.140.181.29.sslip.io" \
  -H "Content-Type: application/json" -d '{"password":"test"}' \
  "https://guaritradbot.13.140.181.29.sslip.io/api/auth/login" 2>&1 | grep -E "HTTP|access-control|content-type" | head -10
echo
echo "=== WebSocket real test (Python) ==="
python3 -c "
import socket, ssl, base64, os, urllib.parse
host = 'guaritradbot.13.140.181.29.sslip.io'
port = 443
# Build the WebSocket upgrade request
key = base64.b64encode(os.urandom(16)).decode()
req = (
    f'GET /ws/live HTTP/1.1\r\n'
    f'Host: {host}\r\n'
    f'Upgrade: websocket\r\n'
    f'Connection: Upgrade\r\n'
    f'Sec-WebSocket-Key: {key}\r\n'
    f'Sec-WebSocket-Version: 13\r\n'
    f'Origin: https://guaritradbot-dash.13.140.181.29.sslip.io\r\n'
    f'\r\n'
)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
with socket.create_connection((host, port), timeout=5) as s:
    s = ctx.wrap_socket(s, server_hostname=host)
    s.sendall(req.encode())
    data = s.recv(4096).decode(errors='replace')
    # First response line
    status_line = data.split('\r\n')[0]
    headers = data.split('\r\n\r\n')[0]
    print('Status line:', status_line)
    print('--- relevant headers ---')
    for line in headers.split('\r\n')[1:]:
        if line.lower().startswith(('sec-', 'upgrade', 'connection', 'http', 'access-control')):
            print(' ', line)
" 2>&1
