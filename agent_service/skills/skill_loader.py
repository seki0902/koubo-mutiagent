"""Load the writing Skill for the Script Agent.

The Skill is a set of human-edited markdown files: a main ``SKILL.md``, four
protocol documents, and a per-category library.  It is not something the model
is expected to browse; the loader selects the material and the Script Agent
either receives it in its prompt or pulls it through the ``load_script_skill``
tool.

Selection is deterministic and mirrors the repository's rule: score each
library file by keyword hits against the topic title, weigh an explicit
filename mention double, and fall back to ``实操攻略.md`` when nothing matches.
Keeping the rule here -- rather than asking the model which category a topic is
-- is what makes the same topic always load the same category.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_LIBRARY_FILE",
    "LIBRARY_RULES",
    "ScriptSkill",
    "SkillUnavailable",
    "build_script_skill",
    "load_script_skill",
    "normalize_text",
    "select_library_file",
    "skill_root",
]

#: The fallback category when no keyword matches: the most general playbook.
DEFAULT_LIBRARY_FILE = "实操攻略.md"

#: Which library file a topic belongs to.  Order matters: the first rule with
#: the highest score wins, so this list is also the tie-break order.
LIBRARY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "招聘会活动.md",
        (
            "招聘会", "双选会", "人才对接会", "引才", "专场", "海归专场",
            "空中双选会", "线上双选会", "报名", "活动", "宣讲会",
        ),
    ),
    (
        "单企业直招.md",
        (
            "直招", "单企业", "单组织", "招聘公告", "校招", "社招",
            "单独招聘", "福耀", "字节", "腾讯", "阿里", "华为", "联合国",
        ),
    ),
    (
        "平台工具.md",
        (
            "平台", "网站", "网址", "入口", "渠道", "工具", "官网", "系统",
            "小程序", "国际组织",
        ),
    ),
    (
        "认知打破.md",
        (
            "误区", "真相", "认知", "打破", "醒醒", "别再", "其实不是",
            "不是", "为什么", "颠覆",
        ),
    ),
    (
        "赛道红利.md",
        (
            "红利", "赛道", "机会", "通道", "政策", "补贴", "安家费",
            "住房补贴", "专项", "绿色通道", "人才引进",
        ),
    ),
    (
        "实操攻略.md",
        (
            "怎么", "步骤", "教程", "方法", "timeline", "时间线", "秋招",
            "春招", "简历", "投递", "建档", "认证", "档案", "应届", "落户",
            "选调", "国考", "省考",
        ),
    ),
)

#: Documents every script gets, in this order, before the category library.
REQUIRED_DOCUMENTS: tuple[str, ...] = (
    "SKILL.md",
    "docs/structure-templates.md",
    "docs/jargon-lexicon.md",
    "docs/emotional-protocol.md",
    "docs/domain-knowledge.md",
)

#: Optional material, added only when it has content.
OPTIONAL_DOCUMENTS: tuple[str, ...] = (
    "examples/input-sample.md",
    "examples/output-sample.md",
)

#: Guards carried over from the repository rule book, stated for this Agent.
#:
#: The legacy pipeline asked for two or three plain-text versions in one
#: answer.  This Agent returns one structured draft instead, so the version
#: instruction is deliberately absent; everything else is unchanged, because
#: it is about not emitting code, commands, or invented facts.
RUNTIME_GUARDRAILS = """\
# 本工具运行约束

你正在口播视频工具里生成文案，不是在 Codex、Claude、Cursor 或任何 Agent 运行环境里。

