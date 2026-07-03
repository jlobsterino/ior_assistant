"""
query_spec - декларативный JSON-IR одной выгрузки + детерминированный компилятор.

См. docs/ФАЗА_В_ДИЗАЙН.md §2.2 (схема) / §2.3 (сигнатуры) / §2.4 (порядок) /
§2.5 (деньги/период/граунд) / §2.6 (маппинг на тулы) / §3.8 (гард пустого df).

ИМПОРТ-ГИГИЕНА (ОФЛАЙН-БЕЗОПАСНЫЙ модуль): на уровне модуля импортируем ТОЛЬКО
dataclasses/typing/datetime + schema_loader + resolve_value_search +
resolve.period_parser + resolve.grounding (все import-safe, без pydantic_settings).
НИКАКИХ backend.data / backend.config / backend.agent.tools.dataframe_ops на уровне
модуля - они тянут pydantic_settings -> офлайн-тест упадёт на импорте.
get_schema()/get_data_store()/REGISTRY и pandas - ЛЕНИВО внутри функций.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

from backend.agent.resolve.grounding import GROUND_STRONG
from backend.agent.resolve.period_parser import parse_period
from backend.agent.resolve.value_search import search_values
from backend.agent.schema.loader import Schema
from backend.agent.status import fmt_int, human_table

# ----- константы-пороги -------------------------------------------------------

# Money-гард ПО МЕТАДАННЫМ: main-колонка с filled_pct ниже этого порога
# блокируется в metric/range (см. §2.5). Единая конфигурируемая константа,
# НЕ по подстроке имени. main-суммы заполнены ~2.26% -> отлично ловятся порогом 5.0.
MONEY_FILLED_PCT = 5.0

# main-таблица БЗ ИОР (источник по умолчанию для FK-проверок).
MAIN_TABLE = "d6_base_of_knowledge_ior"
JOIN_KEY = "incdnt_id"

_AGG_FNS = {"sum", "mean", "count", "max", "min", "nunique"}
_DERIVED_OPS = {"add", "sub", "mul", "div", "safe_div"}

# Производные деньги-алиасы, которые ЯВНО исключены из money-гарда (§2.5):
# содержат 'sum', но рождены join+pre_aggregate / derived_metrics, не main-колонки.
# (Это подстраховка для общеизвестных имён; основной критерий - derived/alias-сет.)
_DERIVED_MONEY_WHITELIST = {"direct_loss_sum", "recovery_sum", "net_loss"}

# ----- dataclasses (§2.3) -----------------------------------------------------

@dataclass
class CompileContext:
    ctx: Any                    # SessionState - register_dataframe / get_df / files
    emit: Optional[Callable]    # progress-события (как у executor); может быть None
    schema: Schema              # белые списки таблиц/колонок/FK/filled_pct
    now: date


@dataclass
class CompileResult:
    ok: bool
    df_id: Optional[str] = None
    file_id: Optional[str] = None
    spec_resolved: Optional[dict] = None     # IR после period_parser/граунд - для нарратора
    error: Optional[str] = None              # actionable, в формате diagnose_empty
    warnings: list = field(default_factory=list)  # незаполн. значение, money-колонка main...
    lineage: list = field(default_factory=list)   # df_id по блокам - sanity/отладка
    funnel: list = field(default_factory=list)    # [{stage, rows}] воронка для UI
    # опц. поля для паритета с run_preset (UI), §5.2:
    stats: Optional[dict] = None
    dossier: Optional[dict] = None
    followups: Optional[list] = None
    skill_id: Optional[str] = None
    skill_title: Optional[str] = None

# ----- schema-хелперы (без store) ---------------------------------------------

def _table(schema: Schema, name: str):
    return schema.get(name)


def _main_columns(schema: Schema, table: str) -> set:
    t = _table(schema, table)
    return set(t.column_names()) if t else set()


def _fk_join_tables(schema: Schema, source: str) -> dict:
    """Таблицы, у которых есть FK на source по JOIN_KEY -> {table: fk_dict}.

    В YAML FK объявлены НА related-таблицах (fin_impact.incdnt_id ->
    ior.incdnt_id), а НЕ на main. Поэтому «join.table ∈ FK(source)» = related,
    у которой foreign_keys[].references начинается с '{source}.' и column==JOIN_KEY.
    """
    out: dict = {}
    for name, t in schema.tables.items():
        if name == source:
            continue
        for fk in (t.foreign_keys or []):
            ref = str(fk.get("references", ""))
            col = fk.get("column")
            if col == JOIN_KEY and (ref == f"{source}.{JOIN_KEY}"
                                    or ref.startswith(f"{source}.")):
                out[name] = fk
                break
    return out


def _col_filled_pct(schema: Schema, table: str, col: str) -> Optional[float]:
    t = _table(schema, table)
    if t is None:
        return None
    c = next((x for x in t.columns if x.name == col), None)
    return c.filled_pct if c else None

# ----- money-гард ПО МЕТАДАННЫМ (§2.5) ----------------------------------------

def is_money_main_col(col: str, schema: Schema, source: str = MAIN_TABLE) -> bool:
    """True -> колонка блокируется в metric/range (деньги бери join к fin_impact).

    Критерий ПО МЕТАДАННЫМ, НЕ по подстроке имени (§2.5):
      (а) колонка принадлежит main-таблице (source), И
      (б) schema filled_pct < MONEY_FILLED_PCT (заполнена редко -> суммы пусты), И
      (в) это ИСХОДНАЯ колонка схемы (derived/aggregate-alias сюда не попадают,
          т.к. их в схеме нет - проверка по column_names()).
    Производные direct_loss_sum/recovery_sum/net_loss НЕ main-колонки -> False.
    """
    if col in _DERIVED_MONEY_WHITELIST:
        return False
    if col not in _main_columns(schema, source):
        return False
    pct = _col_filled_pct(schema, source, col)
    return pct is not None and pct < MONEY_FILLED_PCT


# ----- сбор имён производных/алиасов (для валидации колонок) ------------------

def _join_select_names(spec: dict) -> set:
    """Имена, появляющиеся из join.select / join.pre_aggregate.*.as."""
    names: set = set()
    for j in ((spec.get("source") or {}).get("joins") or []):
        for s in (j.get("select") or []):
            names.add(s)
        pa = j.get("pre_aggregate") or {}
        for _src, body in (pa.get("agg") or {}).items():
            if isinstance(body, dict) and body.get("as"):
                names.add(body["as"])
    return names


def _derive_names(spec: dict) -> set:
    return {d.get("new_column") for d in spec.get("derive") or [] if d.get("new_column")}


def _aggregate_metric_names(spec: dict) -> set:
    agg = spec.get("aggregate") or {}
    return {m.get("as") for m in agg.get("metrics") or [] if m.get("as")}


def _derived_metric_names(spec: dict) -> set:
    return {m.get("as") for m in spec.get("derived_metrics") or [] if m.get("as")}


def _all_known_names(spec: dict, schema: Schema, source: str) -> set:
    """Все имена, на которые МОЖНО ссылаться: main-колонки + join.select +
    pre_aggregate.as + derive + aggregate_metrics.as + derived_metrics.as.
    """
    return (_main_columns(schema, source) | _join_select_names(spec)
            | _derive_names(spec) | _aggregate_metric_names(spec)
            | _derived_metric_names(spec))

# ----- OR-детектор (§2.2-bis) -------------------------------------------------

def _has_or_construct(obj: Any) -> bool:
    """'any_of' / вложенный _or / op=='or' где угодно в структуре фильтров."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in ("any_of", "_or", "or"):
                return True
            if kl == "op" and isinstance(v, str) and v.lower() == "or":
                return True
            if _has_or_construct(v):
                return True
    elif isinstance(obj, list):
        return any(_has_or_construct(x) for x in obj)
    return False


