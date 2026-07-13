#!/bin/bash
# WebSocket with HTTP/2 + curl --http2-prior-knowledge
python3 -c "
import http.client, ssl, base64, os
host = 'guaritradbot.13.140.181.29.sslip.io'
port = 443
key = base64.b64encode(os.urandom(16)).decode()
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
conn = http.client.HTTPSConnection(host, port, context=ctx)
conn.request('GET', '/ws/live?token=test', headers={
    'Host': host,
    'Upgrade': 'websocket',
    'Connection': 'Upgrade',
    'Sec-WebSocket-Key': key,
    'Sec-WebSocket-Version': '13',
    'Origin': 'https://guaritradbot-dash.13.140.181.29.sslip.io',
})
resp = conn.getresponse()
print('Status:', resp.status, resp.reason)
for k, v in resp.getheaders():
    print(f'  {k}: {v}')
body = resp.read()
print('Body:', body[:500])
"
echo
echo "=== Test WebSocket via the bot container directly (skip Traefik) ==="
CID=$(docker ps --filter name=guaritradbot --format "{{.Names}}" | grep -v dash | head -1)
echo "Bot container has Python but no curl. Check if /ws/live is bound inside:"
docker exec "$CID" python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect(('localhost', 8080))
    s.sendall(b'GET /ws/live?token=test HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGVzdA==\r\nSec-WebSocket-Version: 13\r\n\r\n')
    data = s.recv(2048).decode(errors='replace')
    print('Direct (port 8080) response:')
    print(data[:800])
finally:
    s.close()
" 2>&1
