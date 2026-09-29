#!/usr/bin/env bash
# 激活后端 conda 环境（aip）。
#
# 背景：VSCode 远程会话会前置 backend/.venv/bin 到 PATH（空 venv，非后端运行环境），
# 导致 `conda run -n aip python` 解析到 .venv 的空解释器。
# 本脚本清理 .venv 前缀后激活 aip，供开发命令统一使用：
#   source scripts/env_aip.sh && python -u tests/e2e_ask.py
set -u

export PATH=$(printf '%s' "$PATH" | awk -v RS=: -v ORS=: '/\.venv\/bin/ {next} {print}' | sed 's/:$//')
unset VIRTUAL_ENV 2>/dev/null || true
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate aip
