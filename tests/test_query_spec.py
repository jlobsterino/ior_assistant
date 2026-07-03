"""Офлайн-регрессия QuerySpec (8.1) — validate_spec + пуре-хелперы.

Слой 1: только schema + catalog, без venv/store (как test_grounding.py).
compile_query_spec НЕ тестируется здесь — нужен DuckDB-фикстур (8.2), но его
логика разложена в пуре-хелперы, которые покрыты ниже.
Запуск: PYTHONPATH=. python3 tests/test_query_spec.py"""
from backend.agent.query_spec import (
    GROUND_STRONG,
    MONEY_FILLED_PCT,
    build_main_where,
    classify_range_stage,
    detect_fanout,
    eval_derived_metric,
    expand_period_filters,
    is_empty_df,
    is_money_main_col,
    validate_spec,
)
from backend.agent.schema import get_schema

SCHEMA = get_schema()
MAIN = "d6_base_of_knowledge_ior"
FIN = "d6_base_of_knowledge_incident_fin_impact"
REC = "d6_base_of_knowledge_incident_recovery"


def _full_spec():
    """Полный сложный спек из §2.2 (валидный)."""
    return {
        "version": 1,
        "source": {
            "table": MAIN,
            "joins": [
                # per-incident прямые потери: alias 'direct_loss' (НЕ 'direct_loss_sum'!),
                # чтобы агрегат-сумма по группе ('direct_loss_sum') имела ДРУГОЕ имя —
                # иначе range-стадия неоднозначна (см. classify_range_stage).
                {"table": FIN, "on": "incdnt_id", "how": "left",
                 "pre_aggregate": {
                     "group_by": ["incdnt_id"],
                     "agg": {"fin_impact_rub_amt": {
                         "fn": "sum",
                         "filter": {"fin_impact_type_name": {"eq": "Прямая потеря"}},
                         "as": "direct_loss"}}},
                 "select": ["direct_loss"]},
                {"table": REC, "on": "incdnt_id", "how": "left",
                 "pre_aggregate": {
                     "group_by": ["incdnt_id"],
                     "agg": {"recovery_rub_amt": {"fn": "sum", "as": "recovery"}}},
                 "select": ["recovery"]},
            ],
        },
        "filters": [
            {"kind": "period", "intent": {"text": "Q1 2026"},
             "column": "incdnt_entry_dt", "required": True},
            {"kind": "categorical", "column": "org_struct_lvl_3_name",
             "op": "eq", "value": "Волго-Вятский банк",
             "grounded": True, "grounded_via": "search_values"},
            # фильтр по per-incident прямой потере >1млн (post_join, ДО агрегата по процессу)
            {"kind": "range", "column": "direct_loss", "op": "gt", "value": 1000000},
        ],
        "aggregate": {
            "group_by": ["process_lvl_4_name"],
            "metrics": [
                {"as": "direct_loss_sum", "source": "direct_loss", "fn": "sum"},
                {"as": "recovery_sum", "source": "recovery", "fn": "sum"},
                {"as": "cnt", "source": "incdnt_id", "fn": "count"}],
        },
        "derived_metrics": [
            {"as": "net_loss",
             "expr": {"op": "sub", "left": "direct_loss_sum", "right": "recovery_sum"}},
        ],
        "window": [{"partition_by": [], "order_by": "net_loss",
                    "order_desc": True, "top_n": 20, "method": "row_number"}],
        "sort": [{"by": "net_loss", "desc": True}],
        "limit": 100000,
        "select": ["process_lvl_4_name", "cnt", "direct_loss_sum",
                   "recovery_sum", "net_loss"],
        "output": {"format": "excel", "name": "ИОР Q1 2026 ВВБ по процессам"},
    }


# — 1. полный сложный спек валиден —


def test_full_complex_spec_is_valid():
    err = validate_spec(_full_spec(), SCHEMA)
    assert err is None, err


# — 2. OR-конструкты блокируются —


def test_validate_blocks_any_of():
    s = _full_spec()
    s["filters"].append({"any_of": [
        {"kind": "categorical", "column": "org_struct_lvl_3_name", "value": "A"},
        {"kind": "categorical", "column": "org_struct_lvl_3_name", "value": "Б"}]})
    err = validate_spec(s, SCHEMA)
    assert err and "OR" in err, err


def test_validate_blocks_nested_or():
    s = _full_spec()
    s["filters"].append({"_or": [{"x": 1}]})
    err = validate_spec(s, SCHEMA)
    assert err and "OR" in err, err


def test_validate_blocks_op_or():
    s = _full_spec()
    s["filters"].append({"kind": "categorical", "op": "or",
                         "column": "org_struct_lvl_3_name", "value": "x"})
    err = validate_spec(s, SCHEMA)
    assert err and "OR" in err, err


# — 3. money main-колонка vs derived alias —


def test_money_main_col_in_metric_blocked():
    s = _full_spec()
    # incdnt_sum — исходная main-сумма filled 2.26% -> blocker как metric
    s["aggregate"]["metrics"].append(
        {"as": "s", "source": "incdnt_sum", "fn": "sum"}
    )
    err = validate_spec(s, SCHEMA)
    assert err and ("incdnt_sum" in err and "fin_impact" in err), err


