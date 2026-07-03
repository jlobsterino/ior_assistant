"""
grounding – диагностика «почему запрос вернул 0 строк», по РЕАЛЬНЫМ данным.

Корневой баг тестировщиков: модель ставит фильтр на неверную колонку
(ТБ в org_struct_lvl_2_name, хотя они в lvl_3) -> 0 строк -> молчаливый дамп.

diagnose_empty проверяет КАЖДОЕ текстовое условие фильтра по реальному
каталогу витрины:
   • значение реально есть в этой колонке? -> фильтр валиден (0 - реально нет записей)
   • значения тут нет, но оно лежит в ДРУГОЙ колонке? -> НЕВЕРНАЯ колонка, есть исправление

Результат отдаётся рефлектору как actionable-ошибка: какую колонку поставить и
какое реальное значение взять. Это «LLM решает по реальным данным», а не хардкод.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.agent.resolve.value_search import search_values

_STRONG = 0.9      # порог «значение точно есть в этой колонке»
_ELSEWHERE = 0.8   # порог «значение нашлось в другой колонке»


def _flatten_where(where: dict) -> list[tuple]:
    """(col, op, value) из where. Лёгкая копия normalize_where – чтобы grounding
    не тянул весь backend.data (и его pydantic-зависимости) ради разбора dict'а.
    Формы: {col: val} | {col: [list]} | {col: {op: v}} | {col__op: v}."""
    out: list[tuple] = []
    for key, val in (where or {}).items():
        if key == "_or":
            continue
        if "__" in key:
            col, alias = key.rsplit("__", 1)
            op = {"like": "like", "gt": ">", "gte": ">=", "lt": "<",
                  "lte": "<=", "ne": "!=", "eq": "="}.get(alias, "=")
            out.append((col, op, val))
        elif isinstance(val, dict):
            for op, v in val.items():
                out.append((key, str(op).lower(), v))
        elif isinstance(val, list):
            out.append((key, "in", val))
        elif val is None:
            out.append((key, "is", None))
        else:
            out.append((key, "=", val))
    return out


@dataclass
class EmptyDiagnosis:
    likely_wrong_filter: bool      # True -> колонку фильтра почти точно надо менять
    message: str                  # человеко/LLM-читаемая диагностика для рефлектора
    corrections: list = field(default_factory=list)  # [{'filter_column', found_in_column, ...}]


def _inner(val: str) -> str:
    """Снять %like%-обёртку и пробелы."""
    return str(val).strip().strip("%").strip()


def diagnose_empty(table: str, where: dict) -> EmptyDiagnosis:
    """Почему query(table, where) дал 0 строк. Проверка по реальному каталогу."""
    flat = _flatten_where(where)
    # проверяем только текстовые equality / like (даты/числа/диапазоны/IN – пропуск)
    checkable = [(c, op, v) for (c, op, v) in flat
                 if op in ("=", "like") and isinstance(v, str) and v.strip()]

    if not checkable:
        return EmptyDiagnosis(
            False,
            "EMPTY_RESULT: 0 строк. Текстовых фильтров нет (даты/числа) – "
            "вероятно, по заданному периоду/условиям записей действительно нет. "
            "Верни action=ask_user с честным «по этим условиям ничего не найдено»."
        )

    corrections: list[dict] = []
    valid: list[tuple] = []
    for col, _op, v in checkable:
        inner = _inner(v)
        if search_values(inner, columns=[col], min_score=_STRONG, top_k=1):
            valid.append((col, inner))
            continue

        elsewhere = [c for c in search_values(inner, min_score=_ELSEWHERE, top_k=3)
                     if c.column != col]
        if elsewhere:
            best = elsewhere[0]
            corrections.append({
                "filter_column": col, "filter_value": inner,
                "found_in_column": best.column, "found_value": best.value,
                "count": best.count,
                "alternatives": [{"column": c.column, "value": c.value, "count": c.count}
                                 for c in elsewhere]
            })

    if corrections:
        lines = ["EMPTY_RESULT: 0 строк – похоже на НЕВЕРНУЮ колонку фильтра. "
                 "Проверка по реальным данным витрины:"]
        for c in corrections:
            lines.append(
                f"  • значение '{c['filter_value']}' НЕ найдено в колонке "
                f"'{c['filter_column']}'; реально оно лежит в "
                f"'{c['found_in_column']}' (например '{c['found_value']}', "
                f"{c['count']} строк)."
            )
        lines.append("-> Сделай action=retry того же query, заменив колонку фильтра ")
        lines.append("   на 'found_in_column' (значение бери из 'found_value').")
        lines.append("Если уверен, что фильтр верный и 0 – реальный ответ, ")
        lines.append("   верни action=ask_user с честным «не нашёл по условиям».")
        return EmptyDiagnosis(True, "\n".join(lines), corrections)

    if valid:
        vc = ", ".join(f"'{v}' в {col}" for col, v in valid)
        return EmptyDiagnosis(
            False,
            f"EMPTY_RESULT: 0 строк, но фильтры валидны ({vc}). Вероятно, пустое "
            f"пересечение условий (период/комбинация фильтров). Верни action=ask_user "
            f"с честным «по этим условиям записей не найдено, уточнить период/фильтры».",
            []
        )

    # значения не нашлись ни в своей, ни в других колонках (дата/число/свободный текст)
    return EmptyDiagnosis(
        False,
        "EMPTY_RESULT: 0 строк. Значения фильтра не из категориальных справочников "
        "(дата/число/свободный текст). Вероятно, по условиям записей нет – "
        "верни action=ask_user с честным «не нашёл»."
    )


# --- Заземление запроса (А.4) -------------------------------------------
#
# ground_query – извлекает из текста запроса фразы-кандидаты фильтров и
# заземляет их по РЕАЛЬНОМУ каталогу витрины (search_values). Цель – отдать
# планировщику готовое соответствие «фраза -> колонка/значение», чтобы он
# ставил фильтр на ВЕРНУЮ колонку СРАЗУ (а не доводил до самоисправления
# в гардом А.3). Это грауд по данным, а не хардкод: какие колонки/значения
# существуют – решает каталог, разворачивание аббревиатур – LLM.

_GROUND_STRONG = 0.85   # порог уверенного хита для обычной фразы
# Публичный алиас – ЕДИНАЯ константа порога заземления (см. §2.5 «ЕДИНЫЙ ПОРОГ»).
# validate_spec/QuerySpec обязаны брать ИМЕННО ЭТОТ порог, а не заводить свой
# (>=0.9) – иначе ложный blocker на значении, заземлённом пре-процессингом на 0.85-0.9.
GROUND_STRONG = _GROUND_STRONG
_CODE_MIN = 0.6         # для кодов (префикс длинного значения) скор ниже – ок,
                        # но дополнительно требуем префиксное совпадение значения

# коды процессов/событий: П1227, Р12, EVE-5092355
_CODE_RE = re.compile(r"\b(?:[Пп]\d{2,}|EVE-\d+)\b")
# фразы в кавычках (« », " ")
_QUOTED_RE = re.compile(r"['\"«“]([^'\"»”]{2,})['\"»”]")
# последовательность слов с Заглавной, допускает дефис: «Волго-Вятский банк»
_CAP_SEQ_RE = re.compile(
    r"\b[А-ЯЁ][а-яёЁ]*(?:-[А-ЯЁ][а-яёЁ]*)*"
    r"(?:\s+[А-ЯЁ][а-яёЁ]*){0,4}"
)
# предлоги, после которых обычно идёт значение фильтра
_PREP_RE = re.compile(r"\b(?:по|в|во|за|на|для)\s+([^,]{2,60})", re.IGNORECASE)

# слова, которые сами по себе не являются значениями фильтра (шум извлечения)
_STOP = {
    "выгрузи", "выгрузка", "отчёт", "отчет", "покажи", "дай", "сделай", "нужно",
    "по", "в", "во", "за", "на", "для", "и", "с", "со", "год", "году", "года",
    "процесс", "процессу", "процесса", "банк", "банку", "банка", "инцидент",
    "инциденты", "ior", "ИОР", "ввб", "всё", "все", "про",
}

# окончания падежей рус. прилагательных -> попытка привести к именительному.
# не словарь-хардкод значений, а чисто морфологическая нормализация хвоста,
# чтобы «Волго-Вятскому» дало кандидат «Волго-Вятский» для search_values.
_ADJ_ENDINGS = ("ому", "ого", "ому", "ым", "ом", "ой", "ую", "ая", "ое",
                "ые", "ых", "ыми", "ему", "его", "ем", "ий", "ый", "ому")


def _adj_nominative_variants(word: str) -> list[str]:
    """Грубые морфо-варианты слова: исходное + усечение падежного окончания
    с восстановлением именительного («Волго-Вятскому» -> «Волго-Вятский»).
    Применяем к каждому дефисному куску, чтобы покрыть «Северо-Западному» и т.п."""
    variants = {word}
    parts = word.split("-")
    fixed_parts = []
    changed = False
    for p in parts:
        low = p.lower()
        stem = None
        for end in sorted(_ADJ_ENDINGS, key=len, reverse=True):
            if low.endswith(end) and len(low) - len(end) >= 3:
                stem = p[:-len(end)]
                break
        if stem is None:
            # не похоже на склонённое прилагательное – оставить как есть
            fixed_parts.append(p)
            continue

        # типичный именительный прилагательного: стем + «ий»/«ый»
        cand = stem + ("ий" if stem[-1].lower() in "кгхчшщж" else "ый")
        if cand.lower() != low:
            changed = True
        fixed_parts.append(cand)
    if changed:
        variants.add("-".join(fixed_parts))
    return list(variants)


def _extract_phrases(user_query: str) -> list[str]:
    """Фразы-кандидаты фильтров из текста запроса.

    Эвристика (без хардкода значений):
      • куски в кавычках;
      • коды процессов/событий (regex);
      • последовательности слов с Заглавной («Волго-Вятский банк»);
      • хвосты после предлогов «по/в/за/на/для» и куски между запятыми;
      • отдельные значимые слова (для устойчивости к падежам) + их
        морфо-варианты именительного падежа.
    Дедуп с сохранением порядка; шумовые стоп-слова отброшены."""
    phrases: list[str] = []
    seen: set = set()

    def add(p: str) -> None:
        p = p.strip(".,;:!?()\"'").strip()
        if len(p) < 2:
            return
        if p.lower() in _STOP:
            return
        key = p.lower()
        if key not in seen:
            seen.add(key)
            phrases.append(p)

    # 1) кавычки
    for m in _QUOTED_RE.finditer(user_query):
        add(m.group(1))

    # 2) коды
    for m in _CODE_RE.finditer(user_query):
        add(m.group(0))

    # 3) куски между запятыми
    for chunk in re.split(r"[,;]", user_query):
        add(chunk)

    # 4) хвосты после предлогов
    for m in _PREP_RE.finditer(user_query):
        add(m.group(1))

    # 5) последовательности с Заглавной (исходные)
    for m in _CAP_SEQ_RE.finditer(user_query):
        add(m.group(0))

    # 6) отдельные значимые слова + морфо-варианты именительного
    for w in re.findall(r"[А-ЯЁа-яёЁ-]+|[Пп]\d{2,}|EVE-\d+", user_query):
        add(w)
        for v in _adj_nominative_variants(w):
            add(v)

    return phrases


def _is_code(phrase: str) -> bool:
    return bool(_CODE_RE.fullmatch(phrase.strip()))


def ground_query(user_query: str, max_hits: int = 10) -> list[dict]:
    """Заземлить запрос: фразы-кандидаты -> реальные (колонка, значение) витрины.

    Для каждой извлечённой фразы зовёт search_values и оставляет уверенные хиты
    (score >= 0.85). Для кодов-префиксов (П1227, EVE-...) порог ниже, но требуется
    префиксное совпадение значения – это всё ещё уверенно. Аббревиатуры
    (СЭБ/ВВБ) НЕ разворачиваются хардкодом – их развернёт LLM.

    Returns: список dict {phrase, column, value, count, score}, дедуп по
    (phrase, column, value), сорт по (score↓, count↓), кап max_hits.
    Пустой список – норм (ничего уверенно не заземлилось)."""
    if not user_query or not user_query.strip():
        return []

    hits: list[dict] = []
    seen: set = set()

    for phrase in _extract_phrases(user_query):
        code = _is_code(phrase)
        min_score = _CODE_MIN if code else _GROUND_STRONG
        for cand in search_values(phrase, top_k=3, min_score=min_score):
            if cand.score < min_score:
                continue
            if code:
                # код должен реально быть ПРЕФИКСОМ значения (а не случайный fuzzy)
                pref = phrase.strip().lower()
                cval = cand.value.lower()
                if not (cval.startswith(pref) or cval.startswith(pref + " ")):
                    continue
            key = (phrase.lower(), cand.column, cand.value)
            if key in seen:
                continue
            seen.add(key)
            hits.append({
                "phrase": phrase,
                "column": cand.column,
                "value": cand.value,
                "count": cand.count,
                "score": round(cand.score, 3),
            })

    hits.sort(key=lambda h: (h["score"], h["count"]), reverse=True)
    return hits[:max_hits]