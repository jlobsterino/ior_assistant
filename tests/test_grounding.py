"""Регрессия grounding.diagnose_empty - корневой баг тестировщиков.

Проверяет: фильтр на НЕВЕРНУЮ колонку (ТБ в lvl_2, хотя они в lvl_3) -> 0 строк ->
диагностика по реальному каталогу указывает правильную колонку и значение.
Запуск: PYTHONPATH=. python3 tests/test_grounding.py"""
from backend.agent.resolve.grounding import diagnose_empty

MAIN = "d6_base_of_knowledge_ior"


def test_wrong_level_filter_gets_correction():
    # ТБ ошибочно отфильтрован по lvl_2 - реальный баг тестировщиков
    d = diagnose_empty(MAIN, {"org_struct_lvl_2_name": "Волго-Вятский банк"})
    assert d.likely_wrong_filter is True
    assert d.corrections, "должна быть коррекция"
    c = d.corrections[0]
    assert c["found_in_column"] == "org_struct_lvl_3_name"
    assert "Волго-Вятский" in c["found_value"]


def test_like_alias_path_also_diagnosed():
    # планировщик решил пишет org_struct_lvl_2_name__like '%...' (см. prompts Пример 2)
    d = diagnose_empty(MAIN, {"org_struct_lvl_2_name__like": "%Волго-Вятский%"})
    assert d.likely_wrong_filter is True
    assert d.corrections[0]["found_in_column"] == "org_struct_lvl_3_name"


def test_valid_filter_is_not_flagged_as_wrong():
    # та же ТБ, но в ПРАВИЛЬНОЙ колонке - фильтр валиден, 0 = реально нет (не баг колонки)
    d = diagnose_empty(MAIN, {"org_struct_lvl_3_name": "Волго-Вятский банк"})
    assert d.likely_wrong_filter is False
    assert not d.corrections


def test_date_only_filter_is_not_column_error():
    d = diagnose_empty(MAIN, {"incdnt_entry_dt": {">=": "2025-01-01", "<": "2025-02-01"}})
    assert d.likely_wrong_filter is False
    assert "ask_user" in d.message


def test_message_is_actionable_for_reflector():
    d = diagnose_empty(MAIN, {"org_struct_lvl_2_name": "Сибирский банк"})
    assert d.likely_wrong_filter is True
    assert "EMPTY_RESULT" in d.message
    assert "retry" in d.message and "found_value" in d.message


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