"""
value_search – fuzzy-ретривал по РЕАЛЬНОМУ каталогу значений витрины.

Это грауд-примитив для LLM: вместо того чтобы угадывать колонку, модель
вызывает search_values("волговятский банк") и получает топ реальных
кандидатов (колонка, значение, count, filled_pct), УСТОЙЧИВО к опечаткам
и иным формулировкам. Дальше модель сама ВЫБИРАЕТ.

Заменяет хрупкий exact/substring-каскад value_resolver + хардкод synonyms:
   • опечатки -> difflib ratio («Сибрский» ≈ «Сибирский»)
   • дефис/пробел/регистр -> «tight»-нормализация («волговятский» c «волго-вятский банк»)
   • подстрока -> значение внутри длинной фразы и наоборот
   • аббревиатуры (СЭБ) НЕ матчатся здесь намеренно – их разворачивает сам LLM
     («СЭБ» -> ищет «Северо-Западный»), это и есть «LLM лучше хардкода».

Источник истины – kb_value_catalog.json / kb_value_index.json (сняты с реальной
витрины). Никаких curated-правил. Чистый ретривал; решение – за вызывающим (LLM).

search_values("Волго-Вятский банк") -> [Candidate(column='org_struct_lvl_3_name', score=1.0, ...)]
search_values("волговятский")        -> [Candidate(column='org_struct_lvl_3_name', match='tight_sub', ...)]
search_values("Сибрский")           -> [Candidate(column='org_struct_lvl_3_name', match='fuzzy', ...)]
search_values("экзюбра")            -> []  # ничего похожего -> пусто (НЕ молчаливый дамп выше по стеку)
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

from backend.agent.resolve.catalog import load_catalog, load_index, split_col


@dataclass
class Candidate:
    column: str          # короткое имя колонки (без префикса таблицы)
    table: str
    value: str           # реальное значение из витрины (канон)
    count: int           # сколько строк с этим значением
    filled_pct: Optional[float]
    score: float         # 0..1 – насколько похоже на запрос
    match: str           # exact | tight_sub | token | fuzzy

    def to_llm(self) -> dict:
        d = {"column": self.column, "value": self.value,
             "count": self.count, "score": round(self.score, 3),
             "match": self.match}
        if self.filled_pct is not None:
            d["filled_pct"] = self.filled_pct
        return d


_WS_RE = re.compile(r"\s+")
_NONALNUM_RE = re.compile(r"[^0-9a-za-яё]+", re.IGNORECASE)


def _norm(s: str) -> str:
    """lower + схлопывание пробелов."""
    return _WS_RE.sub(" ", str(s or "").strip().lower())


def _tight(s: str) -> str:
    """Убрать ВСЁ кроме букв/цифр: 'волго-вятский банк' -> 'волговятскийбанк'.
    Снимает различия дефис/пробел/кавычки/регистр."""
    return _NONALNUM_RE.sub("", str(s or "").lower())


def _tokens(s: str) -> set:
    return {t for t in _norm(s).split(" ") if len(t) >= 2}


@dataclass(frozen=True)
class _Entry:
    full_col: str
    value: str
    count: int
    filled_pct: Optional[float]
    norm: str
    tight: str
    tokens: frozenset


@functools.lru_cache(maxsize=1)
def _universe() -> list:
    """Плоский список всех (колонка, значение) из реального каталога – кэш."""
    cat = load_catalog()
    out: list[_Entry] = []
    for full_col, info in cat.get("columns", {}).items():
        filled = info.get("filled_pct")
        counts = info.get("counts", {})
        for v in info.get("values", []):
            sv = str(v)
            out.append(_Entry(
                full_col=full_col, value=sv,
                count=int(counts.get(v, counts.get(sv, 0)) or 0),
                filled_pct=filled,
                norm=_norm(sv), tight=_tight(sv), tokens=frozenset(_tokens(sv)),
            ))
    return out


def _score(q_norm: str, q_tight: str, q_tokens: frozenset, e: _Entry) -> tuple[float, str]:
    """Лучший скор похожести запроса на одно значение каталога + вид матча."""
    # 1) точное (после нормализации)
    if q_norm == e.norm:
        return 1.0, "exact"

    best, kind = 0.0, "none"

    # 2) tight-substring в обе стороны (дефис/пробел/опечатки слитности)
    if q_tight and e.tight:
        if q_tight in e.tight:
            # запрос – часть значения: чем длиннее покрытие, тем выше
            s = 0.72 + 0.25 * (len(q_tight) / max(len(e.tight), 1))
            if s > best:
                best, kind = min(s, 0.98), "tight_sub"
        elif e.tight in q_tight:
            # значение – часть длинной фразы запроса
            s = 0.62 + 0.20 * (len(e.tight) / max(len(q_tight), 1))
            if s > best:
                best, kind = min(s, 0.9), "tight_sub"

    # 3) пересечение токенов (Jaccard) – для многословных
    if q_tokens and e.tokens:
        inter = len(q_tokens & e.tokens)
        if inter:
            jac = inter / len(q_tokens | e.tokens)
            s = 0.55 + 0.4 * jac
            if s > best:
                best, kind = s, "token"

    # 4) символьная похожесть (опечатки): difflib на tight-формах
    if q_tight and e.tight and abs(len(q_tight) - len(e.tight)) <= max(4, len(e.tight) // 2):
        ratio = SequenceMatcher(None, q_tight, e.tight).ratio()
        if ratio > best:
            best, kind = ratio, "fuzzy"

    return best, kind


def search_values(query: str, top_k: int = 8, min_score: float = 0.6,
                  columns: Optional[list] = None) -> list[Candidate]:
    """Топ реальных кандидатов (колонка, значение) под фразу. Устойчиво к опечаткам.

    Args:
        query: фраза пользователя/намёк фильтра («волговятский банк», «Штрафные санкции»).
        top_k: сколько кандидатов вернуть.
        min_score: порог отсечки (0..1). Ниже – считаем «не похоже».
        columns: если задан – искать только в этих колонках (короткие имена).

    Returns: список Candidate, отсортирован по (score↓, count↓), дедуп по (колонка, значение).
    Пустой список = ничего похожего (вызывающий решает ask_user, НЕ молчаливый дамп).
    """
    q_norm = _norm(query)
    if not q_norm:
        return []
    q_tight = _tight(query)
    q_tokens = frozenset(_tokens(query))

    # Быстрый путь: точное попадание по inverted-индексу
    idx = load_index()
    exact_hits = idx.get(q_norm)

    col_filter = set(columns) if columns else None
    scored: list[Candidate] = []
    seen: set = set()

    for e in _universe():
        short = split_col(e.full_col)[1]
        if col_filter and short not in col_filter:
            continue
        s, kind = _score(q_norm, q_tight, q_tokens, e)
        if s < min_score:
            continue
        key = (short, e.value)
        if key in seen:
            continue
        seen.add(key)
        scored.append(Candidate(
            column=short, table=split_col(e.full_col)[0], value=e.value,
            count=e.count, filled_pct=e.filled_pct, score=s, match=kind,
        ))

    scored.sort(key=lambda c: (c.score, c.count), reverse=True)
    return scored[:top_k]