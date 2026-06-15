#!/usr/bin/env bash
# DMZ 智能体 · 自检 + 回环自动修复
#
# 反复执行：selfcheck → 若有失败，把失败报告喂给 Claude Code（headless）让它修 →
# 再 selfcheck，直到全绿或达到最大轮数。
#
# 用法：
#   scripts/autofix.sh                 # 最多 5 轮
#   scripts/autofix.sh 8               # 最多 8 轮
#   DRY_RUN=1 scripts/autofix.sh       # 只跑自检、打印将要修的内容，不调用 Claude
#
# 依赖：python、以及 PATH 中的 `claude` CLI（Claude Code）。无 claude 时退化为
# 「只报告、不修复」，退出码反映自检结果，可直接用于 CI。
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2
MAX_ITERS="${1:-5}"

# Pick an interpreter: $PYTHON, else python3, else python.
if [[ -n "${PYTHON:-}" ]]; then PY="$PYTHON"
elif command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else echo "✗ 找不到 python/python3 解释器"; exit 2; fi
SELFCHECK=("$PY" scripts/selfcheck.py)

have_claude() { command -v claude >/dev/null 2>&1; }

run_check() { "${SELFCHECK[@]}"; }              # 人读
run_check_json() { "${SELFCHECK[@]}" --json; }  # 机读

echo "==> 初始自检"
if run_check; then
  echo "✅ 已全绿，无需修复。"
  exit 0
fi

if ! have_claude; then
  echo
  echo "⚠ 未找到 \`claude\` CLI，无法自动修复。以上为失败项，请手动处理后重跑。"
  echo "  （安装 Claude Code 后即可 \`scripts/autofix.sh\` 回环自愈。）"
  exit 1
fi

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo
  echo "==> DRY_RUN：将把以下失败报告交给 Claude 修复（此处不执行）："
  run_check_json
  exit 1
fi

for ((i = 1; i <= MAX_ITERS; i++)); do
  echo
  echo "================ 修复轮次 $i / $MAX_ITERS ================"
  REPORT="$(run_check_json)"

  PROMPT="你是这个仓库（DMZ 智能体，飞书 + hermes-agent）的维护者。\
下面是自动化自检 scripts/selfcheck.py 的 JSON 失败报告。请逐项定位并修复\
**根因**（改源码或测试或文档，不要改 selfcheck 去绕过检查），保持所有现有\
单元测试通过，遵守仓库 CLAUDE.md 的不变量（双层防御、emailAddress 服务端注入、\
记忆层不存敏感字段、max_iterations/timeout 硬上限等）。改完后无需解释，直接修改文件。

自检失败报告：
${REPORT}"

  echo "==> 调用 Claude Code 修复中…"
  # --permission-mode acceptEdits：允许直接改文件；--print：headless 单次执行
  claude --print --permission-mode acceptEdits "$PROMPT" \
    || { echo "⚠ claude 调用失败（轮次 $i）"; }

  echo "==> 复检"
  if run_check; then
    echo
    echo "🎉 第 $i 轮后全绿，修复完成。"
    exit 0
  fi
done

echo
echo "❌ 达到最大轮数 $MAX_ITERS 仍未全绿。请人工介入查看上方失败项。"
exit 1
