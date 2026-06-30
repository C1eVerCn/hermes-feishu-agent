"""bot.skills — 加载 hermes 风格的 SKILL.md（业务专属操作手册）。

skill 是一段**给 LLM 阅读**的 markdown（含 YAML frontmatter），
告诉 LLM 在某场景下该怎么思考、用什么工具、遵循什么流程。
bot 启动时把 skill 文本作为**对话上下文补充**注入 agent，
让 agent 在多轮对话中按 skill 行事。

跟 system prompt 的区别：
- system prompt 是常驻的（每次对话都带）—— 只放身份 + 工具列表
- skill 是按需的（本次任务相关才带）—— 放操作流程、边界、容错

设计动机（2026-06-30）：
system prompt 已经膨胀到 70+ 行 / ~587 token，每次对话都白白带过去。
把"操作知识"（约车流程、字段枚举、闲聊应对、查不到车）抽到 skill，
system prompt 只剩身份与工具列表 → 省 token + 更聚焦。
"""
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).parent


def list_skills() -> list[str]:
    """列出所有可用 skill 名（目录名）。"""
    if not _SKILLS_DIR.exists():
        return []
    return sorted(
        p.name for p in _SKILLS_DIR.iterdir()
        if p.is_dir() and (p / "SKILL.md").exists()
    )


def load_skill(name: str) -> Optional[str]:
    """加载指定 skill 的完整 markdown 文本（含 YAML frontmatter）。

    没找到 → 返回 None。**不抛异常**（调用方决定怎么降级）。
    """
    path = _SKILLS_DIR / name / "SKILL.md"
    if not path.exists():
        log.warning("skill_not_found name=%s path=%s", name, path)
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("skill_load_failed name=%s err=%s", name, e)
        return None


def load_all_skills() -> dict[str, str]:
    """加载所有 skill，返回 {name: content}。"""
    out = {}
    for name in list_skills():
        content = load_skill(name)
        if content:
            out[name] = content
    return out
