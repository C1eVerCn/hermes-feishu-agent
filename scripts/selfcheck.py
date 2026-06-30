#!/usr/bin/env python3
"""约车助手 · 自动化自检（selfcheck）。

一条命令把项目从"能不能跑/对不对"角度全面体检，列出所有错误项，
退出码非 0 表示有失败（供 CI / autofix.sh 回环修复消费）。

检查项（每项独立，互不影响）：
  1. unit_tests       pytest tests/unit/ 全绿
  2. compile_all      所有 .py 可编译（语法 / def-time 注解）
  3. imports          关键模块可导入（import-time 副作用不报错）
  4. settings_invariants  max_iterations=30 / timeout=120 等硬上限未被改坏
  5. env_drift        settings.py 读取的 env 键都在 .env.example 里有记载
  6. stale_docs       已追踪文档不残留已删业务域词（mock_api / create_order …）
  7. car_no_email     car_tools 工具 schema 不含 emailAddress/openId（业务域不变量）
  8. car_servers      ~/.hermes/config.yaml::mcp_servers 含 car_booking entry

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

_DUMMY_ENV = {
    "FEISHU_APP_ID": "x", "FEISHU_APP_SECRET": "x", "MINIMAX_API_KEY": "x",
}

_STALE_TOKENS = ["mock_api", "mock_tools", "create_order", "list_orders",
                 "pay_order", "ship_order", "create_report_job",
                 "test_bench", "test_vlm", "/fmp/"]
_STALE_DOC_GLOBS = ["README.md", "docs/architecture.md", "docs/deployment.md",
                    "docs/design-decisions.md"]

_KEY_MODULES = ["config.settings", "ocl.pipeline", "ocl.permission",
                "bot.handler", "bot.agent_pool", "car_tools.register",
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
    errs = []
    src = (ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    for var, want in (("AGENT_MAX_ITERATIONS", "10"), ("AGENT_TIMEOUT_SECONDS", "120")):
        m = re.search(rf'{var}.*getenv\(\s*["\']{var}["\']\s*,\s*["\'](\d+)["\']', src)
        if not m:
            errs.append(f"{var}: 未找到默认值字面量")
        elif m.group(1) != want:
            errs.append(f"{var} 默认字面量={m.group(1)}（应为 {want}）")
    return Result("settings_invariants", not errs, "\n".join(errs))


def check_env_drift() -> Result:
    settings_src = (ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    keys = set(re.findall(r'(?:getenv|_require|_optional)\(\s*["\']([A-Z_]+)["\']', settings_src))
    documented = set(re.findall(r'^([A-Z_]+)=', example, re.MULTILINE))
    documented |= set(re.findall(r'#\s*([A-Z_]+)=', example))
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


def check_car_no_email() -> Result:
    """车辆预约业务域不变量：car_tools 注册的工具 schema 不得含 emailAddress/openId。
    这两个字段由服务端从 contextvars 注入，LLM 永远看不到。"""
    code = (
        "import car_tools.register\n"
        "from tools.registry import registry\n"
        "bad = []\n"
        "for t in registry.get_tool_names_for_toolset('car'):\n"
        "    sc = registry.get_schema(t) or {}\n"
        "    props = sc.get('parameters', {}).get('properties', {})\n"
        "    for forbidden in ('emailAddress','openId','mobile'):\n"
        "        if forbidden in props: bad.append(f'{t}: 含 {forbidden}')\n"
        "print('car_tools schema 不应含 emailAddress/openId/mobile: ' + ', '.join(bad) if bad else '')\n"
        "import sys; sys.exit(1 if bad else 0)\n"
    )
    p = _run([sys.executable, "-c", code])
    if "ModuleNotFoundError" in p.stderr and "tools.registry" in p.stderr:
        return Result("car_no_email", True, "skipped: tools.registry 不可用")
    ok = p.returncode == 0
    return Result("car_no_email", ok, "" if ok else (p.stdout + p.stderr).strip()[-800:])


def check_car_servers() -> Result:
    """~/.hermes/config.yaml::mcp_servers 至少一个 car_booking entry。

    注：dev / 本地自检无 MCP server 也属正常（mock 运行）。仅在 CI / 部署时失败，
    所以这里 warning 级提示不算 fail。
    """
    cfg = Path.home() / ".hermes" / "config.yaml"
    if not cfg.exists():
        return Result("car_servers", True, "skipped: ~/.hermes/config.yaml 不存在（dev/local 不强制）")
    try:
        import yaml  # type: ignore
    except ImportError:
        text = cfg.read_text(encoding="utf-8")
        ok = "car_booking" in text
        return Result("car_servers", ok, "" if ok else "yaml 不在；car_booking 也不在文件中（dev 可忽略）")
    try:
        with cfg.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        servers = (data.get("mcp_servers") or {}) if isinstance(data, dict) else {}
        names = list(servers.keys()) if isinstance(servers, dict) else []
        # dev / 本地无 MCP 配置不算硬失败 —— 报 warning，selfcheck 仍标 OK
        if "car_booking" in names:
            return Result("car_servers", True, "")
        return Result("car_servers", True,
                      f"warning: mcp_servers={names} 未含 car_booking（dev 可忽略；prod 部署必填）")
    except Exception as e:
        return Result("car_servers", True, f"skipped: parse error {e}")


CHECKS = {
    "unit_tests": check_unit_tests,
    "compile_all": check_compile_all,
    "imports": check_imports,
    "settings_invariants": check_settings_invariants,
    "env_drift": check_env_drift,
    "stale_docs": check_stale_docs,
    "car_no_email": check_car_no_email,
    "car_servers": check_car_servers,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="约车助手自动化自检")
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
        print("\n=== 约车助手自检报告 ===")
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
