import pandas as pd
import asyncio
from backend.agent.query_spec import reorder_columns
from backend.agent.hypothesis import get_total_and_direct_loss, profile_dataframe, generate_hypothesis_narrative, analyze_incident_descriptions

def test_reorder_columns():
    cols = ["org_struct_lvl_3_name", "incdnt_id", "incdnt_sid", "net_loss"]
    sorted_cols = reorder_columns(cols)
    assert sorted_cols[0] == "incdnt_sid"
    assert sorted_cols[-1] == "incdnt_id"
    
    # Russian variants
    cols_ru = ["Орг. структура", "Идентификационный ключ инцидента операционного риска", "Идентификатор события", "Сумма"]
    sorted_cols_ru = reorder_columns(cols_ru)
    assert sorted_cols_ru[0] == "Идентификатор события"
    assert sorted_cols_ru[-1] == "Идентификационный ключ инцидента операционного риска"

def test_get_total_and_direct_loss():
    df = pd.DataFrame({
        "incdnt_sum": [1000.0, 2000.0],
        "direct_loss": [100.0, 200.0]
    })
    total, direct = get_total_and_direct_loss(df)
    assert total == 3000.0
    assert direct == 300.0
    
    # Russian variants
    df_ru = pd.DataFrame({
        "Общая сумма всех последствий (руб.)": [500.0, 1500.0],
        "Прямая потеря – итого (руб.)": [50.0, 150.0]
    })
    total_ru, direct_ru = get_total_and_direct_loss(df_ru)
    assert total_ru == 2000.0
    assert direct_ru == 200.0

def test_profile_dataframe_top_10_and_risk_profile():
    # Setup dataframe with 12 incidents
    df = pd.DataFrame({
        "incdnt_id": list(range(1, 13)),
        "Идентификатор события": [f"EVE-{i}" for i in range(1, 13)],
        "incdnt_sum": [100.0] * 12,
        "incdnt_status_name": ["Утверждён"] * 12,
        "incdnt_type_lvl_1_name": ["Ошибки персонала"] * 12,
        "risk_profile_id": ["RP-01"] * 12,
        "risk_profile_name": ["Операционный риск"] * 12
    })
    profile = profile_dataframe(df)
    
    # Check combined top-10 sum and pct
    assert "Концентрация потерь (Топ-10 инцидентов)" in profile
    assert "Суммарные потери Топ-10 инцидентов" in profile
    # Total loss is 1200, top 10 sum is 1000, percentage is 83.3%
    assert "1 000.00" in profile
    assert "83.3%" in profile
    
    # Check risk profile combination
    assert "Основной вид рискового события" in profile
    assert "RP-01 - Операционный риск" in profile
    
    # Check renamed reason column
    assert "Основная причина" in profile

def test_deleted_statistics():
    df = pd.DataFrame({
        "incdnt_id": [1, 2, 3],
        "incdnt_sum": [100.0, 200.0, 300.0],
        "recovery": [10.0, 20.0, 30.0],
        "incdnt_status_name": ["Утверждён", "Утверждён", "Удалён"]
    })
    loop = asyncio.get_event_loop()
    res = loop.run_until_complete(generate_hypothesis_narrative(
        "анализ", df, {"name": "test.xlsx", "size": "1KB"}, "session-123"
    ))
    
    assert "Информация об удаленных инцидентах" in res
    assert "Количество удаленных инцидентов**: 1" in res
    assert "Сумма потерь по удаленным инцидентам**: 300.00 ₽" in res
    assert "Сумма возмещений по удаленным инцидентам**: 30.00 ₽" in res
    assert "Если вы хотите больше узнать о причинах удаления инцидентов, создайте новую сессию и запросите выгрузку по удаленным инцидентам" in res

def test_functional_blocks_and_detection_channels():
    df = pd.DataFrame({
        "incdnt_id": [1, 2, 3],
        "incdnt_sum": [100.0, 200.0, 300.0],
        "funct_block_lvl_2_name": ["ЦА", "Территориальные банки", "SBR_11"],
        "process_lvl_4_name": ["Кредитование", "Депозиты", "Депозиты"],
        "incdnt_detection_person_name": ["Внешний контролирующий орган", "Клиент", "Служба безопасности"]
    })
    profile = profile_dataframe(df)
    
    # Exclude technical SBR_11 block code
    assert "SBR_11" not in profile
    # Keep human readable ЦА and Территориальные банки
    assert "ЦА" in profile
    assert "Территориальные банки" in profile
    
    # Level 4 Process present
    assert "Показатели по процессам (уровень 4)" in profile
    assert "Кредитование" in profile
    
    # Detection channels present
    assert "Анализ каналов выявления событий" in profile
    assert "Выявлено клиентами**: 1" in profile
    assert "Выявлено внешними контролирующими органами/регуляторами**: 1" in profile


def test_retrospective_risk_profile_analysis():
    df = pd.DataFrame({
        "incdnt_id": [1, 2],
        "incdnt_entry_dt": ["2026-07-01 10:00:00", "2026-07-02 10:00:00"],
        "incdnt_sum": [100.0, 200.0],
        "incdnt_status_name": ["Утверждён", "Утверждён"],
        "risk_profile_id": ["RP-NEW", "RP-OLD"],
        "risk_profile_name": ["Новый риск", "Старый риск"]
    })
    loop = asyncio.get_event_loop()
    res = loop.run_until_complete(generate_hypothesis_narrative(
        "анализ", df, {"name": "test.xlsx", "size": "1KB"}, "session-123"
    ))
    assert "Ретроспективный анализ профилей риска" in res
    assert "Новые профили риска (появились впервые в текущем периоде)" in res
    assert "Возобновившиеся профили риска (перерыв в регистрации >= 180 дней)" in res


if __name__ == "__main__":
    test_reorder_columns()
    print("Passed test_reorder_columns")
    test_get_total_and_direct_loss()
    print("Passed test_get_total_and_direct_loss")
    test_profile_dataframe_top_10_and_risk_profile()
    print("Passed test_profile_dataframe_top_10_and_risk_profile")
    test_deleted_statistics()
    print("Passed test_deleted_statistics")
    test_functional_blocks_and_detection_channels()
    print("Passed test_functional_blocks_and_detection_channels")
    test_retrospective_risk_profile_analysis()
    print("Passed test_retrospective_risk_profile_analysis")
    print("ALL NEW TESTS OK!")
