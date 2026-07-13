import sys
from unittest.mock import MagicMock
import numpy as np
import pandas as pd

# Mock heavy/external modules if they are not installed (e.g. in local windows env)
try:
    import bm25s
except ImportError:
    sys.modules['bm25s'] = MagicMock()

try:
    import faiss
except ImportError:
    faiss_mock = MagicMock()
    # Ensure IndexFlatIP returns a mock index
    faiss_mock.IndexFlatIP = MagicMock(return_value=MagicMock())
    faiss_mock.normalize_L2 = MagicMock()
    sys.modules['faiss'] = faiss_mock

try:
    import sentence_transformers
    from sentence_transformers import SentenceTransformer, CrossEncoder
except ImportError:
    st_mock = MagicMock()
    st_mock.SentenceTransformer = MagicMock()
    st_mock.CrossEncoder = MagicMock()
    sys.modules['sentence_transformers'] = st_mock

try:
    import keybert
    from keybert import KeyBERT
except ImportError:
    kb_mock = MagicMock()
    kb_mock.KeyBERT = MagicMock()
    sys.modules['keybert'] = kb_mock

try:
    import torch
except ImportError:
    torch_mock = MagicMock()
    torch_mock.cuda = MagicMock()
    torch_mock.cuda.is_available = MagicMock(return_value=False)
    sys.modules['torch'] = torch_mock

# Pre-populate builtins.ior_shared_cache to bypass file load on import
import builtins
builtins.ior_shared_cache = {
    "EMBED_MODEL": MagicMock(),
    "KW_MODEL": MagicMock(),
    "DIM": 128,
    "reranker": MagicMock(),
    "ior_meta": {
        "documents": [],
        "doc_sids": [],
        "id_to_index": {},
        "incident_ids": [],
        "incident_dates": []
    },
    "ior_embeddings": np.zeros((3, 128), dtype="float32"),
    "ior_faiss_loaded": MagicMock(),
    "ior_bm25_indexes": []
}

# Now import the actual code to test
from backend.IOR_pipeline_search import get_id_variations, build_and_cache_small_index, _SMALL_FAISS_SESSION_CACHE
import backend.IOR_pipeline_search as ior_search
from backend.agent.hypothesis import profile_dataframe, generate_dynamics_chart
from backend.config import get_settings


def test_get_id_variations_alphanumeric():
    res = get_id_variations("EVE-7818291")
    assert "EVE-7818291" in res
    assert len(res) == 1


def test_get_id_variations_float_string():
    # Large 19-digit float-like string
    res = get_id_variations("1234567890123456789.0")
    assert "1234567890123456789.0" in res
    assert "1234567890123456789" in res
    assert 1234567890123456789 in res


def test_get_id_variations_digit_string():
    res = get_id_variations("12345")
    assert "12345" in res
    assert 12345 in res
    assert "12345.0" in res


def test_get_id_variations_integer():
    res = get_id_variations(1234567890123456789)
    assert "1234567890123456789" in res
    assert 1234567890123456789 in res
    assert "1234567890123456789.0" in res


def test_build_and_cache_small_index_mapping():
    # Setup mock global indices
    original_sid_to_index = ior_search.sid_to_index
    original_id_to_index = ior_search.id_to_index
    original_embeddings = ior_search.embeddings
    
    try:
        # Mock index structure
        ior_search.sid_to_index = {"EVE-7818291": 0}
        ior_search.id_to_index = {1234567890123456789: 1, "99999": 2}
        ior_search.embeddings = np.random.rand(3, 128).astype("float32")
        
        session_id = "test_session_id"
        target_ids = ["EVE-7818291", "1234567890123456789.0", 99999]
        id_to_text_map = {
            "EVE-7818291": "Description 1",
            "1234567890123456789.0": "Description 2",
            99999: "Description 3"
        }
        
        success = build_and_cache_small_index(session_id, target_ids, id_to_text_map)
        assert success is True
        
        session_data = _SMALL_FAISS_SESSION_CACHE.get(session_id)
        assert session_data is not None
        
        # Verify descriptions are mapped correctly
        mapped_ids = [item["id"] for item in session_data["descriptions"]]
        assert "EVE-7818291" in mapped_ids
        assert "1234567890123456789.0" in mapped_ids
        assert 99999 in mapped_ids
        
    finally:
        # Restore original state
        ior_search.sid_to_index = original_sid_to_index
        ior_search.id_to_index = original_id_to_index
        ior_search.embeddings = original_embeddings


def test_profile_dataframe_date_resolution():
    # Setup dataframe with 35 rows to bypass the small df.to_markdown branch and trigger temporal analysis
    df = pd.DataFrame({
        "incdnt_id": list(range(1, 36)),
        "incdnt_desc": ["test"] * 35,
        "start_date": ["2016-01-01"] * 35,
        "incdnt_entry_dt": ["2025-07-01 10:00:00"] * 35,
        "incdnt_loss_sum": [1000] * 35
    })
    
    profile = profile_dataframe(df)
    # The profile should group by entry month (2025-07) rather than start_date month (2016-01)
    assert "2025-07" in profile
    assert "2016-01" not in profile


