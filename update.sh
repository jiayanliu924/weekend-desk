#!/usr/bin/env bash
# 在服务器上更新到 GitHub 最新版本（保留数据、配置和 .env）
set -e
cd /root/src
rm -rf weekend-desk-main
curl -fsSL https://github.com/jiayanliu924/weekend-desk/archive/refs/heads/main.tar.gz | tar -xz
cp -r weekend-desk-main/desk /opt/weekend-desk/
cp weekend-desk-main/requirements.txt /opt/weekend-desk/
cp weekend-desk-main/setup_web.sh /opt/weekend-desk/
rm -rf /opt/weekend-desk/tests && cp -r weekend-desk-main/tests /opt/weekend-desk/
/opt/weekend-desk/venv/bin/pip install -q -r /opt/weekend-desk/requirements.txt
cd /opt/weekend-desk && env -u NTFY_TOPIC -u ANTHROPIC_API_KEY ./venv/bin/python -m pytest -q tests
systemctl restart weekend-desk
bash /opt/weekend-desk/setup_web.sh
(cd /opt/weekend-desk && ./venv/bin/python -m desk backfill > /dev/null 2>&1 || true)
sleep 30
./venv/bin/python -m desk status