def test_money_main_col_in_range_blocked():
    s = _full_spec()
    s["filters"].append({"kind": "range", "column": "incdnt_drct_dmg_sum",
                         "op": "gt", "value": 1000})
    err = validate_spec(s, SCHEMA)
    assert err and "incdnt_drct_dmg_sum" in err, err


def test_derived_alias_net_loss_not_falsely_blocked():
    # net_loss содержит 'Loss', но он derived -> НЕ ловится money-гардом
    s = _full_spec()
    assert validate_spec(s, SCHEMA) is None
    assert is_money_main_col("net_loss", SCHEMA) is False
    assert is_money_main_col("direct_loss_sum", SCHEMA) is False
    assert is_money_main_col("recovery_sum", SCHEMA) is False
    # а исходные main-суммы — да
    assert is_money_main_col("incdnt_sum", SCHEMA) is True
    assert is_money_main_col("incdnt_drct_dmg_sum", SCHEMA) is True
    # категориальная (filled 97%) — НЕ деньги
    assert is_money_main_col("org_struct_lvl_3_name", SCHEMA) is False


# — 4. categorical: без grounded + неверная колонка —


def test_categorical_without_grounded_errors():
    s = _full_spec()
    s["filters"][1]["grounded"] = False
    err = validate_spec(s, SCHEMA)
    assert err and "grounded" in err, err


def test_categorical_grounded_wrong_column_caught():
    # 'Волго-Вятский банк' grounded:true, но на org_struct_lvl_2_name - там его НЕТ.
    s = _full_spec()
    s["filters"][1]["column"] = "org_struct_lvl_2_name"
    err = validate_spec(s, SCHEMA)
    assert err and "org_struct_lvl_2_name" in err, err


def test_categorical_grounded_correct_passes():
    s = _full_spec()
    # только убедимся, что верная колонка проходит граунд-сверку
    s2 = {**s, "filters": [s["filters"][1]], "source": {"table": MAIN},
          "aggregate": {}, "derived_metrics": [], "window": [], "sort": [],
          "select": []}
    assert validate_spec(s2, SCHEMA) is None


# — 5. join без pre_aggregate —


def test_join_without_pre_aggregate_errors():
    s = _full_spec()
    del s["source"]["joins"][0]["pre_aggregate"]
    err = validate_spec(s, SCHEMA)
    assert err and "pre_aggregate" in err, err


def test_join_wrong_table_errors():
    s = _full_spec()
    s["source"]["joins"][0]["table"] = "несуществующая_таблица"
    err = validate_spec(s, SCHEMA)
    assert err and "FK" in err, err


def test_join_wrong_on_key_errors():
    s = _full_spec()
    s["source"]["joins"][0]["on"] = "fin_impact_id"
    err = validate_spec(s, SCHEMA)
    assert err and "incdnt_id" in err, err


def test_source_table_not_in_schema():
    s = _full_spec()
    s["source"]["table"] = "foo"
    err = validate_spec(s, SCHEMA)
    assert err and "source.table" in err, err


def test_unknown_agg_fn_errors():
    s = _full_spec()
    s["aggregate"]["metrics"][0]["fn"] = "median"
    err = validate_spec(s, SCHEMA)
    assert err and "median" in err, err


def test_unknown_derived_op_errors():
    s = _full_spec()
    s["derived_metrics"][0]["expr"]["op"] = "pow"
    err = validate_spec(s, SCHEMA)
    assert err and "pow" in err, err


# — 6. classify_range_stage —


def test_classify_range_post_join_unambiguous():
    # range на per-incident 'direct_loss' (только в join.select) -> post_join однозначно
    stages = classify_range_stage(_full_spec(), SCHEMA)
    assert stages["direct_loss"] == "post_join"


def test_classify_range_ambiguous_name_rejected():
    # если range-колонка есть и в join.select, и в aggregate.metrics.as -> стадия
    # неоднозначна -> validate_spec ОБЯЗАН отклонить (молча выбрать стадию = молча неверно)
    s = _full_spec()
    # сломаем именование обратно к неоднозначному: алиас агрегата == per-incident имя
    s["source"]["joins"][0]["pre_aggregate"]["agg"]["fin_impact_rub_amt"]["as"] = "direct_loss_sum"
    s["source"]["joins"][0]["select"] = ["direct_loss_sum"]
    s["aggregate"]["metrics"][0] = {"as": "direct_loss_sum",
                                    "source": "direct_loss_sum", "fn": "sum"}
    s["filters"][2]["column"] = "direct_loss_sum"
    err = validate_spec(s, SCHEMA)
    assert err and "неоднознач" in err.lower(), err


def test_classify_range_post_join_only():
    s = _full_spec()
    s["aggregate"] = {}
    s["derived_metrics"] = []
    s["window"] = []
    s["sort"] = []
    s["select"] = []
    stages = classify_range_stage(s, SCHEMA)
    assert stages["direct_loss"] == "post_join"


