"""
Парсер Markdown-спецификаций навыков.

Структура MD (фиксированная, см. knowledge_base/scripts/*.md):
  # Скрипт «<название>»
  * **Notebook:** <path>
  * **Skill ID:** <id>
  * **Категория:** <report|calculator|rag>
  ...
  ## 1. Краткое описание для LLM-маршрутизатора
  ## 2. Триггеры - когда применять
  ## 3. Анти-триггеры
  ...
  ## 11. Шаблон ответа LLM пользователю
  ## 16. Контракт ввода-вывода
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from backend.config import get_settings
from backend.skills.definition import SkillDefinition, SkillType

_RE_TITLE = re.compile(r"^#\s*(?:\*\*)*\s*Скрипт\s+[\"«']([^\"»'\*]+)[»\"']", re.MULTILINE)
_RE_NOTEBOOK = re.compile(r"\*\*Notebook:\*\*\s*.*?([\w\.\-/_А-Яа-яёЁ]+?\.ipynb)", re.IGNORECASE)
_RE_SKILL_ID = re.compile(r"\*\*Skill ID:\*\*\s*.*?([\w\.\-_]+)", re.IGNORECASE)
_RE_TYPE = re.compile(r"\*\*Категория:\*\*\s*.*?([a-zA-Z\-_а-яА-ЯёЁ/\s]+)", re.IGNORECASE)


def _split_sections(md: str) -> dict[str, str]:
    """Разбивает MD на словарь {номер_раздела: текст_раздела}.

    Раздел = всё между '## N. Название' до следующего '## M. ...'.
    """
    sections: dict[str, str] = {}
    pattern = re.compile(r"^##\s+(\d+(?:\.\d+)?)\.\s+(.+)$", re.MULTILINE)
    matches = list(pattern.finditer(md))
    for i, m in enumerate(matches):
        sec_no = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        sections[sec_no] = md[start:end].strip()
    return sections


def _extract_list_items(text: str, max_items: int = 50) -> list[str]:
    """Достаёт строки вида 'фраза', 'фраза / другая', либо table-rows.

    Эвристики:
    - Из таблицы вытащить контент первого столбца после '|' (это «Триггер»).
    - Из списка `- "фраза"` - взять текст в кавычках.
    """
    items: list[str] = []

    # Из markdown-таблиц: строки начинающиеся с | (но не заголовок и не сепаратор)
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if set(line.replace("|", "").replace("-", "").replace(":", "").strip()) == set():
            continue  # сепаратор |---|---|
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        first = cells[0]
        # пропускаем заголовок
        if first.lower() in (
            "триггер пользователя", "триггер", "если запрос про",
            "если запрос содержит", "если запрос",
            "если запрос требует", "формулировка пользователя",
            "запрос пользователя", "текст пользователя",
            "если пользователь спрашивает", "текст", "формулировка"
        ):
            continue
        # достаём из ячейки фразы в "кавычках"
        quoted = re.findall(r"«([^»]+)»", first)
        if quoted:
            items.extend(quoted)
        elif first and len(first) < 200 and not first.startswith("-"):
            items.append(first)

    # Также - bullet-list: - "фраза", или - **фраза**
    for line in text.splitlines():
        m = re.match(r"^\s*[-*]\s+(?:\*+)?[\"«']([^\"»']+)[»\"']", line)
        if m:
            items.append(m.group(1).strip())

    # уникализируем, сохраняя порядок
    seen = set()
    out = []
    for it in items:
        it = it.strip().rstrip(".")
        if it and it not in seen:
            seen.add(it)
            out.append(it)
        if len(out) >= max_items:
            break
    return out


def _extract_json_schema(text: str) -> dict:
    """Достаёт JSON Schema из раздела (первый ```json блок)."""
    m = re.search(r"```json\s*\n(.+?)\n```", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def _normalize_type(raw: str) -> SkillType:
    raw = (raw or "").lower().strip()
    if "calc" in raw or "калькул" in raw:
        return "calculator"
    if "rag" in raw or "vnd" in raw or "внд" in raw:
        return "rag"
    return "report"


def parse_skill_md(md_path: Path) -> Optional[SkillDefinition]:
    """Парсит .md и возвращает SkillDefinition (или None если структура неверна)."""
    if not md_path.exists():
        return None

    text = md_path.read_text(encoding="utf-8")

    title_m = _RE_TITLE.search(text)
    if not title_m:
        return None
    title = title_m.group(1).strip()

    skill_id_m = _RE_SKILL_ID.search(text)
    skill_id = skill_id_m.group(1) if skill_id_m else md_path.stem

    notebook_m = _RE_NOTEBOOK.search(text)
    notebook_path: Optional[Path] = None
    if notebook_m:
        nb_raw = notebook_m.group(1)
        # путь может быть относительный - пробуем найти
        cfg = get_settings()
        candidate = Path(nb_raw)
        if not candidate.is_absolute():
            # 1) относительно kb_notebooks
            p1 = cfg.kb_notebooks_path / Path(nb_raw).name
            if p1.exists():
                notebook_path = p1
            else:
                # 2) относительно md
                p2 = (md_path.parent / nb_raw).resolve()
                if p2.exists():
                    notebook_path = p2
        else:
            if candidate.exists():
                notebook_path = candidate

    type_m = _RE_TYPE.search(text)
    skill_type = _normalize_type(type_m.group(1) if type_m else "report")

    sections = _split_sections(text)

    # §1 - описание
    description = sections.get("1", "").split("\n\n")[0].strip() if "1" in sections else ""
    # subtitle - первая короткая фраза без markdown
    subtitle = ""
    if description:
        import re as _re
        clean = _re.sub(r"\*\*([^*]+)\*\*", r"\1", description)
        clean = _re.sub(r"\*([^*]+)\*", r"\1", clean)
        clean = clean.replace("\n", " ").strip()
        # Берём до первой точки / переноса
        m = _re.match(r"^([^.!?\n]+[.!?]?)", clean)
        subtitle = m.group(1).strip() if m else clean[:120]

    # §2 - триггеры
    triggers = _extract_list_items(sections.get("2", "")) if "2" in sections else []

    # §3 - анти-триггеры
    anti_triggers = _extract_list_items(sections.get("3", "")) if "3" in sections else []

    # Примеры использования из §2 / §15 / §1
    examples: list[str] = []
    for sec_no in ("15", "2"):
        items = _extract_list_items(sections.get(sec_no, ""))
        for it in items:
            if it and it not in examples:
                examples.append(it)
        if len(examples) >= 5:
            break
    examples = examples[:5]

    # Placeholder - самый осмысленный пример: содержит конкретику (год / SID / период)
    placeholder = ""
    # Приоритет 1: конкретный SID 'EVE-1234567'
    for cand in triggers + examples:
        if re.search(r"\bEVE-\d{5,}\b", cand, re.IGNORECASE):
            placeholder = cand
            break
    # Приоритет 2: год / квартал / месяц + без многоточия / {...}
    if not placeholder:
        for cand in triggers + examples:
            low = cand.lower()
            if "{" in cand or "..." in cand or ".." in cand:
                continue
            if re.search(r"\b20\d{2}\b", low) or re.search(r"\bq[1-4]\b", low):
                placeholder = cand
                break
    # Приоритет 3: первый чистый триггер (без шаблонных скобок)
    if not placeholder:
        for cand in triggers:
            if "{" not in cand and "..." not in cand and ".." not in cand:
                placeholder = cand
                break
    if not placeholder and triggers:
        placeholder = triggers[0]

    # §11 - шаблон ответа
    response_template = sections.get("11", "").strip()

    # §16 - контракт ввода/вывода
    schema_text = sections.get("16", "")
    input_schema = _extract_json_schema(schema_text)
    # output - второй ```json блок
    json_blocks = re.findall(r"```json\s*\n(.+?)\n```", schema_text, re.DOTALL)
    output_schema = {}
    if len(json_blocks) >= 2:
        try:
            output_schema = json.loads(json_blocks[1])
        except json.JSONDecodeError:
            pass

    return SkillDefinition(
        skill_id=skill_id,
        title=title,
        type=skill_type,
        category=skill_type,
        md_path=md_path,
        notebook_path=notebook_path,
        description=description,
        subtitle=subtitle,
        triggers=triggers,
        anti_triggers=anti_triggers,
        examples=examples,
        placeholder=placeholder,
        response_template=response_template,
        input_schema=input_schema,
        output_schema=output_schema,
        full_text=text,
    )