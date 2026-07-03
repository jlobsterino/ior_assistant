"""Регрессия period_parser - детерминированный разбор РУ-периодов.
Покрывает кейсы тестировщиков (январь+Q1, диапазоны, кварталы, год, «без периода»)."""
from backend.agent.resolve.period_parser import parse_period


def _se(text):
    p = parse_period(text)
    return None if p is None else (p.start, p.end, p.label, p.kind)


def test_single_month_not_quarter():
    # #6: «январь 2026» НЕ должен превращаться в Q1
    assert _se("за январь 2026 года") == ("2026-01-01", "2026-02-01", "январь 2026", "month")


def test_genitive_month():
    assert _se("за августа 2025 года") == ("2025-08-01", "2025-09-01", "август 2025", "month")


def test_full_year():
    assert _se("за 2026 год инциденты") == ("2026-01-01", "2027-01-01", "2026 год", "year")


def test_quarter_q_notation():
    assert _se("за Q1 2026") == ("2026-01-01", "2026-04-01", "Q1 2026", "quarter")


def test_quarter_roman():
    assert _se("за I кв. 2026") == ("2026-01-01", "2026-04-01", "Q1 2026", "quarter")


def test_quarter_digit_word():
    assert _se("4 квартал 2025") == ("2025-10-01", "2026-01-01", "Q4 2025", "quarter")


def test_month_range():
    # #1: «с января по март 2026» - диапазон, а не один месяц
    assert _se("с января по март 2026") == ("2026-01-01", "2026-04-01", "январь-март 2026", "range")


def test_half_year():
    assert _se("первое полугодие 2026") == ("2026-01-01", "2026-07-01", "первое полугодие 2026", "half")


def test_march():
    assert _se("за март 2026") == ("2026-03-01", "2026-04-01", "март 2026", "month")


def test_no_period_returns_none():
    # Дат не выдумываем
    assert parse_period("выгрузи все инциденты") is None
    assert parse_period("инциденты по Сибирскому банку") is None


def test_no_year_returns_none():
    # Месяц без года - без явного года период не строим
    assert parse_period("за январь") is None


def test_end_is_exclusive_half_open():
    p = parse_period("за январь 2026")
    f = p.as_filter()
    assert f == {"incdnt_entry_dt__gte": "2026-01-01", "incdnt_entry_dt__lt": "2026-02-01"}


def test_explicit_date_range_dmy():
    # БАГ из контура: «01.11.2025-20.11.2025» раньше давал ВЕСЬ 2025 год (схватывал только год)
    assert _se("с 01.11.2025 по 20.11.2025") == (
        "2025-11-01", "2025-11-21", "01.11.2025-20.11.2025", "range"
    )
    assert _se("01.11.2025-20.11.2025")[:2] == ("2025-11-01", "2025-11-21")


def test_explicit_single_date():
    assert _se("за 15.11.2025")[:2] == ("2025-11-15", "2025-11-16")


def test_explicit_iso_range():
    assert _se("период 2025-11-01 .. 2025-11-20")[:2] == ("2025-11-01", "2025-11-21")


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