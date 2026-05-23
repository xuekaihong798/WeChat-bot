"""Skill 加载器 — 兼容 Rebyte SKILL.md 格式。

启动时扫描 skills/ 目录，解析所有 SKILL.md 的 YAML frontmatter。
"""

import os
import logging

logger = logging.getLogger(__name__)


def _parse_skill_md(path: str) -> dict | None:
    """Parse a SKILL.md file. Returns {name, description, body, path} or None."""
    try:
        with open(path) as f:
            raw = f.read()
    except Exception:
        logger.warning("无法读取 skill: %s", path)
        return None

    # Parse YAML frontmatter (delimited by ---)
    body = raw
    meta = {}
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            meta_str = parts[1].strip()
            body = parts[2].strip()
            for line in meta_str.split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()

    name = meta.get("name", os.path.basename(os.path.dirname(path)))
    desc = meta.get("description", "")
    if not desc:
        logger.warning("Skill %s 缺少 description，跳过", name)
        return None

    return {
        "name": name,
        "description": desc,
        "body": body,
        "path": path,
        "dir": os.path.dirname(path),
    }


class SkillRegistry:
    """管理和查询已加载的 Skills。"""

    def __init__(self, skills_dir: str = "skills"):
        self.skills: dict[str, dict] = {}
        self._load(skills_dir)

    def _load(self, skills_dir: str):
        if not os.path.isdir(skills_dir):
            logger.info("skills/ 目录不存在，跳过")
            return

        for entry in sorted(os.listdir(skills_dir)):
            skill_dir = os.path.join(skills_dir, entry)
            if not os.path.isdir(skill_dir):
                continue
            md_path = os.path.join(skill_dir, "SKILL.md")
            if not os.path.isfile(md_path):
                continue
            skill = _parse_skill_md(md_path)
            if skill:
                self.skills[skill["name"]] = skill
                logger.info("📋 加载 skill: %s", skill["name"])

    def metadata_text(self) -> str:
        """生成注入 system prompt 的 skill 列表（仅 name + description）。"""
        if not self.skills:
            return ""
        lines = ["\n可用 Skills（根据描述自动匹配）："]
        for name, s in self.skills.items():
            lines.append(f"- {name}: {s['description']}")
        return "\n".join(lines)

    def get(self, name: str) -> dict | None:
        """获取完整 skill 数据。"""
        return self.skills.get(name)

    def list_names(self) -> list[str]:
        return list(self.skills.keys())


# 全局实例（模块级单例）
registry = SkillRegistry()
