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
    import sys
    from unittest.mock import MagicMock
    sys.modules['transformers'] = MagicMock()
    sys.modules['torch'] = MagicMock()
    
    import asyncio
    from backend.agent.hypothesis import generate_hypothesis_narrative
    import local_qwen
    
    # Save original
    original_ask = local_qwen.ask_local_qwen
    called_prompts = []
    
    def mock_ask_local_qwen(messages, max_tokens=4096):
        for m in messages:
            called_prompts.append(m["content"])
        return "Это краткая суммаризация выборки."
        
    local_qwen.ask_local_qwen = mock_ask_local_qwen
    
    try:
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
            assert "3 000.00 ₽" in report
            assert "300.00 ₽" in report
            assert "2 700.00 ₽" in report
            assert "Это краткая суммаризация выборки." in report
            
            # Verify that the summarization instructions were appended to system prompt
            system_prompt = called_prompts[0]
            assert "размер выборки мал" in system_prompt or "НЕ ВКЛЮЧАЙ раздел" in system_prompt
        finally:
            loop.close()
    finally:
        local_qwen.ask_local_qwen = original_ask



def test_grounding_morphological_variants():
    from backend.agent.resolve.grounding import _adj_nominative_variants
    v1 = _adj_nominative_variants("эквайринга")
    assert "эквайринг" in v1
    v2 = _adj_nominative_variants("обращениям")
    assert "обращение" in v2



def test_empty_dataframe_export_success():
    import asyncio
    from backend.agent.tools.dataframe_ops import export_excel, export_csv
    from backend.agent.query_spec import CompileContext, compile_query_spec
    from backend.agent.schema import get_schema
    from datetime import date
    
    class MockContext:
        def __init__(self):
            self.dataframes = {"df_empty": pd.DataFrame(columns=["incdnt_id", "incdnt_sid"])}
            self.dataframe_meta = {}
            self.files = {}
            self.emit = MagicMock()
        def get_df(self, did):
            return self.dataframes[did]
        def register_dataframe(self, df, *args, **kwargs):
            from dataclasses import dataclass
            @dataclass
            class DfRef:
                df_id: str
            return DfRef(df_id="df_empty")
        def register_file(self, *args, **kwargs):
            from dataclasses import dataclass
            @dataclass
            class FileRef:
                file_id: str
                name: str
            return FileRef(file_id="file_empty", name="empty.xlsx")
            
    ctx = MockContext()
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        res_ex = loop.run_until_complete(export_excel(ctx, "df_empty", "empty.xlsx"))
        assert res_ex.ok is True
        assert res_ex.output["file_id"] is None
        
        res_csv = loop.run_until_complete(export_csv(ctx, "df_empty", "empty.csv"))
        assert res_csv.ok is True
        assert res_csv.output["file_id"] is None
        
        from backend.agent.tools.registry import REGISTRY as registry
        original_execute = registry.execute
        async def mock_execute(tool_name, args, state):
            from backend.agent.tools.base import ToolResult
            if tool_name == "query":
                return ToolResult(ok=True, output={"df_id": "df_empty"})
            return ToolResult(ok=True)
        registry.execute = mock_execute
        
        try:
            cctx = CompileContext(ctx=ctx, emit=ctx.emit, schema=get_schema(), now=date.today())
            spec = {
                "source": {"table": "d6_base_of_knowledge_ior"},
                "filters": [
                    {"kind": "period", "column": "incdnt_entry_dt", "op": "eq", "value": "2026-03-01"}
                ]
            }
            res_compile = loop.run_until_complete(compile_query_spec(cctx, spec, registry=registry))
            assert res_compile.ok is True
            assert res_compile.file_id is None
        finally:
            registry.execute = original_execute
    finally:
        loop.close()


def test_appeals_mapping_and_where_operator_mapping():
    from backend.data.base import build_where_clauses
    
    # 1. Test logical operator mapping
    where_dict = {"incdnt_id": {"gt": 100}, "direct_loss": {"eq": 500}}
    col_types = {"incdnt_id": "bigint", "direct_loss": "decimal"}
    clauses = build_where_clauses(where_dict, col_types)
    assert "incdnt_id > 100" in clauses
    assert "direct_loss = 500" in clauses
    
    # 2. Test appeals (src_type_lvl_2_name = "Обращение клиента") compound mapping
    where_appeals = {"src_type_lvl_2_name": "\u041e\u0431\u0440\u0430\u0449\u0435\u043d\u0438\u0435 \u043a\u043b\u0438\u0435\u043d\u0442\u0430"}
    clauses_app = build_where_clauses(where_appeals, {"src_type_lvl_2_name": "string"})
    expected_compound = (
        "(incdnt_detection_person_name = '\u041a\u043b\u0438\u0435\u043d\u0442' "
        "OR src_type_lvl_2_name LIKE '%\u043e\u0431\u0440\u0430\u0449\u0435\u043d\u0438%' "
        "OR incdnt_source_name LIKE '%\u043a\u043b\u0438\u0435\u043d\u0442%')"
    )
    assert expected_compound in clauses_app


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
