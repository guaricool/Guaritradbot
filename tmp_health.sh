#!/bin/bash
echo "=== ALL containers (any name) ==="
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
echo
echo "=== ALL containers including stopped ==="
docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
echo
echo "=== Traefik ==="
docker ps --filter name=traefik --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo
echo "=== Test bot API directly from inside the bot container ==="
CID=$(docker ps --filter name=guaritradbot --format "{{.Names}}" | grep -v dash | head -1)
echo "Bot CID: $CID"
docker exec "$CID" curl -sv http://localhost:8080/api/healthz 2>&1 | tail -15
echo
echo "=== Test from outside (public hostname) ==="
curl -sv -o /dev/null -w "HTTP %{http_code} | total=%{time_total}s | connect=%{time_connect}s\n" \
  "https://guaritradbot.13.140.181.29.sslip.io/api/healthz" 2>&1 | tail -5
echo
echo "=== WebSocket test (HEAD/upgrade probe) ==="
curl -sv -o /dev/null -w "HTTP %{http_code}\n" \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Sec-WebSocket-Version: 13" \
  "https://guaritradbot.13.140.181.29.sslip.io/ws/live" 2>&1 | tail -5
