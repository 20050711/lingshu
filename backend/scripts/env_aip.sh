#!/usr/bin/env bash
# 激活后端 conda 环境（aip）。
#
# 为什么不写死路径：仓库要能公开分发，路径里不该带开发机的用户名。
# 按以下顺序找，找到第一个能用即停；也可用 AIP_CONDA_ENV 显式指定。
AIP_CONDA_ENV="${AIP_CONDA_ENV:-}"
for c in "$AIP_CONDA_ENV" "$HOME/miniconda3/envs/aip" "$HOME/anaconda3/envs/aip" \
         "/opt/conda/envs/aip" "/usr/local/envs/aip"; do
  [ -n "$c" ] && [ -x "$c/bin/python" ] && { export PATH="$c/bin:$PATH"; \
    echo "aip env: $c/bin/python ($("$c/bin/python" -V 2>&1))"; return 0 2>/dev/null || exit 0; }
done
echo "未找到 aip conda 环境：请设置 AIP_CONDA_ENV=/path/to/envs/aip" >&2
exit 1