# ----- range-стадия (§2.4) ----------------------------------------------------

def classify_range_stage(spec: dict, schema: Schema) -> dict:
    """{"column": 'pre_source'|'post_join'|'post_aggregate'} для каждого range-фильтра.

    Стадия ДЕТЕРМИНИРОВАННА из объявленных колонок (НЕ угадывается):
      pre_source     - колонка ∈ main-схема (-> where_main, SQL до source);
      post_join      - колонка ∈ join.select / pre_aggregate.as (маска после join);
      post_aggregate - колонка ∈ aggregate.metrics.as / derived_metrics.as.
    Колонка ни в одной стадии -> исключение ValueError (validate_spec ловит её РАНЬШЕ
    и возвращает actionable-строку, так что в рантайме KeyError не случится).
    """
    source = (spec.get("source") or {}).get("table") or MAIN_TABLE
    main_cols = _main_columns(schema, source)
    post_join = _join_select_names(spec)
    post_agg = _aggregate_metric_names(spec) | _derived_metric_names(spec)
    out: dict = {}
    for f in (spec.get("filters") or []):
        if not isinstance(f, dict) or (f.get("kind") or "").lower() != "range":
            continue
        col = f.get("column")
        # # НЕОДНОЗНАЧНОСТЬ: имя есть и в join.select/pre_aggregate (per-incident), и в
        # # aggregate/derived (агрегат по группе) - стадия применения непонятна, а это
        # # ДВЕ разные семантики (фильтр инцидентов vs фильтр групп). Молча выбрать
        # # стадию - молча неверный результат. Требуем РАЗНЫЕ имена.
        if col in post_join and col in post_agg:
            raise ValueError(
                f"range-фильтр по {col!r}: имя есть и в join.select/pre_aggregate "
                f"('значение per-incident), и в aggregate/derived (агрегат по группе) - "
                f"стадия неоднозначна. Переименуй один из агрегатов "
                f"в РАЗНЫЕ имена (напр. 'direct_loss' до агрегата, 'direct_loss_sum' после) "
                f"и поставь range на нужную из них."
            )
        if col in post_agg:
            out[col] = "post_aggregate"
        elif col in post_join:
            out[col] = "post_join"
        elif col in main_cols:
            out[col] = "pre_source"
        else:
            raise ValueError(
                f"range-фильтр по колонке {col!r}: не принадлежит ни main-схеме, "
                f"ни join.select/pre_aggregate, ни aggregate/derived - стадия неизвестна"
            )
    return out

# ----- validate_spec (§2.3) ---------------------------------------------------