def test_generate_dynamics_chart():
    from backend.storage.database import FileRepo
    original_add = FileRepo.add
    
    mock_file = MagicMock()
    mock_file.id = "mock-chart-file-id"
    FileRepo.add = MagicMock(return_value=mock_file)
    
    try:
        df = pd.DataFrame({
            "incdnt_id": [1, 2, 3],
            "incdnt_desc": ["test1", "test2", "test3"],
            "incdnt_entry_dt": ["2025-07-01 10:00:00", "2025-07-02 11:00:00", "2025-07-03 12:00:00"],
            "incdnt_loss_sum": [1000, 2000, 3000]
        })
        
        session_id = "test_chart_session"
        
        # Generate chart
        chart_id = generate_dynamics_chart(df, session_id)
        assert chart_id == "mock-chart-file-id"
        
        # Cleanup generated files in test
        settings = get_settings()
        for f in settings.files_path.glob("chart_*test_chart_session*"):
            try:
                f.unlink()
            except OSError:
                pass
    finally:
        FileRepo.add = original_add


def test_ior_hypothesis_skill_registration():
    from backend.skills.registry import get_registry
    registry = get_registry()
    
    # Force reload to scan scripts dir again
    registry.reload()
    
    skill = registry.get("ior_hypothesis_v2")
    assert skill is not None
    assert skill.skill_id == "ior_hypothesis_v2"
    assert skill.notebook_path.name == "ior_hypothesis_v2.ipynb"
    assert "incdnt_entry_dt_begin" in skill.input_schema["required"]


def test_run_preset_async_emit():
    import asyncio
    from backend.agent.tools.run_preset import run_preset
    from unittest.mock import MagicMock
    
    ctx = MagicMock()
    emitted_events = []
    
    async def mock_emit(event, data):
        emitted_events.append((event, data))
        
    ctx.emit = mock_emit
    ctx.register_file = MagicMock()
    ctx.register_dataframe = MagicMock()
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            run_preset(ctx, "ior_hypothesis_v2", {
                "incdnt_entry_dt_begin": "2025-01-01",
                "incdnt_entry_dt_end": "2025-01-02"
            })
        )
        assert result.ok is True
        assert len(emitted_events) > 0
        assert emitted_events[0][0] == "notebook_phase"
        assert emitted_events[0][1]["phase"] == "spark_starting"
    finally:
        loop.close()


def test_run_preset_period_mapping():
    import asyncio
    from backend.agent.tools.run_preset import run_preset
    from unittest.mock import MagicMock
    from backend.skills.runners.notebook_runner import get_runner
    
    ctx = MagicMock()
    ctx.register_file = MagicMock()
    ctx.register_dataframe = MagicMock()
    
    runner = get_runner()
    original_run_phased = runner.run_phased
    called_params = []
    
    def mock_run_phased(*args, **kwargs):
        params = kwargs.get("params")
        called_params.append(params)
        return original_run_phased(*args, **kwargs)
        
    runner.run_phased = mock_run_phased
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            run_preset(ctx, "ior_hypothesis_v2", {
                "period": {"text": "Q1 2025"}
            })
        )
        assert result.ok is True
        assert len(called_params) == 1
        params = called_params[0]
        assert "period" not in params
        assert params["incdnt_entry_dt_begin"] == "2025-01-01"
        assert params["incdnt_entry_dt_end"] == "2025-03-31"
    finally:
        runner.run_phased = original_run_phased
        loop.close()


def test_profile_dataframe_zero_loss_warning():
    df = pd.DataFrame({
        "incdnt_id": list(range(1, 35)),
        "incdnt_desc": ["test"] * 34,
        "incdnt_sum": [0] * 34,
        "recovery": [0] * 34
    })
    profile = profile_dataframe(df)
    assert "Данные по потерям отсутствуют (равны нулю)" in profile
    assert "Не зацикливайся на нулевых потерях" in profile


def test_profile_dataframe_side_by_side_mapping():
    df = pd.DataFrame({
        "incdnt_id": list(range(1, 35)),
        "incdnt_desc": ["test"] * 34,
        "incdnt_sum": [1000] * 34,
        "recovery": [200] * 34
    })
    profile = profile_dataframe(df)
    assert "Общая сумма всех последствий (incdnt_sum)" in profile
    assert "Сумма возмещений (recovery)" in profile
    assert "Чистые потери (Net Loss)" in profile


def test_hypothesis_threshold_guard():
    import asyncio
    from backend.agent.hypothesis import generate_hypothesis_narrative
    df = pd.DataFrame({
        "incdnt_id": [101, 102, 103, 104, 105],
        "incdnt_sum": [1000, 2000, 3000, 4000, 5000],
        "recovery": [100, 200, 300, 400, 500],
        "incdnt_status_name": ["Утверждение", "Утверждение", "Черновик", "Удален", "Удален"]
    })
    file_info = {"name": "small_test.xlsx", "size": "15 KB"}
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        report = loop.run_until_complete(
            generate_hypothesis_narrative("Выведи гипотезы", df, file_info, "session_123")
        )
        assert "Всего инцидентов в файле" in report
        assert "15 000.00 ₽" in report
        assert "1 500.00 ₽" in report
        assert "13 500.00 ₽" in report
        assert "Аналитические гипотезы не формировались, так как размер выборки составляет менее 20 инцидентов" in report
        assert "Инцидент 105" in report
        assert "Инцидент 104" in report
    finally:
        loop.close()


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