必须遵守：
- 只输出中文口播稿正文。
- 不要输出 Markdown 代码块。
- 不要输出 XML、DSML、JSON、YAML 或工具调用格式。
- 不要输出 <｜｜DSML｜｜tool_calls>、Bash、curl、pwd、ls、find、Invoke-WebRequest 等命令内容。
- 不要声称正在读取文件、搜索网页、调用工具、写入飞书或执行脚本。
- 遇到 skill 中要求实时搜索、写 Bash、写飞书、读写本地文件的步骤时，只把它理解为事实谨慎原则，不要执行，也不要把步骤写出来。
- 如果事实不确定，用概括表述替代，不要编造具体企业、金额、时间、地点。
- 本次只产出一版稿件，按结构化 schema 返回分节内容；不要在一个字段里塞多版。"""

_NORMALIZE_RE = re.compile(r"[\s，。、！？：；（）【】“”'\"·—\-]+")


class SkillUnavailable(RuntimeError):
    """The Skill directory could not be read."""


@dataclass(frozen=True)
class ScriptSkill:
    """The Skill material selected for one topic."""

    topic_title: str
    library_file: str
    files: tuple[str, ...]
    prompt: str


def skill_root() -> Path:
    """Return the Skill directory shipped with the Agent service."""

    root = Path(__file__).resolve().parents[1] / "skill"
    if not (root / "SKILL.md").is_file():
        raise SkillUnavailable(f"Skill 目录缺少 SKILL.md: {root}")
    return root


def normalize_text(value: str) -> str:
    """Fold a topic or keyword to the form the scoring rule compares."""

    return _NORMALIZE_RE.sub("", value.strip().lower())


def _library_files(root: Path) -> list[str]:
    library_dir = root / "library"
    if not library_dir.is_dir():
        raise SkillUnavailable(f"Skill 目录缺少 library: {library_dir}")
    files = sorted(
        entry.name
        for entry in library_dir.iterdir()
        if entry.is_file() and entry.suffix.lower() == ".md"
    )
    if not files:
        raise SkillUnavailable(f"library 中没有 .md 文件: {library_dir}")
    return files


def select_library_file(root: Path, topic_title: str) -> str:
    """Return the library file one topic belongs to.

    Args:
        root: the resolved Skill directory.
        topic_title: usually the confirmed candidate's title; the user's wording
            for a topic works too, since the rule is keyword containment.
    """

    files = _library_files(root)
    known = set(files)
    normalized = normalize_text(topic_title)

    scores: dict[str, int] = {name: 0 for name in files}
    for file_name, keywords in LIBRARY_RULES:
        if file_name not in known:
            continue
        score = sum(
            1
            for keyword in keywords
            if normalize_text(keyword) and normalize_text(keyword) in normalized
        )
        if normalize_text(file_name[: -len(".md")]) in normalized:
            score += 2
        scores[file_name] += score

    best = max(scores.values())
    if best == 0:
        return DEFAULT_LIBRARY_FILE if DEFAULT_LIBRARY_FILE in known else files[0]

    for file_name, _ in LIBRARY_RULES:
        if scores.get(file_name) == best:
            return file_name
    return DEFAULT_LIBRARY_FILE if DEFAULT_LIBRARY_FILE in known else files[0]


def _read(root: Path, relative: str) -> str:
    path = root / relative
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_script_skill(topic_title: str) -> ScriptSkill:
    """Load the Skill material for one topic.

    Raises:
        SkillUnavailable: the Skill directory is missing or unreadable.  Callers
            that must not fail a turn catch this and continue without it.
    """

    root = skill_root()
    library_file = select_library_file(root, topic_title)

    sections: list[str] = []
    files: list[str] = []
    for relative in (*REQUIRED_DOCUMENTS, f"library/{library_file}", *OPTIONAL_DOCUMENTS):
        content = _read(root, relative)
        if not content:
            continue
        sections.append(content)
        files.append(relative)

    if not sections:
        raise SkillUnavailable(f"Skill 目录中没有可用内容: {root}")

    sections.append(RUNTIME_GUARDRAILS)
    return ScriptSkill(
        topic_title=topic_title,
        library_file=library_file,
        files=tuple(files),
        prompt="\n\n---\n\n".join(sections),
    )


def build_script_skill(topic_title: str) -> ScriptSkill | None:
    """Load the Skill for a topic, or report that none is available.

    A turn must not fail because the skill assets are missing: writing without
    the playbook is worse, but writing nothing is worse still.
    """

    try:
        return load_script_skill(topic_title)
    except SkillUnavailable:
        return None