def validate_spec(spec: dict, schema: Schema) -> Optional[str]:
    """Чисто-функциональная валидация IR. None (ок) или actionable-строка.

    БЕЗ обращения к store. Граунд категориального - повторная сверка через
    search_values по ЕДИНОЙ константе GROUND_STRONG (§2.5).
    """
    if not isinstance(spec, dict):
        return "QuerySpec должен быть объектом (dict)."

    # --- source.table - (по умолчанию - единственная main-таблица БЗ ИОР;
    # модель не обязана её указывать, это устраняет класс ошибки «source.table обязатесен»)
    src = spec.get("source") or {}
    table = src.get("table") or MAIN_TABLE
    if table not in schema.table_names():
        return (f"source.table {table!r} нет в схеме. "
                f"Доступны: {schema.table_names()}.")
    main_cols = _main_columns(schema, table)

    # --- joins: ∈ FK(source) и on=='incdnt_id'; 1:N без pre_aggregate -> error —
    fk_tables = _fk_join_tables(schema, table)
    for j in (src.get("joins") or []):
        jt = j.get("table")
        if jt not in fk_tables:
            return (f"join.table {jt!r} не связан с {table} по FK. "
                    f"Доступны для join: {sorted(fk_tables.keys())}.")
        if j.get("on") != JOIN_KEY:
            return (f"join к {jt!r} on={j.get('on')!r}, а должно быть "
                    f"{JOIN_KEY!r} (join всегда по {JOIN_KEY}).")
        # related-таблицы many-to-per-incident -> без pre_aggregate fan-out 1:N
        if not j.get("pre_aggregate"):
            return (f"join к {jt!r} без pre_aggregate -> fan-out 1:N (двойной счёт). "
                    f"Нужен pre_aggregate (group_by [{JOIN_KEY}] + agg sum/count).")
        pa = j.get("pre_aggregate") or {}
        # колонки-источники pre_aggregate.agg должны быть в related-схеме
        rel_cols = _main_columns(schema, jt)
        for gb in (pa.get("group_by") or []):
            if gb not in rel_cols:
                return f"pre_aggregate.group_by {gb!r} нет в таблице {jt}."
        for srccol, body in (pa.get("agg") or {}).items():
            if srccol not in rel_cols:
                return f"pre_aggregate.agg источник {srccol!r} нет в таблице {jt}."
            fn = (body or {}).get("fn") if isinstance(body, dict) else None
            if fn not in _AGG_FNS:
                return (f"pre_aggregate.agg fn={fn!r} не поддержан "
                        f"(допустимо: {sorted(_AGG_FNS)}).")
            # фильтр pre_aggregate не должен содержать OR
            if _has_or_construct((body or {}).get("filter")):
                return "OR не поддержан в v1 (any_of/_or/op:or в pre_aggregate.filter)."

    known = _all_known_names(spec, schema, table)

    # --- OR-конструкты в любом filter (§2.2-bis) —
    if _has_or_construct(spec.get("filters")):
        return ("OR-фильтр (any_of) в QuerySpec v1 не поддержан (store молча "
                "отбрасывает _or). Разбей на отдельные выгрузки и объедини, либо "
                "используй фильтр-IN ({col:[a,b]}) если это OR по ОДНОЙ колонке.")

    # --- derive: источник ∈ схема/known —
    for d in (spec.get("derive") or []):
        s = d.get("source")
        if s not in known:
            return f"derive.source {s!r} не найден среди колонок/производных."

    # --- aggregate —
    agg = spec.get("aggregate") or {}
    if agg:
        for gb in (agg.get("group_by") or []):
            if gb not in known:
                return f"aggregate.group_by {gb!r} не найден среди колонок/производных."
        for m in (agg.get("metrics") or []):
            fn = m.get("fn")
            if fn not in _AGG_FNS:
                return (f"aggregate.metrics fn={fn!r} не поддержан "
                        f"(допустимо: {sorted(_AGG_FNS)}).")
            msrc = m.get("source")
            if msrc not in known:
                return f"aggregate.metrics.source {msrc!r} не найден."
            if fn == "sum" and is_money_main_col(msrc, schema, table):
                # money-гард: исходная main-сумма как metric -> blocker
                return (f"metrics: сумма по money-колонке main {msrc!r} (filled "
                        f"<{MONEY_FILLED_PCT}%) - суммы main почти пусты. "
                        f"Бери деньги через join к fin_impact/recovery "
                        f"(pre_aggregate sum по JOIN_KEY).")

    # --- derived_metrics: белый список ops ---
    for dm in (spec.get("derived_metrics") or []):
        expr = dm.get("expr") or {}
        op = expr.get("op")
        if op not in _DERIVED_OPS:
            return (f"derived_metrics op={op!r} не поддержан "
                    f"(допустимо: {sorted(_DERIVED_OPS)}).")
        for side in ("left", "right"):
            ref = expr.get(side)
            # ref может быть числом-литералом или именем
            if isinstance(ref, str) and ref not in known:
                return f"derived_metrics.{side} {ref!r} не найден среди колонок."

    # --- filters: money-range, categorical-граунд, range-стадия —
    for f in (spec.get("filters") or []):
        if not isinstance(f, dict):
            return (f"каждый фильтр должен быть объектом, а не {type(f).__name__} "
                    f"({f!r}). Пример: {{\"kind\":\"categorical\",\"column\":\"...\", "
                    f"\"op\":\"eq\",\"value\":\"...\",\"grounded\":true}} или "
                    f"\"{{\\\"kind\\\":\\\"period\\\",\\\"intent\\\":\\\"{{\\\\\\\"text\\\\\\\":\\\\\\\"Q1 2026\\\\\\\"}}\\\",\" "
                    f"\"\\\\\\\"column\\\\\\\":\\\\\\\"incdnt_entry_dt\\\\\\\",\\\\\\\"required\\\\\\\":true}}\"")
        kind = (f.get("kind") or "").lower()
        col = f.get("column")

        if kind == "period":
            if col and col not in main_cols:
                return f"period.column {col!r} нет в main-схеме."
            continue

        if kind == "range":
            # money-гард: range по исходной main-сумме -> blocker
            if is_money_main_col(col, schema, table):
                return (f"range-фильтр по money-колонке main {col!r} (filled "
                        f"<{MONEY_FILLED_PCT}%) - суммы main почти пусты. Бери "
                        f"деньги через join к fin_impact/recovery.")
            continue  # стадия проверяется ниже общим classify_range_stage

        if kind == "categorical":
            if col not in main_cols:
                return f"categorical.column {col!r} нет в main-схеме."
            if not f.get("grounded"):
                return (f"categorical-фильтр {col!r}={f.get('value')!r} без "
                        f"grounded:true. Заземли значение через search_values "
                        f"(граунд категориального обязателен, инвариант 2).")
            # повторная сверка по ЕДИНОЙ константе GROUND_STRONG (§2.5)
            value = f.get("value")
            if isinstance(value, str) and value.strip():
                hits = search_values(value, columns=[col],
                                     min_score=GROUND_STRONG, top_k=3)
                if not hits or hits[0].score < GROUND_STRONG:
                    elsewhere = search_values(value, min_score=GROUND_STRONG, top_k=3)
                    corr = ""
                    if elsewhere:
                        e = elsewhere[0]
                        corr = (f" Реально найдено в колонке '{e.column}' "
                                f"(например '{e.value}').")
                    return (f"categorical {col!r}={value!r} НЕ заземлено "
                            f"в этой колонке (score<{GROUND_STRONG}).{corr}")
            continue

        if kind == "like":
            # free_text -> граунд НЕ применяется (§2.5); колонка должна быть в схеме
            if col not in known:
                return f"like.column {col!r} нет среди колонок."
            continue

        # неизвестный kind
        if kind:
            return (f"filter.kind {kind!r} не поддержан "
                    f"(period|categorical|range|like).")

    # --- range-стадия для каждого range (детерминированно; ловим неизвестную колонку) —
    try:
        classify_range_stage(spec, schema)
    except ValueError as e:
        return str(e)

    # --- window/sort/select ссылаются на существующие/деривованные имена —
    for w in (spec.get("window") or []):
        for pb in (w.get("partition_by") or []):
            if pb not in known:
                return f"window.partition_by {pb!r} не найден среди колонок."
        ob = w.get("order_by")
        if ob and ob not in known:
            return f"window.order_by {ob!r} не найден среди колонок."
    for s in (spec.get("sort") or []):
        by = s.get("by")
        if by and by not in known:
            return f"sort.by {by!r} не найден среди колонок."
    for c in (spec.get("select") or []):
        if c not in known:
            return f"select-колонка {c!r} не найдена среди колонок/производных."

    return None

