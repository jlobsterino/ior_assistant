"""Регрессия ground_query (А.4) - заземление фраз запроса по реальному
каталогу витрины, чтобы планировщик выбирал ВЕРНУЮ колонку СРАЗУ.

Корневой баг: ТБ в org_struct_lvl_3_name, а модель ставит lvl_2. ground_query
должен поднять верную колонку из реальных данных - устойчиво к падежам.
Офлайн, без LLM и без backend.data (grounding тянет только value_search).
Запуск: PYTHONPATH=. python3 tests/test_planner_grounding.py"""
from backend.agent.resolve.grounding import ground_query


def test_tb_resolves_to_lvl3_not_lvl2():
    # «Волго-Вятскому» (дат. падеж) - ground_query должен поднять именительный
    hits = ground_query("Выгрузи ИОР по Волго-Вятскому банку за 2025")
    cols = [h["column"] for h in hits]
    assert "org_struct_lvl_3_name" in cols, cols
    assert "org_struct_lvl_2_name" not in cols, cols
    h = next(h for h in hits if h["column"] == "org_struct_lvl_3_name")
    assert "Волго-Вятский" in h["value"]


def test_process_code_resolves_to_process_lvl4():
    hits = ground_query("отчёт по процессу П1227")
    cols = [h["column"] for h in hits]
    assert "process_lvl_4_name" in cols, cols
    h = next(h for h in hits if h["column"] == "process_lvl_4_name")
    assert h["value"].startswith("П1227")


def test_risk_profile_phrase_resolves():
    hits = ground_query("Штрафные санкции")
    assert hits, "должен быть хит"
    assert hits[0]["column"] == "risk_profile_name"
    assert hits[0]["value"] == "Штрафные санкции"


def test_nonsense_returns_no_confident_hits():
    assert ground_query("бессмысленная чушь зюзюбра") == []


def test_empty_query_is_empty():
    assert ground_query("") == []
    assert ground_query("   ") == []


def test_hits_have_contract_fields():
    hits = ground_query("Штрафные санкции")
    for h in hits:
        assert set(h) >= {"phrase", "column", "value", "count", "score"}


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f" ok  {fn.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    raise SystemExit(0 if passed == len(fns) else 1)