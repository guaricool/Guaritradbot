#!/bin/bash
echo "=== new container ==="
docker ps --filter name=guaritradbot --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
echo
echo "=== WebSocket test through Traefik ==="
python3 << 'PYEOF'
import socket, ssl, base64, os
host = 'guaritradbot.13.140.181.29.sslip.io'
port = 443
key = base64.b64encode(os.urandom(16)).decode()
req = (
    'GET /ws/live?token=wrongtoken HTTP/1.1\r\n'
    f'Host: {host}\r\n'
    'Upgrade: websocket\r\n'
    'Connection: Upgrade\r\n'
    f'Sec-WebSocket-Key: {key}\r\n'
    'Sec-WebSocket-Version: 13\r\n'
    '\r\n'
)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
with socket.create_connection((host, port), timeout=8) as s:
    s = ctx.wrap_socket(s, server_hostname=host)
    s.sendall(req.encode())
    data = s.recv(2048).decode(errors='replace')
    print(data[:1000])
PYEOF