# ----- period-хелпер (§2.4 шаг 1) ---------------------------------------------

def expand_period_filters(spec: dict, now: date) -> tuple:
    """(where_period, labels). None&required -> спец-ошибка (строка) вместо where.

    Возвращает либо (dict, dict), либо (str-ошибка, {}) если required-период не
    распарсился (НЕ молчаливый полный дамп - инвариант 1+3).
    """
    where: dict = {}
    labels: dict = {}
    for f in (spec.get("filters") or []):
        if not isinstance(f, dict) or (f.get("kind") or "").lower() != "period":
            continue
        # LLM кладёт intent то объектом {"text":...}, то прямой строкой "Q1 2026" -
        # принимаем оба, плюс fallback на f["text"]/f["value"].
        intent = f.get("intent")
        if isinstance(intent, dict):
            text = intent.get("text") or ""
        elif isinstance(intent, str):
            text = intent
        else:
            text = f.get("text") or f.get("value") or ""
        col = f.get("column") or "incdnt_entry_dt"
        period = parse_period(text, column=col)
        if period is None:
            if f.get("required"):
                return (f"Период '{text}' не распознан, а он обязателен - уточни "
                        f"год/квартал (даты не выгружаются, инвариант 3).", {})
            continue
        where[f"{col}__gte"] = period.start
        where[f"{col}__lt"] = period.end
        labels[col] = period.label
    return where, labels

# ----- build_main_where (§2.4 шаг 2а) -----------------------------------------

def build_main_where(spec: dict, schema: Schema, now: Optional[date] = None) -> dict:
    """Категориальные / range(pre_source) / period -> where для query-тула.

    Форма where-тула: {col:val} | {col:[in]} | {col__op:v}. range(post_join/
    post_aggregate) сюда НЕ попадают - они применяются pandas-маской позже.
    """
    table = (spec.get("source") or {}).get("table") or MAIN_TABLE
    where: dict = {}

    # period
    pw, _labels = expand_period_filters(spec, now or date.today())
    if isinstance(pw, dict):
        where.update(pw)

    stages = classify_range_stage(spec, schema)

    for f in (spec.get("filters") or []):
        if not isinstance(f, dict):
            continue
        kind = (f.get("kind") or "").lower()
        col = f.get("column")
        op = (f.get("op") or "eq").lower()
        value = f.get("value")

        if kind == "categorical":
            if op == "in" and isinstance(value, list):
                where[col] = value
            elif op in ("eq", "="):
                where[col] = value
            elif op == "like":
                where[f"{col}__like"] = value
            elif op == "ne":
                where[f"{col}__ne"] = value
            else:
                where[f"{col}_{op}"] = value

        elif kind == "range" and stages.get(col) == "pre_source":
            opmap = {"gt": "gt", ">": "gt", "gte": "gte", ">=": "gte",
                     "lt": "lt", "<": "lt", "lte": "lte", "<=": "lte"}
            where[f"{col}__{opmap.get(op, op)}"] = value

        elif kind == "like" and f.get("free_text"):
            v = value if isinstance(value, str) and "%" in str(value) else f"%{value}%"
            where[f"{col}__like"] = v

    return where

