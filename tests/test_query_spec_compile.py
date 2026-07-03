"""B.2 – интеграционный прогон compile_query_spec ОФЛАЙН на pandas-фикстурах.

Боевой compile зовёт REGISTRY (тянет dataframe_ops->backend.config->pydantic_settings,
которого офлайн нет). Поэтому ВНЕДРЯЕМ фейковый реестр с верными pandas-тулами
(query=фильтр фикстуры, join=merge, group_by=groupby.agg, top_n=sort.head, export=заглушка)
– и гоняем ВЕСЬ конвейер §2.4 end-to-end: period->source->pre_aggregate->join->post_join->маска->
aggregate->derived_metrics->sort->select->гард пустого финального df (§3.8).

Главное, что проверяем (must_fix #1 из ревью дизайна): гард пустого результата сработает
на АГРЕГИРОВАННОЙ выгрузке (маска обнулила df ПОСЛЕ join+agg), а не только на сыром query.

Запуск: PYTHONPATH=. python3 tests/test_query_spec_compile.py
"""

import asyncio
import types
from datetime import date

import pandas as pd

from backend.agent.query_spec import CompileContext, compile_query_spec
from backend.agent.schema.loader import get_schema

MAIN = "d6_base_of_knowledge_ior"
FIN = "d6_base_of_knowledge_incident_fin_impact"
REC = "d6_base_of_knowledge_incident_recovery"


# --- фейковый SessionState -----------------------
class FakeState:
    def __init__(self):
        self.dataframes = {}
        self.dataframe_meta = {}
        self._n = 0

    def register_dataframe(self, df, description="", created_by="", df_id=None):
        if df_id is None:
            self._n += 1
            df_id = f"df_{self._n}"
        self.dataframes[df_id] = df.reset_index(drop=True)
        self.dataframe_meta[df_id] = types.SimpleNamespace(
            df_id=df_id, rows=len(df), columns=list(df.columns)
        )
        return types.SimpleNamespace(df_id=df_id)

    def get_df(self, df_id):
        return self.dataframes[df_id]


class _TR:
    def __init__(self, ok=True, output=None, error=None, summary=""):
        self.ok, self.output, self.error, self.summary = ok, output, error, summary


def _apply_where(df, where):
    """pandas-аналог query-тула: поддерживает {col:val}|{col:{in}}|[col:{op:v}]|{col__op:v}."""
    if not where:
        return df
    out = df
    for k, v in where.items():
        if "__" in k:
            col, op = k.rsplit("__", 1)
            out = _cmp(out, col, op, v)
        elif isinstance(v, dict):
            for op, val in v.items():
                out = _cmp(out, k, str(op).lower(), val)
        elif isinstance(v, list):
            out = out[out[k].isin(v)]
        else:
            out = out[out[k] == v]
    return out


def _cmp(df, col, op, v):
    s = df[col]
    if isinstance(v, date) and not isinstance(v, pd.Timestamp):
        v = pd.Timestamp(v)
    op = op.lower()
    if op in ("gte", ">="):
        return df[s >= v]
    if op in ("gt", ">"):
        return df[s > v]
    if op in ("lte", "<="):
        return df[s <= v]
    if op in ("lt", "<"):
        return df[s < v]
    if op in ("ne", "!="):
        return df[s != v]
    if op == "like":
        pat = str(v).strip("%")
        return df[s.astype(str).str.contains(pat, case=False, na=False)]
    return df[s == v]  # eq / =


