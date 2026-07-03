"""Регрессия status - человеческие формулировки статусов агента (детерминированные)."""
from backend.agent.status import (describe_spec, fmt_int, fmt_money,
                                  human_table, humanize_action, understand_summary)

_SPEC = {
    "source": {"table": "d6_base_of_knowledge_ior", "joins": [
        {"table": "d6_base_of_knowledge_incident_recovery"}]},
    "filters": [{"kind": "period", "intent": {"text": "Q1 2026"}},
                {"kind": "categorical", "value": "Волго-Вятский банк"},
                {"kind": "range", "column": "direct_loss", "op": "gt", "value": 1000000}],
    "aggregate": {"group_by": ["process_lvl_4_name"]},
    "derived_metrics": [{"as": "net_loss"}],
}


def test_fmt_int_thousands():
    assert fmt_int(12658) == "12 658"


def test_fmt_money_compact():
    assert fmt_money(1000000) == "1 млн"
    assert fmt_money(1500000) == "1.5 млн"
    assert fmt_money(2000000000) == "2 млрд"


def test_human_table():
    assert human_table("d6_base_of_knowledge_incident_fin_impact") == "финансовый эффект"
    assert human_table("неизвестная") == "неизвестная"


def test_describe_spec_reads_human():
    d = describe_spec(_SPEC)
    assert "за Q1 2026" in d
    assert "Волго-Вятский банк" in d
    assert "по процессам" in d
    assert "возмещения" in d


def test_humanize_action_spec():
    title, detail = humanize_action("run_query_spec", {"spec": _SPEC})
    assert title == "Собираю выгрузку" and "по процессам" in detail


def test_humanize_action_search():
    title, detail = humanize_action("search_values", {"query": "ВВ банк"})
    assert title == "Ищу значение в данных" and detail == "ВВ банк"


def test_understand_summary():
    s = understand_summary(
        [{"column": "org_struct_lvl_3_name", "value": "Волго-Вятский банк"}],
        "Q1 2026", "spec_required")
    assert "Q1 2026" in s and "Волго-Вятский банк" in s


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f" ok  {fn.__name__}"); passed += 1
        except Exception:
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    raise SystemExit(0 if passed == len(fns) else 1)