# ----- pandas-хелперы (ленивый импорт pandas) ---------------------------------

def eval_derived_metric(df, dm: dict):
    """pandas.Series для одного derived_metric. Белый список ops; div+safe_div (0->NaN)."""
    import numpy as np
    import pandas as pd  # noqa: F401 (доступность гарантируем ленивым импортом)
    
    expr = dm.get("expr") or {}
    op = expr.get("op")
    if op not in _DERIVED_OPS:
        raise ValueError(f"derived op {op!r} не в белом списке {sorted(_DERIVED_OPS)}")
        
    def _operand(ref):
        if isinstance(ref, (int, float)):
            return ref
        return df[ref]
        
    left = _operand(expr.get("left"))
    right = _operand(expr.get("right"))
    
    if op == "add":
        return left + right
    if op == "sub":
        return left - right
    if op == "mul":
        return left * right
    if op in ("div", "safe_div"):
        # safe_div: деление на 0 -> NaN (а не inf / ZeroDivisionError)
        rser = right if hasattr(right, "replace") else pd.Series(right, index=df.index)
        return left / rser.replace(0, np.nan)
    raise ValueError(f"derived op {op!r} не реализован")


def is_empty_df(df) -> bool:
    """Гард §3.8: финальный df пуст -> нечего выгружать."""
    return df is None or len(df) == 0


def grounding_warnings(spec: dict) -> list:
    """Честность (П4): где категориальное значение заземлено НЕ идеально - пометка.
    
    Если value сопоставлено с колонкой со score в зоне [GROUND_STRONG, ~0.96) - 
    агент «додумал», и аудитор должен это видеть, чтобы проверить колонку."""
    out: list = []
    for f in (spec.get("filters") or []):
        if not isinstance(f, dict) or (f.get("kind") or "").lower() != "categorical":
            continue
        if f.get("free_text"):
            continue
            
        val, col = f.get("value"), f.get("column")
        if not isinstance(val, str) or not val.strip():
            continue
            
        hits = search_values(val, columns=[col], top_k=1)
        sc = hits[0].score if hits else 0.0
        if GROUND_STRONG <= sc <= 0.96:
            out.append(f"Значение '{val}' сопоставлено с колонкой {col} с уверенностью "
                       f"{int(sc * 100)}% - при сомнении проверьте.")
    return out


def detect_fanout(right_df, on) -> bool:
    """right[on].duplicated().any() - 1:N fan-out при join (двойной счёт)."""
    if right_df is None:
        return False
    keys = on if isinstance(on, list) else [on]
    try:
        return bool(right_df.duplicated(subset=keys).any())
    except Exception:
        # колонки ключа нет - пусть join_dfs вернет свою ошибку, не считаем fan-out
        return False


# ----- compile_query_spec (§2.4) - async, совет REGISTRY (ленивый импорт) -----


