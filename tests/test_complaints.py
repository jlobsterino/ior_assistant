import sys
import os
import unittest
import asyncio
from unittest.mock import MagicMock, patch
import numpy as np
import pandas as pd

# Mock heavy modules for local testing
try:
    import bm25s
except ImportError:
    sys.modules['bm25s'] = MagicMock()

try:
    import faiss
except ImportError:
    faiss_mock = MagicMock()
    faiss_mock.IndexFlatIP = MagicMock(return_value=MagicMock())
    sys.modules['faiss'] = faiss_mock

try:
    import torch
except ImportError:
    torch_mock = MagicMock()
    torch_mock.cuda = MagicMock()
    torch_mock.cuda.is_available = MagicMock(return_value=False)
    sys.modules['torch'] = torch_mock

try:
    import sentence_transformers
except ImportError:
    st_mock = MagicMock()
    st_mock.SentenceTransformer = MagicMock()
    st_mock.CrossEncoder = MagicMock()
    sys.modules['sentence_transformers'] = st_mock

try:
    import keybert
except ImportError:
    kb_mock = MagicMock()
    kb_mock.KeyBERT = MagicMock()
    sys.modules['keybert'] = kb_mock

try:
    import transformers
except ImportError:
    sys.modules['transformers'] = MagicMock()

import builtins
builtins.ior_shared_cache = {
    "EMBED_MODEL": MagicMock(),
    "KW_MODEL": MagicMock(),
    "DIM": 128,
    "reranker": MagicMock(),
    "p_meta": {
        "doc_ids": ["1", "2", "3"],
        "id_to_index": {"1": 0, "2": 1, "3": 2},
        "req_reg_dates": ["2026-01-01", "2026-01-02", "2026-01-03"]
    },
    "p_faiss_loaded": MagicMock(),
    "p_bm25_indexes": []
}

import backend.pipeline_search as ps
# Populate the mock metadata globals
ps.doc_ids = ["1", "2", "3"]
ps.id_to_index = {"1": 0, "2": 1, "3": 2}
ps.req_reg_dates = ["2026-01-01", "2026-01-02", "2026-01-03"]

from backend.pipeline_search import (
    load_meta,
    load_complaints_data_from_spark,
    rerank,
    search_pipeline,
    doc_ids,
    id_to_index,
    req_reg_dates
)

from backend.agent.complaint_hypothesis import (
    extract_short_descriptions_summary,
    profile_complaints_dataframe,
    generate_complaint_hypothesis_narrative
)


class TestComplaintsPipeline(unittest.TestCase):
    
    def test_load_meta_structure(self):
        # Verify global metadata lists are correct
        self.assertEqual(len(doc_ids), 3)
        self.assertIn("1", id_to_index)
        self.assertEqual(id_to_index["1"], 0)
        self.assertEqual(req_reg_dates[0], "2026-01-01")

    @patch('pyspark.sql.SparkSession')
    def test_load_complaints_data_from_spark(self, mock_spark_session_cls):
        # Mock SparkSession builder and session
        mock_builder = mock_spark_session_cls.builder
        mock_spark = MagicMock()
        mock_builder.getOrCreate.return_value = mock_spark
        
        mock_df = MagicMock()
        mock_df.columns = ["id", "req_desc", "msg_pprb_chat"]
        
        # Mock pandas dataframe conversion
        mock_pandas_df = pd.DataFrame({
            "id": ["1", "2"],
            "req_desc": ["Desc 1", "Desc 2"],
            "msg_pprb_chat": ["Chat 1", "Chat 2"]
        })
        
        # Setup selection and filter sequence return value
        mock_df.select.return_value.filter.return_value.toPandas.return_value = mock_pandas_df
        mock_spark.read.table.return_value = mock_df
        
        res_df = load_complaints_data_from_spark(["1", "2"])
        self.assertEqual(len(res_df), 2)
        self.assertEqual(list(res_df["id"]), ["1", "2"])
        self.assertEqual(list(res_df["req_desc"]), ["Desc 1", "Desc 2"])

    @patch('backend.pipeline_search.load_complaints_data_from_spark')
    def test_rerank_with_dynamic_data(self, mock_spark_loader):
        # Mock load from Spark
        mock_spark_loader.return_value = pd.DataFrame({
            "id": ["1", "2"],
            "req_desc": ["Desc 1", "Desc 2"],
            "msg_pprb_chat": ["Chat 1", "Chat 2"]
        })
        
        # Mock cross encoder prediction scores
        from backend.pipeline_search import reranker
        reranker.predict.return_value = [0.8, 0.5]
        
        res = rerank("test query", ["1", "2"])
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["id"], "1")
        self.assertEqual(res[0]["Короткое описание"], "Desc 1")
        self.assertEqual(res[0]["Транскрибация диалога"], "Chat 1")
        self.assertAlmostEqual(res[0]["score"], 0.68997448) # 1 / (1 + exp(-0.8))

    def test_extract_short_descriptions_summary_limit_200_and_grouping(self):
        # Test deduplication & frequency counting up to 200 items
        descs = ["Потеря карты"] * 10 + ["Блокировка карты"] * 5 + [f"Уникальное описание {i}" for i in range(250)]
        df = pd.DataFrame({"Короткое описание": descs})
        
        summary = extract_short_descriptions_summary(df, max_unique=200)
        self.assertIn("Потеря карты", summary)
        self.assertIn("10 обращений", summary)
        self.assertIn("Блокировка карты", summary)
        self.assertIn("5 обращений", summary)
        self.assertIn("топ-200", summary)

    def test_profile_complaints_dataframe_with_sva_metrics(self):
        df = pd.DataFrame({
            "id": ["1", "2", "3"],
            "Короткое описание": ["Проблема с картой", "Сбой приложения", "Проблема с картой"],
            "Транскрибация диалога": ["Диалог 1", "Диалог 2", "Диалог 3"],
            "score": [0.9, 0.8, 0.7],
            "date": ["2026-01-01", "2026-01-02", "2026-01-03"],
            "Метрика СВА": ["101", "102", "101"]
        })
        
        profile = profile_complaints_dataframe(df)
        self.assertIn("Распределение по метрикам СВА:", profile)
        self.assertIn("Метрика '101': 2 обращений", profile)
        self.assertIn("Проблема с картой", profile)

    @patch("backend.agent.complaint_hypothesis.def_ask_gigachat")
    def test_generate_complaint_hypothesis_batching(self, mock_ask):
        mock_ask.side_effect = ["Отчет по батчу 1", "Отчет по батчу 2", "Финальный отчет с гипотезами"]
        
        # 12 complaints to trigger 2 batches of top 5 transcriptions
        df = pd.DataFrame({
            "id": [str(i) for i in range(1, 13)],
            "Короткое описание": [f"Описание {i}" for i in range(1, 13)],
            "Транскрибация диалога": [f"Транскрибация диалога номер {i}" for i in range(1, 13)],
            "score": [0.9 - i * 0.01 for i in range(12)],
            "date": ["2026-01-05"] * 12,
            "Метрика СВА": ["101"] * 12
        })
        file_info = {"name": "test_complaints.xlsx"}
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            narrative = loop.run_until_complete(
                generate_complaint_hypothesis_narrative("Сделай гипотезу по сбоям", df, file_info)
            )
            self.assertEqual(narrative, "Финальный отчет с гипотезами")
            self.assertEqual(mock_ask.call_count, 3)
        finally:
            loop.close()


if __name__ == '__main__':
    unittest.main()
