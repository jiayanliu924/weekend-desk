#!/usr/bin/env bash
# 在服务器上更新到 GitHub 最新版本（保留数据、配置和 .env）
# 推荐用后台方式跑（断网也不中断，日志在 /root/update.log）：
#   systemd-run --unit=wd-update --collect bash -c 'curl -fsSL https://raw.githubusercontent.com/jiayanliu924/weekend-desk/main/update.sh | bash > /root/update.log 2>&1'
set -e
mkdir -p /root/src && cd /root/src
rm -rf weekend-desk-main
curl -fsSL https://github.com/jiayanliu924/weekend-desk/archive/refs/heads/main.tar.gz | tar -xz
/opt/weekend-desk/venv/bin/pip install -q -r weekend-desk-main/requirements.txt
# 先在新代码里跑测试，没过就整体放弃：旧版本照常运行，不会出现"一半新一半旧"
if ! (cd weekend-desk-main && env -u NTFY_TOPIC -u ANTHROPIC_API_KEY nice -n 10 /opt/weekend-desk/venv/bin/python -m pytest -q -p no:cacheprovider tests); then
  echo "【没更新】测试没过，旧版本继续运行。把这份日志发给 Claude。"
  exit 1
fi
cp weekend-desk-main/update.sh /opt/weekend-desk/update.sh
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
cd /opt/weekend-desk
systemctl restart weekend-desk
bash /opt/weekend-desk/setup_web.sh
(cd /opt/weekend-desk && ./venv/bin/python -m desk backfill > /dev/null 2>&1 || true)
# 若在周末窗口内更新了代码，用新代码重新冻结本周规则，避免本周被判"作废"而跳过下单
(cd /opt/weekend-desk && ./venv/bin/python -m desk refreeze || true)
sleep 30
./venv/bin/python -m desk status
# 回测单独限内存（350M）、低优先级跑，超了只杀它自己，不拖垮网站和下单
(cd /opt/weekend-desk && nohup systemd-run --scope --quiet -p MemoryMax=350M -p MemorySwapMax=0 -p CPUQuota=50% nice -n 19 ./venv/bin/python -m desk.labs_auto >> /opt/weekend-desk/data/labs_auto.log 2>&1 &)
echo "【更新完成】"