async def compile_query_spec(cctx: CompileContext, spec: dict,
                             registry=None) -> CompileResult:
    """ДЕТЕРМИНИРОВАННО раскладывает IR на существующие тулы. LLM не зовётся.
    
    Порядок строго §2.4: 0 validate -> 1 period -> 2 source+joins(pre_aggregate до
    join) -> 3 derive -> 4 aggregate(*_range_post_*) -> 5 derived_metrics -> 6 window -> 
    7 sort -> 8 select-проекция -> 9 OUTPUT (гард пустого финального df §3.8). Любой
    ToolResult.ok==False -> CompileResult(ok=False, error=...). Эвиктит
    промежуточные df (оставляет финальный).
    
    registry: для тестируемости - можно ВНЕДРИТЬ реестр тулов (DI). По умолчанию
    (None) лениво берётся боевой REGISTRY. Внедрение позволяет прогнать компилятор
    офлайн на pandas-фикстурах без store/pydantic_settings (В.2).
    """
    # ленивый импорт боевого реестра - НЕ на уровне модуля (pydantic_settings офлайн).
    # При внедрённом registry импорт НЕ выполняется -> компилятор офлайн-тестируем.
    if registry is None:
        from backend.agent.tools.registry import REGISTRY as registry
        
    schema = cctx.schema
    state = cctx.ctx
    now = cctx.now or date.today()
    lineage: list = []
    warnings: list = []
    funnel: list = []                  # [{stage, rows}] - воронка строк для UI
    intermediate: list = []            # df_id для эвикции
    
    async def _emit(ev: str, payload: dict):
        if cctx.emit:
            try:
                res = cctx.emit(ev, payload)
                if hasattr(res, "__await__"):
                    await res
            except Exception:
                pass

    async def _activity(aid: str, title: str, detail=None, status: str = "active"):
        """Под-шаг компиляции для премиальной ленты статусов фронта."""
        await _emit("activity", {"id": aid, "kind": "data", "title": title,
                                 "detail": detail, "status": status})

    def _rows(did):
        try:
            if did in getattr(state, "dataframes", {}):
                return len(state.dataframes[did])
        except Exception:
            return None
        return None

    def _evict(keep: Optional[str]):
        for did in intermediate:
            if did and did != keep and did in getattr(state, "dataframes", {}):
                try:
                    del state.dataframes[did]
                    if did in getattr(state, "dataframe_meta", {}):
                        del state.dataframe_meta[did]
                except Exception:
                    pass

    # нормализация: source.table по умолчанию = единственная main-таблица
    if isinstance(spec, dict):
        spec = dict(spec)
        src = dict(spec.get("source") or {})
        src.setdefault("table", MAIN_TABLE)
        if not src.get("table"):
            src["table"] = MAIN_TABLE
        spec["source"] = src

    # --- 0. validate ---
    err = validate_spec(spec, schema)
    if err:
        return CompileResult(ok=False, error=err)

    table = spec["source"].get("table") or MAIN_TABLE
    spec_resolved = dict(spec)
    warnings = grounding_warnings(spec)   # честность (П4): где значение заземлено не идеально

    # --- 1. period ---
    period_where, labels = expand_period_filters(spec, now)
    if isinstance(period_where, str):
        return CompileResult(ok=False, error=period_where)
    if labels:
        spec_resolved["period"] = {"labels": labels}

    # --- 2a. where_main ---
    where_main = build_main_where(spec, schema, now)
    stages = classify_range_stage(spec, schema)

    # --- проекция SOURCE-запроса ---
    # БАГ «выгрузка в один столбец»: ДЕТАЛЬНАЯ (пер-инцидентная) выгрузка без
    # агрегации и без явного select - это рабочая таблица для аудитора. Ему нужен
    # ПОЛНЫЙ набор столбцов (даты, статус, тип, оргструктура, суммы, описание...),
    # а не один incdnt_id. Раньше "needed" всегда стартовал с {"incdnt_id"} и при
    # пустых select/agg/derive схлопывался ровно до него -> в Excel один столбец.
    # Решение: для детальной выгрузки берём ВСЕ колонки main (columns=None = SELECT *).
    select_cols = list(spec.get("select") or [])
    agg = spec.get("aggregate") or {}
    if not agg and not select_cols:
        # детальная выгрузка -> все столбцы main. incdnt_id и описание (FAISS-хвост
        # §5.2) входят автоматически; join-алиасы (суммы/recovery) и derive/window
        # добавятся дальше по конвейеру (§2с/§3/§6). order_by/sort не страдают.
        main_columns = None
    else:
        # агрегация ИЛИ явный select -> минимально достаточный набор:
        # join-key + pre_aggregate/aggregate group_by + источники derive + select.
        needed = {JOIN_KEY}
        for j in (spec["source"].get("joins") or []):
            for pa_gb in ((j.get("pre_aggregate") or {}).get("group_by") or []):
                needed.add(pa_gb)
        for gb in (agg.get("group_by") or []):
            needed.add(gb)
        for d in (spec.get("derive") or []):
            if d.get("source") in _main_columns(schema, table):
                needed.add(d["source"])
        # money/категориальные/period колонки - уже в where, не нужны в проекции,
        # но колонки select из main добавим
        for c in select_cols:
            if c in _main_columns(schema, table):
                needed.add(c)
        
        # Если это детальная выгрузка, всегда загружаем стандартные аудиторские колонки
        if not agg:
            standard_cols = [
                "incdnt_sid", "incdnt_id", "incdnt_status_name", "incdnt_autoreg_flag",
                "incdnt_detection_person_name", "incdnt_source_name", "src_type_lvl_1_name",
                "src_type_lvl_2_name", "incdnt_type_lvl_1_name", "incdnt_type_lvl_2_name",
                "incdnt_detection_dt", "incdnt_start_dt", "incdnt_entry_dt",
                "org_struct_lvl_3_name", "org_struct_lvl_4_name", "process_lvl_4_name",
                "incdnt_summary_descr_txt", "incdnt_full_descr_txt"
            ]
            for col in standard_cols:
                if col in _main_columns(schema, table):
                    needed.add(col)

        main_columns = [c for c in needed if c in _main_columns(schema, table)] or None

    # --- 2b. SOURCE query ---
    await _activity("data:source", "Загружаю инциденты", status="active")
    res = await registry.execute(
        "query",
        {"table": table, "where": where_main, "columns": main_columns,
         "limit": spec.get("limit") or 100_000},
        state
    )
    if not res.ok:
        await _activity("data:source", "Загрузка инциденты", "не удалось", "failed")
        return CompileResult(ok=False, error=res.error)
    cur = res.output["df_id"]
    lineage.append(cur)
    main_df_id = cur
    await _activity("data:source", "Загрузил инциденты",
                    f"{fmt_int(_rows(cur))} строк", "done")
    funnel.append({"stage": "Инциденты", "rows": _rows(cur)})

    # --- 2c. JOINS (pre_aggregate ДО join) ---
    for j in (spec["source"].get("joins") or []):
        jt = j["table"]
        pa = j["pre_aggregate"]
        pa_filter = None
        agg_src = None
        agg_fn = None
        agg_alias = None
        for srccol, body in (pa.get("agg") or {}).items():
            agg_src, agg_fn = srccol, body.get("fn")
            agg_alias = body.get("as") or f"{srccol}_{agg_fn}"
            pa_filter = body.get("filter")
            break  # один метрик на pre_aggregate в v1-маппинге

        # query related (+ фильтр типа потерь, если есть)
        await _activity(f"data:join:{jt}", f"Подключаю {human_table(jt)}", status="active")
        rel_cols = list({JOIN_KEY, agg_src} | set(pa.get("group_by") or []))
        rq = await registry.execute(
            "query",
            {"table": jt, "where": pa_filter or {}, "columns": rel_cols,
             "limit": _MAX_REL_ROWS},
            state
        )
        if not rq.ok:
            return CompileResult(ok=False, error=rq.error)
        rel_id = rq.output["df_id"]
        intermediate.append(rel_id)

        # pre_aggregate: group_by [incdnt_id] + agg + alias (кастомная pandas-логика
        # # для alias; group_by-тул не делает alias, поэтому считаем тут структурно)
        agg_id = _pre_aggregate(state, rel_id, pa.get("group_by") or [JOIN_KEY],
                                agg_src, agg_fn, agg_alias)
        intermediate.append(agg_id)

        # join_dfs (pre_aggregate гарантирует уникальность ключа -> гард проходит)
        jr = await registry.execute(
            "join_dfs",
            {"left_df": cur, "right_df": agg_id, "on": JOIN_KEY,
             "how": j.get("how") or "left"},
            state
        )
        if not jr.ok:
            await _activity(f"data:join:{jt}", f"Подключение «{human_table(jt)}»",
                            "не удалось", "failed")
            return CompileResult(ok=False, error=jr.error)
        intermediate.append(cur)
        cur = jr.output["df_id"]
        lineage.append(cur)
        await _activity(f"data:join:{jt}", f"Подключил {human_table(jt)}",
                        "готово", "done")

        # --- range post_join маски ---
        for f in (spec.get("filters") or []):
            if (f.get("kind") or "").lower() != "range":
                continue
            col = f.get("column")
            if stages.get(col) != "post_join":
                continue
            await _activity(f"data:mask:{col}", f"Фильтрую по «{col}»", status="active")
            cur = _apply_range_mask(state, cur, col, f.get("op"), f.get("value"))
            intermediate.append(cur)
            lineage.append(cur)
            await _activity(f"data:mask:{col}", f"Отфильтровал по «{col}»",
                            f"{fmt_int(_rows(cur))} строк", "done")
            funnel.append({"stage": f"Фильтр «{col}»", "rows": _rows(cur)})

    # --- 3. DERIVE ---
    for d in (spec.get("derive") or []):
        dr = await registry.execute(
            "derive_column",
            {"df_id": cur, "source": d["source"], "new_column": d["new_column"],
             "op": d.get("op", "month")},
            state
        )
        if not dr.ok:
            return CompileResult(ok=False, error=dr.error)
        intermediate.append(cur)
        cur = dr.output["df_id"]
        lineage.append(cur)

    # --- 4. AGGREGATE ---
    if agg:
        from backend.agent.status import human_column
        gb_human = ", ".join(human_column(c) for c in (agg.get("group_by") or []))
        await _activity("data:aggregate", f"Группирую по {gb_human}", status="active")
        agg_map = {m["source"]: m["fn"] for m in (agg.get("metrics") or [])}
        gr = await registry.execute(
            "group_by",
            {"df_id": cur, "by": agg.get("group_by") or [], "agg": agg_map},
            state
        )
        if not gr.ok:
            await _activity("data:aggregate", "Группировка", "не удалось", "failed")
            return CompileResult(ok=False, error=gr.error)
        intermediate.append(cur)
        cur = gr.output["df_id"]
        lineage.append(cur)
        # rename агрегированных колонок в alias-имена (group_by-тул не делает alias)
        cur = _rename_agg_aliases(state, cur, agg.get("metrics") or [])
        await _activity("data:aggregate", f"Сгруппировал по {gb_human}",
                        f"{fmt_int(_rows(cur))} групп", "done")
        funnel.append({"stage": f"Группы «{gb_human}»", "rows": _rows(cur)})

    # --- range post_aggregate маски ---
    for f in (spec.get("filters") or []):
        if (f.get("kind") or "").lower() != "range":
            continue
        col = f.get("column")
        if stages.get(col) != "post_aggregate":
            continue
        await _activity(f"data:mask:{col}", f"Фильтрую по «{col}»", status="active")
        cur = _apply_range_mask(state, cur, col, f.get("op"), f.get("value"))
        intermediate.append(cur)
        lineage.append(cur)
        await _activity(f"data:mask:{col}", f"Отфильтровал по «{col}»",
                        f"{fmt_int(_rows(cur))} строк", "done")
        funnel.append({"stage": f"Фильтр «{col}»", "rows": _rows(cur)})

    # --- 5. DERIVED_METRICS ---
    for dm in (spec.get("derived_metrics") or []):
        cur = _apply_derived_metric(state, cur, dm)
        intermediate.append(cur)
        lineage.append(cur)

    # --- 6. WINDOW ---
    for w in (spec.get("window") or []):
        wr = await registry.execute(
            "window_rank",
            {"df_id": cur, "partition_by": w.get("partition_by"),
             "order_by": w.get("order_by"), "order_desc": w.get("order_desc", True),
             "top_n": w.get("top_n"), "method": w.get("method", "row_number")},
            state
        )
        if not wr.ok:
            return CompileResult(ok=False, error=wr.error)
        intermediate.append(cur)
        cur = wr.output["df_id"]
        lineage.append(cur)

    # --- 7. SORT ---
    for s in (spec.get("sort") or []):
        sr = await registry.execute(
            "top_n",
            {"df_id": cur, "by": s.get("by"),
             "n": spec.get("limit") or 100_000, "ascending": not s.get("desc", True)},
            state
        )
        if not sr.ok:
            return CompileResult(ok=False, error=sr.error)
        intermediate.append(cur)
        cur = sr.output["df_id"]
        lineage.append(cur)

    # --- 8. SELECT-проекция финального df (Выгрузка содержит ИМЕННО select-колонки) ---
    sel = spec.get("select") or []
    if sel:
        # Для детальных выгрузок обогащаем список колонок стандартными аудиторскими полями,
        # если они были загружены из базы, чтобы не терять контекст для гипотез и графиков.
        if not spec.get("aggregate"):
            existing_cols = list(state.get_df(cur).columns)
            standard_cols = [
                "incdnt_sid", "incdnt_id", "incdnt_status_name", "incdnt_autoreg_flag",
                "incdnt_detection_person_name", "incdnt_source_name", "src_type_lvl_1_name",
                "src_type_lvl_2_name", "incdnt_type_lvl_1_name", "incdnt_type_lvl_2_name",
                "incdnt_detection_dt", "incdnt_start_dt", "incdnt_entry_dt",
                "org_struct_lvl_3_name", "org_struct_lvl_4_name", "process_lvl_4_name",
                "fin_impact_rub_amt", "direct_loss", "recovery_rub_amt", "recovery", "net_loss",
                "incdnt_summary_descr_txt", "incdnt_full_descr_txt"
            ]
            new_sel = list(sel)
            for col in standard_cols:
                if col in existing_cols and col not in new_sel:
                    new_sel.append(col)
            sel = new_sel

        cur2 = _project_columns(state, cur, sel)
        if cur2 != cur:
            intermediate.append(cur)
            cur = cur2
            lineage.append(cur)

    # --- 9. OUTPUT с гардом пустого финального df (§3.8) ---
    final_df = state.get_df(cur)
    if is_empty_df(final_df):
        _evict(None)
        return CompileResult(
            ok=False, spec_resolved=spec_resolved, lineage=lineage, warnings=warnings,
            error=("EMPTY_RESULT: после применения фильтров (диапазон/маска/агрегат) "
                   "строк не осталось - нечего выгружать. Проверьте range-порог / "
                   "категориальное значение / период, либо честно сообщите пользователю.")
        )

    out = spec.get("output") or {}
    fmt = (out.get("format") or "excel").lower()
    tool = "export_csv" if fmt == "csv" else "export_excel"
    n_final = _rows(cur)
    await _activity("data:export", "Формирую файл", status="active")
    er = await registry.execute(tool, {"df_id": cur, "name": out.get("name")}, state)
    if not er.ok:
        await _activity("data:export", "Формирование файла", "не удалось", "failed")
        _evict(cur)
        return CompileResult(ok=False, error=er.error, lineage=lineage)
    await _activity("data:export", "Файл готов",
                    f"{fmt_int(n_final)} строк", "done")
                    
    funnel.append({"stage": "Итог", "rows": n_final})

    # лист «Методология» в xlsx (П5) - для рабочего дела аудитора
    try:
        fm = getattr(state, "files", {}).get(er.output.get("file_id"))
        path = getattr(fm, "path", None)
        if path and str(path).endswith(".xlsx"):
            from backend.agent.result import write_methodology_sheet
            write_methodology_sheet(path, spec_resolved, funnel)
    except Exception:  # noqa: BLE001
        pass

    _evict(cur)  # эвиктим промежуточные, оставляем финальный df + источник
    return CompileResult(
        ok=True, df_id=cur, file_id=er.output.get("file_id"),
        spec_resolved=spec_resolved, lineage=lineage, warnings=warnings, funnel=funnel
    )

