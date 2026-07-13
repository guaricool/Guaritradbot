#!/bin/bash
echo "=== ALL containers including exited (look for Traefik) ==="
docker ps -a --no-trunc --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo
echo "=== ports listening on the host ==="
ss -tlnp 2>/dev/null | head -30 || netstat -tlnp 2>/dev/null | head -30
echo
echo "=== iptables rules for port 443 / 80 ==="
iptables -L -n 2>/dev/null | grep -E "(:443|:80)" | head -10 || echo "(no iptables hits)"
echo
echo "=== test direct to bot's exposed port 8080 (Traefik normally proxies here) ==="
curl -sv -o /dev/null -w "HTTP %{http_code} | total=%{time_total}s\n" \
  -H "Origin: https://guaritradbot-dash.13.140.181.29.sslip.io" \
  -H "Access-Control-Request-Method: POST" \
  -X OPTIONS \
  "http://13.140.181.29:8080/api/auth/login" 2>&1 | tail -10
echo
echo "=== /api/login direct (no auth) ==="
curl -sv -X POST -H "Origin: https://guaritradbot-dash.13.140.181.29.sslip.io" \
  -H "Content-Type: application/json" -d '{"password":"test"}' \
  "http://13.140.181.29:8080/api/auth/login" 2>&1 | tail -15
echo
echo "=== /api/audit?limit=5 direct (no auth, no CORS) ==="
curl -sv "http://13.140.181.29:8080/api/audit?limit=5" 2>&1 | tail -10
echo
echo "=== check what is listening on 443 ==="
ss -tlnp 2>/dev/null | grep ":443 " || netstat -tlnp 2>/dev/null | grep ":443 "
echo
echo "=== check via public https (worked? 404 OK or no?) ==="
curl -sv -o /tmp/curl_root.out -w "HTTP %{http_code}\n" "https://guaritradbot.13.140.181.29.sslip.io/" 2>&1 | grep -E "HTTP |Server:|Location:"
head -3 /tmp/curl_root.out 2>/dev/null
