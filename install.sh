#!/usr/bin/env bash
# Weekend Desk 一键安装（Ubuntu 22.04 / 24.04，以 root 运行）
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/weekend-desk

echo "==> 安装系统依赖"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip tzdata >/dev/null

echo "==> 复制程序到 $DEST"
mkdir -p "$DEST"
cp -r "$SRC"/desk "$SRC"/requirements.txt "$DEST"/
if [ -d "$SRC/tests" ]; then cp -r "$SRC/tests" "$DEST"/; fi
if [ -f "$SRC/README.md" ]; then cp "$SRC/README.md" "$DEST"/; fi
[ -f "$DEST/config.toml" ] || cp "$SRC/config.toml" "$DEST/config.toml"
[ -f "$DEST/events.toml" ] || cp "$SRC/events.toml" "$DEST/events.toml"

echo "==> 建立 Python 环境"
python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --upgrade pip
"$DEST/venv/bin/pip" install -q -r "$DEST/requirements.txt"

if [ ! -f "$DEST/.env" ]; then
  if [ -n "${NONINTERACTIVE:-}" ]; then
    KEY="${ANTHROPIC_API_KEY:-}"
  else
    echo
    read -r -p "粘贴你的 Anthropic API Key（用于读新闻抽事件，没有可先回车跳过）： " KEY
  fi
  TOPIC="${NTFY_TOPIC:-weekend-desk-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
  cat > "$DEST/.env" <<EOF
ANTHROPIC_API_KEY=$KEY
NTFY_TOPIC=$TOPIC
EOF
  chmod 600 "$DEST/.env"
fi
TOPIC=$(grep NTFY_TOPIC "$DEST/.env" | cut -d= -f2)

cd "$DEST"
if [ -d tests ]; then echo "==> 自检（模拟一个完整周末）"; env -u NTFY_TOPIC -u ANTHROPIC_API_KEY ./venv/bin/python -m pytest -q tests; fi

echo "==> 设置开机自启的后台服务"
cat > /etc/systemd/system/weekend-desk.service <<EOF
[Unit]
Description=Weekend Desk (XYZ100 weekend price discovery research)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DEST
ExecStart=$DEST/venv/bin/python -m desk run
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable weekend-desk
systemctl restart weekend-desk

cp "$SRC/setup_web.sh" "$DEST/" 2>/dev/null && bash "$DEST/setup_web.sh" || true

echo "==> 回补历史，生成基线报告和期权历史粗看"
./venv/bin/python -m desk backfill > /dev/null || true
head -14 data/reports/baseline_history.md 2>/dev/null || true
head -9 data/reports/options_history.md 2>/dev/null || true

sleep 20
./venv/bin/python -m desk status || true

cat <<EOF

========================================================
 安装完成，程序已在后台常驻运行。

 手机推送：装 ntfy App（iOS / Android），订阅这个频道：
     $TOPIC
 （频道名就是密码，别外传）

 常用命令（先 cd $DEST）：
   ./venv/bin/python -m desk status      看运行状态
   ./venv/bin/python -m desk report      生成最近一周周报
   journalctl -u weekend-desk -f         看实时日志
========================================================
EOF
