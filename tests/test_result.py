"""Регрессия result — сборка result-пакета выгрузки для UI (методология/воронка/
превью/ключевые числа). Чистые билдеры на pandas-фикстуре."""
import pandas as pd

from backend.agent.result import (build_bars, build_conditions, build_methodology,
                                  build_preview, build_result_package, build_summary)

_SPEC = {
    "source": {"table": "d6_base_of_knowledge_ior",
               "joins": [{"table": "d6_base_of_knowledge_incident_recovery"}]},
    "period": {"labels": {"incdnt_entry_dt": "Q1 2026"}},
    "filters": [{"kind": "period", "column": "incdnt_entry_dt", "intent": {"text": "Q1 2026"}},
                {"kind": "categorical", "column": "org_struct_lvl_3_name", "value": "Волго-Вятский банк"},
                {"kind": "range", "column": "direct_loss", "op": "gt", "value": 1000000}],
    "aggregate": {"group_by": ["process_lvl_4_name"],
                  "metrics": [{"as": "direct_loss_sum", "source": "direct_loss", "fn": "sum"}]},
    "derived_metrics": [{"as": "net_loss", "expr": {"op": "sub",
                                                    "left": "direct_loss_sum", "right": "recovery_sum"}}],
}
_DF = pd.DataFrame({
    "process_lvl_4_name": ["П200 Б", "П100 А", "П300 В"],
    "cnt": [1, 1, 2], "direct_loss_sum": [3e6, 1.5e6, 8e5],
    "recovery_sum": [1e6, 2e5, 1e5], "net_loss": [2e6, 1.3e6, 7e5]})


def test_conditions_chips():
    c = build_conditions(_SPEC)
    kinds = [x["kind"] for x in c]
    assert "period" in kinds and "filter" in kinds and "range" in kinds
    assert "group" in kinds and "join" in kinds
    period = next(x for x in c if x["kind"] == "period")
    assert period["detail"] == "Q1 2026" and period["editable"] is True


def test_methodology_mentions_join_money():
    m = build_methodology(_SPEC)
    assert "Q1 2026" in m and "Волго-Вятский банк" in m
    assert "2.26%" in m and "возмещения" in m  # объяснение методологии денег


def test_summary_aggregate_highlights():
    s = build_summary(_DF, _SPEC)
    assert s["is_aggregate"] and s["metric"] == "net_loss"
    labels = {h["label"]: h for h in s["highlights"]}
    assert labels["Максимум"]["value"] == "2 млн" and labels["Максимум"]["sub"] == "П200 Б"
    assert labels["Суммарно"]["value"] == "4 млн"


def test_bars_topn_sorted():
    b = build_bars(_DF, _SPEC)
    assert b and b["items"][0]["label"] == "П200 Б" and b["items"][0]["pct"] == 1.0
    assert [i["label"] for i in b["items"]] == ["П200 Б", "П100 А", "П300 В"]


def test_preview_top_rows():
    p = build_preview(_DF)
    assert "процессам" in p["headers"] and p["total"] == 3
    assert p["rows"][0][0] == "П200 Б"


def test_package_assembles():
    pkg = build_result_package(_SPEC, _DF, funnel=[{"stage": "Инциденты", "rows": 3418},
                                                  {"stage": "Итог", "rows": 24}], warnings=["w"], file_id="f1")
    assert pkg["file_id"] == "f1" and pkg["funnel"][0]["rows"] == 3418
    assert pkg["summary"]["total"] == 3 and pkg["conditions"] and pkg["methodology"]


def test_empty_df_summary_safe():
    s = build_summary(pd.DataFrame(), _SPEC)
    assert s["total"] == 0 and s["highlights"] == []


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