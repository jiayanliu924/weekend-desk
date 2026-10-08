#!/usr/bin/env bash
# 网页看板：https（Caddy 自动申请证书）+ 常驻服务。可重复运行。
set -e
DEST=/opt/weekend-desk
IP=$(curl -fsS -m 5 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address || curl -fsS -m 5 https://api.ipify.org)
HOST="$(echo "$IP" | tr . -).sslip.io"

grep -q '^WEB_SECRET=' "$DEST/.env" || echo "WEB_SECRET=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> "$DEST/.env"

cat > /etc/systemd/system/weekend-desk-web.service <<UNIT
[Unit]
Description=Weekend Desk web dashboard
After=network-online.target

[Service]
WorkingDirectory=$DEST
ExecStart=$DEST/venv/bin/uvicorn desk.web:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

if ! command -v caddy >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy >/dev/null
fi
cat > /etc/caddy/Caddyfile <<CADDY
$HOST {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
CADDY

systemctl daemon-reload
systemctl enable weekend-desk-web >/dev/null 2>&1
systemctl restart weekend-desk-web
systemctl enable caddy >/dev/null 2>&1
systemctl restart caddy
echo "网页地址：https://$HOST"
echo "创建账号：cd $DEST && ./venv/bin/python -m desk adduser 你的名字"
