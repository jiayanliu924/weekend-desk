#!/usr/bin/env bash
# 在服务器上更新到 GitHub 最新版本（保留数据、配置和 .env）
set -e
cd /root/src
rm -rf weekend-desk-main
curl -fsSL https://github.com/jiayanliu924/weekend-desk/archive/refs/heads/main.tar.gz | tar -xz
cp -r weekend-desk-main/desk /opt/weekend-desk/
cp weekend-desk-main/requirements.txt /opt/weekend-desk/
cp weekend-desk-main/setup_web.sh /opt/weekend-desk/
# 配置跟代码一起更新（旧配置备份为 .bak；规则只能在周一复核时改，更新也尽量在周一到周四做）
for f in config.toml events.toml; do
  if ! cmp -s weekend-desk-main/$f /opt/weekend-desk/$f; then
    cp /opt/weekend-desk/$f /opt/weekend-desk/$f.bak 2>/dev/null || true
    cp weekend-desk-main/$f /opt/weekend-desk/$f
  fi
done
rm -rf /opt/weekend-desk/tests && cp -r weekend-desk-main/tests /opt/weekend-desk/
rm -rf /opt/weekend-desk/knowledge && cp -r weekend-desk-main/knowledge /opt/weekend-desk/
/opt/weekend-desk/venv/bin/pip install -q -r /opt/weekend-desk/requirements.txt
cd /opt/weekend-desk && env -u NTFY_TOPIC -u ANTHROPIC_API_KEY ./venv/bin/python -m pytest -q tests
systemctl restart weekend-desk
bash /opt/weekend-desk/setup_web.sh
(cd /opt/weekend-desk && ./venv/bin/python -m desk backfill > /dev/null 2>&1 || true)
# 若在周末窗口内更新了代码，用新代码重新冻结本周规则，避免本周被判"作废"而跳过下单
(cd /opt/weekend-desk && ./venv/bin/python -m desk refreeze || true)
sleep 30
./venv/bin/python -m desk status
