#!/usr/bin/env bash
set -e
set -o pipefail
cd /opt/weekend-desk
mkdir -p /opt/weekend-desk/data/reports
LOG=/opt/weekend-desk/data/reports/bear_call_v1.log
echo "开始跑熊市看涨价差 BC1/BC2（开发段回测，不碰钱）" | tee -a "$LOG"
./venv/bin/python -m desk.options_lab --dev knowledge/proposals/bear_call_v1/BC1.json --who opus-bc1 2>&1 | tee -a "$LOG"
./venv/bin/python -m desk.options_lab --dev knowledge/proposals/bear_call_v1/BC2.json --who opus-bc2 2>&1 | tee -a "$LOG"
./venv/bin/python -m desk.options_lab --status 2>&1 | tee -a "$LOG"
