"""
toolvalidate – разбор и валидация ОДНОГО действия ReAct-контроллера (§3.1/§3.2).

Заменяет хардкод-костыли планировщика (_autofix_plan_tools / _TOOL_FUZZY_ALIASES /
авто-обёртка): неизвестный тул НЕ маппится молча по curated-словарю, а возвращается
модели как НАБЛЮДЕНИЕ-ошибка со списком доступных действий (тот же канал, что и
EMPTY_RESULT). Это «опора на реальные данные + рассуждение LLM», а не словарь синонимов.

ОФЛАЙН-БЕЗОПАСНЫЙ: только stdlib. valid_tools/arg_schemas передаются снаружи
(в проде – из REGISTRY; в тестах – из фейка), модуль не тянет backend.data.config.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

# Виртуальные действия – не зарегистрированные тулы, обрабатываются циклом напрямую
VIRTUAL_ACTIONS = {"run_query_spec", "run_query", "final", "ask_user"}


@dataclass
class Action:
    thought: str
    action: str
    args: dict = field(default_factory=dict)


def compile_action(raw, valid_tools) -> tuple[Optional[Action], Optional[str]]:
    """(Action, None) либо (None, err-наблюдение). Без молчаливого маппинга.

    Терпим к типичным деформациям LLM: list-обёртка, {"tool":...} вместо
    {"action":...}, размазанные поля без 'args'. Но НЕ угадывает имя тула.
    """
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    if not isinstance(raw, dict):
        return None, "ответ не является JSON-объектом {thought, action, args}."
    action = raw.get("action") or raw.get("tool")
    if not action or not isinstance(action, str):
        return None, "нет поля 'action' (имя действия). Верни {thought, action, args}."
    args = raw.get("args")
    if not isinstance(args, dict):
        # поля размазаны на верхнем уровне – собираем их в args
        args = {k: v for k, v in raw.items()
                if k not in ("action", "tool", "thought", "args")}
    valid = set(valid_tools) | VIRTUAL_ACTIONS
    if action not in valid:
        return None, (f"действия '{action}' не существует. Доступны: "
                      f"{sorted(valid)}. Выбери ОДНО из списка.")
    return Action(thought=str(raw.get("thought", "")), action=action, args=args), None


def validate_tool_call(name: str, args: dict, schema,
                       arg_schemas: Optional[dict] = None) -> list[str]:
    """Список ошибок (пустой = ок). Ошибки -> НАБЛЮДЕНИЕ модели, не краш.

    Проверяет: существование table по Schema; обязательные args по args_schema тула
    (если переданы arg_schemas={name: args_schema}). Колонки в where не валидируем
    жёстко (грауд/тул-гарды это ловят), чтобы не плодить ложных ошибок.
    """
    errs: list[str] = []
    if name in ("final", "ask_user"):
        return errs
    tbl = args.get("table")
    if tbl and tbl not in set(schema.table_names()):
        errs.append(f"таблицы {tbl!r} нет; доступны: {schema.table_names()}")
    if arg_schemas and name in arg_schemas:
        for req in ((arg_schemas[name] or {}).get("required") or []):
            if req not in (args or {}):
                errs.append(f"нет обязательного аргумента {req!r} для действия {name!r}.")
    return errs


def canon_args(args) -> str:
    """Канон args для анти-зацикливания: сорт ключей + числа+float (100 == 100.0).

    Ловит микро-вариации, переставленные ключи, int/float-шум. Хешируемая строка.
    """
    def norm(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            return {k: norm(v[k]) for k in sorted(v, key=str)}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return v
    return json.dumps(norm(args or {}), sort_keys=True, ensure_ascii=False, default=str)


def parse_controller_json(text: str) -> dict:
    """Робастный разбор JSON-ответа: срез ```json-fence``` + первый сбалансированный {...}.

    Возвращает {} если не разобралось (контроллер обработает как invalid).
    """
    if not text:
        return {}
    import re
    s = text.strip()
    m = re.search(r"```(?:json)?\s*\n(.+?)\n```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    i = s.find("{")
    if i == -1:
        return {}
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                s = s[i:j + 1]
                break
    # Убираем комментарии // и /* */
    s_clean = re.sub(r"//.*$", "", s, flags=re.MULTILINE)
    s_clean = re.sub(r"/\*.*?\*/", "", s_clean, flags=re.DOTALL)
    s_clean = s_clean.strip()
    
    try:
        return json.loads(s_clean)
    except Exception:  # noqa: BLE001
        try:
            import ast
            parsed = ast.literal_eval(s_clean)
            if isinstance(parsed, dict):
                return parsed
        except Exception:  # noqa: BLE001
            pass
        return {}