class FakeRegistry:
    """Минимальный реестр с верными pandas-тулами (контракт как у боевых)."""
    def __init__(self, fixtures):
        self.fixtures = fixtures

    async def execute(self, name, args, ctx):
        try:
            return getattr(self, f"_{name}")(ctx, **args)
        except Exception as e:  # noqa: BLE001
            return _TR(ok=False, error=f"{type(e).__name__}: {e}")

    def _query(self, ctx, table, where=None, columns=None, limit=None, **kw):
        df = self.fixtures[table].copy()
        df = _apply_where(df, where)
        if columns:
            df = df[[c for c in columns if c in df.columns]]
        m = ctx.register_dataframe(df, f"query({table})", "query")
        return _TR(output={"df_id": m.df_id, "rows": len(df), "columns": list(df.columns)})

    def _join_dfs(self, ctx, left_df, right_df, on, how="left", **kw):
        a, b = ctx.get_df(left_df), ctx.get_df(right_df)
        merged = a.merge(b, on=on, how=how)
        m = ctx.register_dataframe(merged, "join", "join_dfs")
        return _TR(output={"df_id": m.df_id, "rows": len(merged)})

    def _group_by(self, ctx, df_id, by, agg, **kw):
        df = ctx.get_df(df_id)
        g = df.groupby(by, dropna=False).agg(agg).reset_index()
        m = ctx.register_dataframe(g, "group_by", "group_by")
        return _TR(output={"df_id": m.df_id, "rows": len(g), "columns": list(g.columns)})

    def _derive_column(self, ctx, df_id, source, new_column, op="month", **kw):
        df = ctx.get_df(df_id).copy()
        s = pd.to_datetime(df[source], errors="coerce")
        df[new_column] = (s.dt.to_period("M").astype(str) if op == "month"
                          else s.dt.year.astype(str))
        m = ctx.register_dataframe(df, "derive", "derive_column")
        return _TR(output={"df_id": m.df_id})

    def _window_rank(self, ctx, df_id, partition_by=None, order_by=None,
                     order_desc=True, top_n=None, method="row_number", **kw):
        df = ctx.get_df(df_id).copy()
        parts = ([partition_by] if isinstance(partition_by, str) else (partition_by or []))
        asc = not order_desc
        if parts:
            df["rank"] = df.groupby(parts, dropna=False)[order_by].rank(
                method="first", ascending=asc).astype(int)
        else:
            df["rank"] = df[order_by].rank(method="first", ascending=asc).astype(int)
        if top_n is not None:
            df = df[df["rank"] <= int(top_n)]
        m = ctx.register_dataframe(df, "window", "window_rank")
        return _TR(output={"df_id": m.df_id})

    def _top_n(self, ctx, df_id, by, n=10, ascending=False, **kw):
        df = ctx.get_df(df_id)
        s = df.sort_values(by=by, ascending=ascending, na_position="last").head(n)
        m = ctx.register_dataframe(s, "top_n", "top_n")
        return _TR(output={"df_id": m.df_id})

    def _export_excel(self, ctx, df_id, name=None, **kw):
        return _TR(output={"file_id": "file_1", "name": name or "out.xlsx",
                           "rows": len(ctx.get_df(df_id))})

    _export_csv = _export_excel


# --- фикстуры -----------------------
def _fixtures():
    main = pd.DataFrame([
        {"incdnt_id": 1, "incdnt_entry_dt": pd.Timestamp("2026-02-10"),
         "org_struct_lvl_3_name": "Волго-Вятский банк", "process_lvl_4_name": "П100 Процесс А"},
        {"incdnt_id": 2, "incdnt_entry_dt": pd.Timestamp("2026-03-01"),
         "org_struct_lvl_3_name": "Волго-Вятский банк", "process_lvl_4_name": "П100 Процесс А"},
        {"incdnt_id": 3, "incdnt_entry_dt": pd.Timestamp("2026-02-15"),
         "org_struct_lvl_3_name": "Волго-Вятский банк", "process_lvl_4_name": "П200 Процесс Б"},
        {"incdnt_id": 4, "incdnt_entry_dt": pd.Timestamp("2026-02-10"),
         "org_struct_lvl_3_name": "Сибирский банк", "process_lvl_4_name": "П100 Процесс А"},
        {"incdnt_id": 5, "incdnt_entry_dt": pd.Timestamp("2025-12-01"),
         "org_struct_lvl_3_name": "Волго-Вятский банк", "process_lvl_4_name": "П100 Процесс А"},
    ])
    fin = pd.DataFrame([
        {"incdnt_id": 1, "fin_impact_rub_amt": 1_500_000.0, "fin_impact_type_name": "Прямая потеря"},
        {"incdnt_id": 1, "fin_impact_rub_amt": 900_000.0, "fin_impact_type_name": "Косвенная потеря"},
        {"incdnt_id": 2, "fin_impact_rub_amt": 500_000.0, "fin_impact_type_name": "Прямая потеря"},
        {"incdnt_id": 3, "fin_impact_rub_amt": 3_000_000.0, "fin_impact_type_name": "Прямая потеря"},
    ])
    rec = pd.DataFrame([
        {"incdnt_id": 1, "recovery_rub_amt": 200_000.0},
        {"incdnt_id": 3, "recovery_rub_amt": 1_000_000.0},
    ])
    return {MAIN: main, FIN: fin, REC: rec}