_MAX_REL_ROWS = 2_000_000


# ----- приватные pandas-хелперы для compile (ленивый pandas) -----


def _pre_aggregate(state, df_id, group_by, src, fn, alias):
    """Структурный фильтр-агрегат с alias (group_by-тул не умеет alias)."""
    df = state.get_df(df_id)
    grouped = df.groupby(group_by, dropna=False)[src].agg(fn).reset_index()
    grouped = grouped.rename(columns={src: alias})
    return state.register_dataframe(
        grouped, f"pre_aggregate({df_id}, {fn}({src})->{alias})",
        "query_spec.pre_aggregate"
    ).df_id


def _apply_range_mask(state, df_id, col, op, value):
    df = state.get_df(df_id)
    s = df[col]
    op = (op or "gt").lower()
    if op in ("gt", ">"):
        mask = s > value
    elif op in ("gte", ">="):
        mask = s >= value
    elif op in ("lt", "<"):
        mask = s < value
    elif op in ("lte", "<="):
        mask = s <= value
    elif op in ("eq", "="):
        mask = s == value
    elif op in ("ne", "!="):
        mask = s != value
    else:
        mask = s > value
    masked = df[mask.fillna(False)].reset_index(drop=True)
    return state.register_dataframe(
        masked, f"range_mask({df_id}, {col} {op} {value})",
        "query_spec.range_mask"
    ).df_id


