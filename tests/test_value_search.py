"""Регрессия value_search (fuzzy-ретривал по реальному каталогу витрины).

Главное, чего НЕ умел старый exact/substring-резолвер: опечатки и слитное
написание. Плюс инвариант: непонятная фраза -> пусто (НЕ молчаливый дамп).
Запуск: python3 tests/test_value_search.py (pytest в venv нет)."""
from backend.agent.resolve.value_search import search_values


def test_exact_value_top_is_dominant_column():
    r = search_values("Волго-Вятский банк")
    assert r, "должны быть кандидаты"
    assert r[0].column == "org_struct_lvl_3_name"      # 12658 строк > lvl_4 1205
    assert r[0].match == "exact" and r[0].score == 1.0


def test_typo_hyphen_dropped_still_resolves():
    # "Волго-Вятский банк" без дефиса - старый substring это ронял
    r = search_values("волговятский банк")
    assert r and r[0].column == "org_struct_lvl_3_name"
    assert r[0].value == "Волго-Вятский банк"
    assert r[0].score >= 0.9


def test_char_typo_fuzzy_match():
    # "Сибрский" - пропущена буква; difflib должен дотянуть до "Сибирский банк"
    r = search_values("Сибрский банк")
    cols = [c.column for c in r[:3]]
    assert "org_struct_lvl_3_name" in cols
    top_l3 = next(c for c in r if c.column == "org_struct_lvl_3_name")
    assert "Сибирский" in top_l3.value


def test_risk_profile_exact():
    r = search_values("Штрафные санкции")
    assert r and r[0].column == "risk_profile_name" and r[0].score == 1.0


def test_event_type_lvl1_with_number():
    r = search_values("1. Ошибки персонала и недостатки процессов")
    assert r and r[0].column == "incdnt_type_lvl_1_name"


def test_nonsense_returns_empty_not_dump():
    assert search_values("экозябра несуществующая чушь") == []


def test_abbreviation_is_not_silently_resolved():
    # СЗБ = 'Северо-Западный банк' по строкам - это работа LLM (развернуть),
    # НЕ хардкода. search сам по себе НЕ должен выдавать его как уверенный матч.
    r = search_values("СЗБ")
    assert not any("Северо-Западный" in c.value and c.score > 0.9 for c in r)


def test_columns_filter_restricts_search():
    r = search_values("Сибирский банк", columns=["org_struct_lvl_3_name"])
    assert r and all(c.column == "org_struct_lvl_3_name" for c in r)


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