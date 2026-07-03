"""
Определение Skill - единицы функциональности AI-помощника
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

SkillType = Literal["report", "calculator", "rag"]

@dataclass
class SkillDefinition:
    """Спецификация одного навыка, спарсенная из Markdown."""

    skill_id: str                      # ior_period_pao_sberbank_v2
    title: str                         # "ИОР за период по ПАО Сбербанк"
    type: SkillType = "report"
    category: str = "report"

    # File paths
    md_path: Path = None               # path к .md
    notebook_path: Optional[Path] = None  # path к .ipynb (для type=report)

    # Извлечённые из MD блоки
    description: str = ""
    subtitle: str = ""                 # короткое описание (§1)
    triggers: list[str] = field(default_factory=list)      # одна фраза для UI
    anti_triggers: list[str] = field(default_factory=list) # фразы пользователя (§2)
    examples: list[str] = field(default_factory=list)      # анти-триггеры (§3)
    placeholder: str = ""              # примеры запросов
    response_template: str = ""        # пример для welcome-card
    input_schema: dict = field(default_factory=dict)       # шаблон ответа LLM (§11)
    output_schema: dict = field(default_factory=dict)      # JSON Schema (§16)
    full_text: str = ""
                                       # полный текст MD (для контекста)

    def to_dict(self) -> dict:
        return {
            "id": self.skill_id,
            "skill_id": self.skill_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "type": self.type,
            "category": self.category,
            "desc": self.description,
            "description": self.description,
            "triggers": self.triggers,
            "examples": self.examples,
            "placeholder": self.placeholder,
            "has_notebook": self.notebook_path is not None,
        }

    def short_for_router(self) -> str:
        """Краткое описание для подачи в промпт LLM-маршрутизатора."""
        triggers_preview = ", ".join(f'"{t}"' for t in self.triggers[:5])
        return (
            f"- **{self.skill_id}** ({self.type}): {self.title}.\n"
            f"  Описание: {self.description[:200]}\n"
            f"  Триггеры: {triggers_preview}"
        )