def _spec(range_threshold=1_000_000):
    return {
        "version": 1,
        "source": {"table": MAIN, "joins": [
            {"table": FIN, "on": "incdnt_id", "how": "left",
             "pre_aggregate": {"group_by": ["incdnt_id"], "agg": {"fin_impact_rub_amt": {
                 "fn": "sum", "filter": {"fin_impact_type_name": {"eq": "Прямая потеря"}},
                 "as": "direct_loss"}}}, "select": ["direct_loss"]},
            {"table": REC, "on": "incdnt_id", "how": "left",
             "pre_aggregate": {"group_by": ["incdnt_id"],
                               "agg": {"recovery_rub_amt": {"fn": "sum", "as": "recovery"}}},
             "select": ["recovery"]},
        ]},
        "filters": [
            {"kind": "period", "intent": {"text": "Q1 2026"},
             "column": "incdnt_entry_dt", "required": True},
            {"kind": "categorical", "column": "org_struct_lvl_3_name", "op": "eq",
             "value": "Волго-Вятский банк", "grounded": True},
            {"kind": "range", "column": "direct_loss", "op": "gt", "value": range_threshold},
        ],
        "aggregate": {"group_by": ["process_lvl_4_name"], "metrics": [
            {"as": "direct_loss_sum", "source": "direct_loss", "fn": "sum"},
            {"as": "recovery_sum", "source": "recovery", "fn": "sum"},
            {"as": "cnt", "source": "incdnt_id", "fn": "count"}]},
        "derived_metrics": [{"as": "net_loss",
                             "expr": {"op": "sub", "left": "direct_loss_sum", "right": "recovery_sum"}}],
        "sort": [{"by": "net_loss", "desc": True}],
        "limit": 100000,
        "select": ["process_lvl_4_name", "cnt", "direct_loss_sum", "recovery_sum", "net_loss"],
        "output": {"format": "excel", "name": "ИОР Q1 2026 ВВБ по процессам"},
    }


def _run(spec, fixtures=None):
    state = FakeState()
    cctx = CompileContext(ctx=state, emit=None, schema=get_schema(), now=date(2026, 6, 24))
    reg = FakeRegistry(fixtures or _fixtures())
    res = asyncio.run(compile_query_spec(cctx, spec, registry=reg))
    return res, state


# --- тесты -----------------------
def test_complex_vygruzka_compiles_end_to_end():
    res, state = _run(_spec())
    assert res.ok, res.error
    df = state.get_df(res.df_id)
    # select-проекция: именно эти колонки и в этом порядке
    assert list(df.columns) == ["process_lvl_4_name", "cnt", "direct_loss_sum",
                                "recovery_sum", "net_loss"], list(df.columns)
    # период (Q1 2026) убрал inc5; категориальный (ВВБ) убрал inc4; маска >1млн убрала inc2
    # -> выжили inc1 (П100, прямая 1.5М, возмещ 0.2М) и inc3 (П200, прямая 3М, возмещ 1М)
    assert len(df) == 2, df.to_dict("records")
    # сортировка по net_loss desc: П200 (2.0М) перед П100 (1.3М)
    assert list(df["process_lvl_4_name"]) == ["П200 Процесс Б", "П100 Процесс А"]
    assert list(df["net_loss"]) == [2_000_000.0, 1_300_000.0]
    assert list(df["direct_loss_sum"]) == [3_000_000.0, 1_500_000.0]
    assert list(df["recovery_sum"]) == [1_000_000.0, 200_000.0]
    assert list(df["cnt"]) == [1, 1]


def test_pre_aggregate_filter_excludes_kosvennaya():
    # inc1 имеет Прямая 1.5М + Косвенная 0.9М; pre_aggregate(filter Прямая) = 1.5М (НЕ 2.4М)
    res, state = _run(_spec())
    df = state.get_df(res.df_id)
    row = df[df["process_lvl_4_name"] == "П100 Процесс А"].iloc[0]
    assert row["direct_loss_sum"] == 1_500_000.0   # Косвенная не попала


def test_empty_after_aggregate_blocks_export():
    # ПОРОГ 10млн -> ни один инцидент не проходит post_join маску -> агрегат пуст ->
    # гард §3.8 (must_fix #1) ОБЯЗАН вернуть ok=False, файл НЕ создаётся.
    res, _ = _run(_spec(range_threshold=10_000_000))
    assert res.ok is False
    assert "EMPTY_RESULT" in (res.error or ""), res.error


def test_required_period_unparseable_blocks():
    spec = _spec()
    spec["filters"][0]["intent"]["text"] = "когда-нибудь потом"  # не распознаётся
    res, _ = _run(spec)
    assert res.ok is False
    assert "не распознан" in (res.error or "").lower(), res.error


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f" ok  {fn.__name__}"); passed += 1
        except Exception:
            print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    raise SystemExit(0 if passed == len(fns) else 1)