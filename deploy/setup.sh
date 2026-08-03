#!/bin/bash
# One-shot deployment: eoxs-ingestion systemd service + nginx reverse proxy
# + a Let's Encrypt IP-address certificate for the VPS's public IP (no
# domain needed -- see letsencrypt.org/2026/01/15/6day-and-ip-general-availability).
# Run as: sudo bash deploy/setup.sh
set -euo pipefail

REPO_DIR="/home/deploy/eoxs-wiki-db"
PUBLIC_IP="5.223.44.95"
CONTACT_EMAIL="eoxs.innovation@gmail.com"

echo "== 1/12: installing eoxs-ingestion systemd service =="
cp "$REPO_DIR/deploy/eoxs-ingestion.service" /etc/systemd/system/eoxs-ingestion.service
systemctl daemon-reload
systemctl enable --now eoxs-ingestion
sleep 2
systemctl is-active --quiet eoxs-ingestion && echo "eoxs-ingestion: active" || { echo "eoxs-ingestion FAILED to start"; systemctl status eoxs-ingestion --no-pager; exit 1; }

echo "== 2/12: opening 80/443 in ufw (default-deny incoming; only 22 was allowed) =="
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw reload
else
  echo "ufw not active -- skipping (nothing to open)"
fi

echo "== 3/12: installing nginx =="
apt-get update -qq
apt-get install -y -qq nginx

echo "== 4/12: installing certbot via snap (apt's certbot is 2.9, too old for --ip-address) =="
snap install --classic certbot
ln -sf /snap/bin/certbot /usr/local/bin/certbot

echo "== 5/12: phase 1 -- HTTP-only nginx config, to serve the ACME challenge =="
mkdir -p /var/www/certbot
cp "$REPO_DIR/deploy/nginx-http-only.conf" /etc/nginx/sites-available/eoxs-ingestion
ln -sf /etc/nginx/sites-available/eoxs-ingestion /etc/nginx/sites-enabled/eoxs-ingestion
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx || systemctl restart nginx

echo "== 6/12: requesting the IP-address certificate (valid ~6-7 days, auto-renews) =="
certbot certonly --webroot -w /var/www/certbot \
  --ip-address "$PUBLIC_IP" --preferred-profile shortlived \
  --agree-tos --non-interactive -m "$CONTACT_EMAIL"

echo "== 7/12: phase 2 -- HTTPS nginx config + reload-hook so nginx picks up each renewal =="
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cp "$REPO_DIR/deploy/reload-nginx.sh" /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
cp "$REPO_DIR/deploy/nginx-https.conf" /etc/nginx/sites-available/eoxs-ingestion
nginx -t
systemctl reload nginx

echo "== 8/12: confirming the snap-bundled renewal timer (runs 2x/day, well under the 3-day requirement) =="
systemctl list-timers snap.certbot.renew.timer --no-pager || echo "WARNING: snap.certbot.renew.timer not found -- check manually"

echo "== 9/12: installing the 2-hourly full-sweep cron-fallback timer =="
cp "$REPO_DIR/deploy/eoxs-sweep.service" /etc/systemd/system/eoxs-sweep.service
cp "$REPO_DIR/deploy/eoxs-sweep.timer" /etc/systemd/system/eoxs-sweep.timer
systemctl daemon-reload
systemctl enable --now eoxs-sweep.timer
systemctl list-timers eoxs-sweep.timer --no-pager

echo "== 10/12: installing the 6-hourly wiki-ingestion pipeline timer (Phase 3->4->5: detect+ingest, consolidate, review) =="
cp "$REPO_DIR/deploy/eoxs-wiki-pipeline.service" /etc/systemd/system/eoxs-wiki-pipeline.service
cp "$REPO_DIR/deploy/eoxs-wiki-pipeline.timer" /etc/systemd/system/eoxs-wiki-pipeline.timer
systemctl daemon-reload
systemctl enable --now eoxs-wiki-pipeline.timer
systemctl list-timers eoxs-wiki-pipeline.timer --no-pager

echo "== 11/12: installing the MCP HTTP server (remote connector) behind nginx at /mcp/ =="
cp "$REPO_DIR/deploy/eoxs-mcp.service" /etc/systemd/system/eoxs-mcp.service
systemctl daemon-reload
systemctl enable --now eoxs-mcp
sleep 2
systemctl is-active --quiet eoxs-mcp && echo "eoxs-mcp: active" || { echo "eoxs-mcp FAILED to start"; systemctl status eoxs-mcp --no-pager; exit 1; }

echo
echo "Done. Verify with:"
echo "  curl https://$PUBLIC_IP/health"
echo "  curl -X POST https://$PUBLIC_IP/mcp/ -H 'Authorization: Bearer <MCP_HTTP_TOKEN>' -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"clientInfo\":{\"name\":\"test\",\"version\":\"1\"}}}'"
echo "  sudo ufw status   # confirm 80/443 are reachable if ufw is active"