def test_classify_range_pre_source_main_numeric():
    s = _full_spec()
    s["aggregate"] = {}
    s["derived_metrics"] = []
    s["window"] = []
    s["sort"] = []
    s["select"] = []
    # range по main-числовой колонке (mistake_cnt, filled 93% -> не money) -> pre_source
    s["filters"] = [{"kind": "range", "column": "incdnt_mistake_cnt",
                     "op": "gt", "value": 0}]
    s["source"] = {"table": MAIN}
    stages = classify_range_stage(s, SCHEMA)
    assert stages["incdnt_mistake_cnt"] == "pre_source"


def test_classify_range_unknown_column_via_validate():
    s = _full_spec()
    s["aggregate"] = {}
    s["derived_metrics"] = []
    s["window"] = []
    s["sort"] = []
    s["select"] = []
    s["filters"] = [{"kind": "range", "column": "не_существует",
                     "op": "gt", "value": 1}]
    s["source"] = {"table": MAIN}
    err = validate_spec(s, SCHEMA)
    assert err and "не_существует" in err, err


# — 7. detect_fanout / is_empty_df / eval_derived_metric —


def test_detect_fanout_true_with_dupes():
    import pandas as pd
    df = pd.DataFrame({"incdnt_id": [1, 1, 2], "x": [10, 20, 30]})
    assert detect_fanout(df, "incdnt_id") is True


def test_detect_fanout_false_unique():
    import pandas as pd
    df = pd.DataFrame({"incdnt_id": [1, 2, 3], "x": [10, 20, 30]})
    assert detect_fanout(df, "incdnt_id") is False


def test_is_empty_df():
    import pandas as pd
    assert is_empty_df(pd.DataFrame()) is True
    assert is_empty_df(None) is True
    assert is_empty_df(pd.DataFrame({"a": [1]})) is False


def test_eval_derived_metric_sub():
    import pandas as pd
    df = pd.DataFrame({"direct_loss_sum": [100.0, 50.0],
                       "recovery_sum": [30.0, 10.0]})
    s = eval_derived_metric(
        df, {"as": "net", "expr": {"op": "sub", "left": "direct_loss_sum",
                                   "right": "recovery_sum"}}
    )
    assert list(s) == [70.0, 40.0]


def test_eval_derived_metric_safe_div_zero_is_nan():
    import math
    import pandas as pd
    df = pd.DataFrame({"a": [10.0, 5.0], "b": [2.0, 0.0]})
    s = eval_derived_metric(
        df, {"as": "r", "expr": {"op": "safe_div", "left": "a", "right": "b"}}
    )
    assert s.iloc[0] == 5.0
    assert math.isnan(s.iloc[1])  # деление на 0 -> NaN, не inf/исключение


# — 8. expand_period_filters / build_main_where —


def test_expand_period_required_missing_errors():
    s = {"filters": [{"kind": "period", "intent": {"text": "за квартал"},
                      "column": "incdnt_entry_dt", "required": True}]}
    from datetime import date
    where, _ = expand_period_filters(s, date(2026, 6, 24))
    assert isinstance(where, str) and "обязател" in where


def test_expand_period_resolves_quarter():
    from datetime import date
    where, labels = expand_period_filters(_full_spec(), date(2026, 6, 24))
    assert where["incdnt_entry_dt__gte"] == "2026-01-01"
    assert where["incdnt_entry_dt__lt"] == "2026-04-01"
    assert labels["incdnt_entry_dt"] == "Q1 2026"


def test_build_main_where_has_categorical_and_period_not_postjoin_range():
    from datetime import date
    w = build_main_where(_full_spec(), SCHEMA, date(2026, 6, 24))
    assert w["org_struct_lvl_3_name"] == "Волго-Вятский банк"
    assert w["incdnt_entry_dt__gte"] == "2026-01-01"
    # range по direct_loss_sum - post_*, НЕ в where_main
    assert not any("direct_loss_sum" in k for k in w)


# — 9. константы —


def test_single_ground_threshold():
    from backend.agent.resolve.grounding import _GROUND_STRONG
    assert _GROUND_STRONG == GROUND_STRONG == 0.85
    assert MONEY_FILLED_PCT == 5.0


def test_bare_string_filter_clean_error_not_crash():
    # GigaChat иногда кладёт фильтр строкой -> раньше падало AttributeError 'str'.get
    err = validate_spec({"source": {"table": MAIN}, "filters": ["прямые потери"]}, SCHEMA)
    assert err and "фильтр" in err.lower(), err


def test_period_intent_as_string_coerced():
    # intent строкой "Q1 2026" вместо {"text":...} -> коэрция, без краша
    from backend.agent.query_spec import expand_period_filters
    from datetime import date as _d
    where, labels = expand_period_filters(
        {"filters": [{"kind": "period", "intent": "Q1 2026",
                      "column": "incdnt_entry_dt", "required": True}]}, _d(2026, 6, 24))
    assert where.get("incdnt_entry_dt__gte") == "2026-01-01"
    assert where.get("incdnt_entry_dt__lt") == "2026-04-01"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f"  ok  {fn.__name__}"); passed += 1
        except Exception:
            print(f"FAIL  {fn.__name__}"); traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    raise SystemExit(0 if passed == len(fns) else 1)