def _rename_agg_aliases(state, df_id, metrics):
    """После group_by переименовать агрегат-колонки в alias-имена."""
    rename = {m["source"]: m["as"] for m in metrics
              if m.get("as") and m.get("as") != m.get("source")}
    if not rename:
        return df_id
    df = state.get_df(df_id)
    cols = set(df.columns)
    rename = {k: v for k, v in rename.items() if k in cols and v not in cols}
    if not rename:
        return df_id
    renamed = df.rename(columns=rename)
    return state.register_dataframe(
        renamed, f"alias({df_id})", "query_spec.alias"
    ).df_id


def _apply_derived_metric(state, df_id, dm):
    df = state.get_df(df_id).copy()
    df[dm['as']] = eval_derived_metric(df, dm)
    return state.register_dataframe(
        df, f"derived_metric({df_id}, {dm['as']})",
        "query_spec.derived_metric"
    ).df_id


def _project_columns(state, df_id, select):
    """Оставить в финальном df ТОЛЬКО select-колонки (в их порядке), которые
    реально есть. Если ничего не совпало - возвращаем df_id как есть (не роняем)."""
    df = state.get_df(df_id)
    keep = [c for c in select if c in df.columns]
    if not keep or keep == list(df.columns):
        return df_id
    return state.register_dataframe(
        df[keep], f"select({df_id})", "query_spec.select"
    ).df_id