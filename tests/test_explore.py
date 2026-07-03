"""Регрессия explore (П7) - обзор базы: таблицы/колонки/справочники значений."""
from backend.agent.explore import column_values, schema_overview

MAIN = "d6_base_of_knowledge_ior"


def test_schema_overview_has_main():
    ov = {t["table"]: t for t in schema_overview()}
    assert MAIN in ov
    t = ov[MAIN]
    assert t["rows"] and t["columns"]
    cols = {c["name"]: c for c in t["columns"]}
    assert "org_struct_lvl_3_name" in cols
    assert cols["org_struct_lvl_3_name"]["has_values"] is True


def test_column_values_from_catalog():
    cv = column_values(MAIN, "org_struct_lvl_3_name", limit=5)
    assert cv["total"] > 10 and len(cv["values"]) == 5
    assert all("value" in v for v in cv["values"])


def test_column_values_contains_filter():
    cv = column_values(MAIN, "org_struct_lvl_3_name", contains="банк")
    assert cv["values"] and all("банк" in v["value"].lower() for v in cv["values"])


def test_unknown_column_empty():
    cv = column_values(MAIN, "нет_такой", limit=5)
    assert cv["values"] == [] and cv["total"] == 0


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