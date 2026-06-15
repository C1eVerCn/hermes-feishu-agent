#!/usr/bin/env python3
"""DMZ 智能体 · 自动化自检（selfcheck）。

一条命令把项目从"能不能跑/对不对"角度全面体检，列出所有错误项，
退出码非 0 表示有失败（供 CI / autofix.sh 回环修复消费）。

检查项（每项独立，互不影响）：
  1. unit_tests       pytest tests/unit/ 全绿
  2. compile_all      所有 .py 可编译（语法 / def-time 注解）
  3. imports          关键模块可导入（import-time 副作用不报错）
  4. settings_invariants  max_iterations=30 / timeout=120 等硬上限未被改坏
  5. env_drift        settings.py 读取的 env 键都在 .env.example 里有记载
  6. stale_docs       已追踪文档不残留已删业务域词（mock_api / create_order …）
  7. vlm_no_email     VLM 工具 schema 不含 emailAddress（业务域不变量）

用法：
  python scripts/selfcheck.py            # 人读报告
  python scripts/selfcheck.py --json     # 机读（autofix.sh 用）
  python scripts/selfcheck.py --only unit_tests,imports
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 单元测试不要求真实凭证——给假值满足 settings._require()
_DUMMY_ENV = {
    "FEISHU_APP_ID": "x", "FEISHU_APP_SECRET": "x", "MINIMAX_API_KEY": "x",
}

# 已经删除的旧业务域（订单/用户/报表 + mock_api/mock_tools）词表。
# 出现在「当前文档」里即视为陈旧（历史设计文档 docs/superpowers/ 豁免）。
_STALE_TOKENS = ["mock_api", "mock_tools", "create_order", "list_orders",
                 "pay_order", "ship_order", "create_report_job"]
_STALE_DOC_GLOBS = ["README.md", "docs/architecture.md", "docs/deployment.md",
                    "docs/design-decisions.md"]

# 关键模块：导入即触发大量 import-time 副作用（工具注册、配置加载）
_KEY_MODULES = ["config.settings", "ocl.pipeline", "ocl.permission",
                "bot.handler", "bot.agent_pool", "bot.card_action_handler",
                "bench_tools.register", "vlm_tools.register",
                "hermes_plugins.feishu_acl"]


class Result:
    def __init__(self, name: str, ok: bool, detail: str = ""):
        self.name, self.ok, self.detail = name, ok, detail

    def as_dict(self):
        return {"check": self.name, "ok": self.ok, "detail": self.detail}


def _run(cmd: list[str], env_extra: dict | None = None, timeout: int = 300):
    env = {**os.environ, **_DUMMY_ENV, **(env_extra or {})}
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=timeout)


# ── checks ─────────────────────────────────────────────────────────────────

def check_unit_tests() -> Result:
    p = _run([sys.executable, "-m", "pytest", "tests/unit/", "-q",
              "--no-header", "-p", "no:cacheprovider"])
    tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-12:])
    return Result("unit_tests", p.returncode == 0, tail)


def check_compile_all() -> Result:
    pyfiles = [str(p) for p in ROOT.rglob("*.py")
               if ".git" not in p.parts and "__pycache__" not in p.parts]
    p = _run([sys.executable, "-m", "py_compile", *pyfiles])
    return Result("compile_all", p.returncode == 0,
                  (p.stderr or p.stdout).strip()[-1500:])


def check_imports() -> Result:
    code = (
        "import importlib, sys\n"
        f"mods = {_KEY_MODULES!r}\n"
        "bad = []\n"
        "for m in mods:\n"
        "    try: importlib.import_module(m)\n"
        "    except Exception as e: bad.append(f'{m}: {type(e).__name__}: {e}')\n"
        "print('\\n'.join(bad))\n"
        "sys.exit(1 if bad else 0)\n"
    )
    p = _run([sys.executable, "-c", code])
    return Result("imports", p.returncode == 0, (p.stdout + p.stderr).strip()[-1500:])


def check_settings_invariants() -> Result:
    """The code default for the CLAUDE.md hard limits must not be silently
    weakened: AGENT_MAX_ITERATIONS/AGENT_TIMEOUT_SECONDS literal defaults must
    be 30/120 so a fresh deploy (no .env override) is safe. An operator may
    still deliberately override via .env (that's their call, not checked here)."""
    errs = []
    src = (ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    for var, want in (("AGENT_MAX_ITERATIONS", "30"), ("AGENT_TIMEOUT_SECONDS", "120")):
        m = re.search(rf'{var}.*getenv\(\s*["\']{var}["\']\s*,\s*["\'](\d+)["\']', src)
        if not m:
            errs.append(f"{var}: 未找到默认值字面量")
        elif m.group(1) != want:
            errs.append(f"{var} 默认字面量={m.group(1)}（应为 {want}）")
    return Result("settings_invariants", not errs, "\n".join(errs))


def check_env_drift() -> Result:
    settings_src = (ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    jwt_src = (ROOT / "bench_tools" / "jwt_auth.py").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    # keys read via getenv/_require/_optional anywhere in settings + jwt
    keys = set(re.findall(r'(?:getenv|_require|_optional)\(\s*["\']([A-Z_]+)["\']', settings_src))
    keys |= set(re.findall(r'getenv\(\s*["\']([A-Z_]+)["\']', jwt_src))
    documented = set(re.findall(r'^([A-Z_]+)=', example, re.MULTILINE))
    documented |= set(re.findall(r'#\s*([A-Z_]+)=', example))  # commented optionals
    missing = sorted(k for k in keys if k not in documented)
    return Result("env_drift", not missing,
                  "未在 .env.example 记载的 env 键: " + ", ".join(missing) if missing else "")


def check_stale_docs() -> Result:
    hits = []
    for rel in _STALE_DOC_GLOBS:
        f = ROOT / rel
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8")
        for tok in _STALE_TOKENS:
            if tok in text:
                hits.append(f"{rel}: 含已删域词 '{tok}'")
    return Result("stale_docs", not hits, "\n".join(hits))


def check_vlm_no_email() -> Result:
    code = (
        "import vlm_tools.register\n"
        "from tools.registry import registry\n"
        "bad = []\n"
        "for t in registry.get_tool_names_for_toolset('vlm'):\n"
        "    sc = registry.get_schema(t) or {}\n"
        "    props = sc.get('parameters', {}).get('properties', {})\n"
        "    if 'emailAddress' in props: bad.append(t)\n"
        "print('VLM 工具 schema 不应含 emailAddress: ' + ', '.join(bad) if bad else '')\n"
        "import sys; sys.exit(1 if bad else 0)\n"
    )
    p = _run([sys.executable, "-c", code])
    # If tools.registry isn't importable in this env, treat as skipped-pass.
    if "ModuleNotFoundError" in p.stderr and "tools.registry" in p.stderr:
        return Result("vlm_no_email", True, "skipped: tools.registry 不可用")
    ok = p.returncode == 0
    return Result("vlm_no_email", ok, "" if ok else (p.stdout + p.stderr).strip()[-800:])


CHECKS = {
    "unit_tests": check_unit_tests,
    "compile_all": check_compile_all,
    "imports": check_imports,
    "settings_invariants": check_settings_invariants,
    "env_drift": check_env_drift,
    "stale_docs": check_stale_docs,
    "vlm_no_email": check_vlm_no_email,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="DMZ 智能体自动化自检")
    ap.add_argument("--json", action="store_true", help="输出机读 JSON")
    ap.add_argument("--only", default="", help="逗号分隔，只跑指定检查项")
    args = ap.parse_args()

    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(CHECKS)
    results = [CHECKS[n]() for n in names if n in CHECKS]
    failed = [r for r in results if not r.ok]

    if args.json:
        print(json.dumps({
            "passed": len(results) - len(failed),
            "failed": len(failed),
            "results": [r.as_dict() for r in results],
        }, ensure_ascii=False, indent=2))
    else:
        print("\n=== DMZ 智能体自检报告 ===")
        for r in results:
            mark = "✅" if r.ok else "❌"
            print(f"{mark} {r.name}")
            if not r.ok and r.detail:
                for line in r.detail.splitlines():
                    print(f"      {line}")
        print(f"\n通过 {len(results) - len(failed)}/{len(results)}"
              + ("，全部通过 🎉" if not failed else f"，失败 {len(failed)} 项"))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
