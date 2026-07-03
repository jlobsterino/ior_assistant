"""
Skill Registry - автоматическое обнаружение и загрузка навыков из MD.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from backend.config import get_settings
from backend.skills.definition import SkillDefinition
from backend.skills.md_parser import parse_skill_md

logger = logging.getLogger(__name__)


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, SkillDefinition] = {}
        self._lock = threading.RLock()
        self._loaded = False

    def load_all(self) -> int:
        """Сканирует папку scripts/*.md и регистрирует все навыки."""
        cfg = get_settings()
        scripts_dir = cfg.kb_scripts_path

        if not scripts_dir.exists():
            logger.warning("[Registry] Папка %s не существует", scripts_dir)
            return 0

        with self._lock:
            self._skills.clear()
            count = 0
            for md_path in sorted(scripts_dir.glob("*.md")):
                try:
                    skill = parse_skill_md(md_path)
                    if skill is None:
                        logger.warning("[Registry] Не удалось распарсить %s", md_path.name)
                        continue
                    self._skills[skill.skill_id] = skill
                    count += 1
                    logger.info(
                        "[Registry] Загружен skill '%s' (%s)",
                        skill.skill_id,
                        skill.type,
                    )
                except Exception as e:
                    logger.exception("[Registry] Ошибка парсинга %s: %s", md_path, e)
            self._loaded = True
            return count

    def list_all(self) -> list[SkillDefinition]:
        with self._lock:
            return list(self._skills.values())

    def get(self, skill_id: str) -> Optional[SkillDefinition]:
        with self._lock:
            return self._skills.get(skill_id)

    def reload(self) -> int:
        return self.load_all()

    def for_router_prompt(self) -> str:
        """Готовый блок текста для системного промпта LLM."""
        with self._lock:
            parts = [s.short_for_router() for s in self._skills.values()]
        return "\n\n".join(parts)


# --- Singleton -------------------------------------------------------------

_registry: Optional[SkillRegistry] = None


def get_registry() -> SkillRegistry:
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
        _registry.load_all()
    return _registry