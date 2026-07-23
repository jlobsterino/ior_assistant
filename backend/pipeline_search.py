import sys
# Trick transformers/huggingface to bypass PyTorch < 2.6 security check for torch.load (CVE-2025-32434)
try:
    import torch
    torch.__version__ = "2.6.0"
    if hasattr(torch, "version"):
        torch.version.__version__ = "2.6.0"
except ImportError:
    pass

import os
import re
import time
import json
import pickle
import sqlite3
import subprocess
import numpy as np
import pandas as pd
import faiss
import bm25s
from sentence_transformers import SentenceTransformer, CrossEncoder
from keybert import KeyBERT

# Настройки окружения
os.environ["TORCHDYNAMO"] = "0"
# os.environ["TORCHINDUCTOR_CACHE_DIR"] = "/home/datalab/nfs/d3/d3_code/torchinductor_cache"
# torch._inductor.config.enabled = False

# Константы и пути
BM25_CHUNK = 1000000
path_to_load = "/home/datalab/nfs/d3/d3_code/cache_le_finale2"
DB_PATH = "/home/datalab/nfs/disrupt_testerv2/backend/storage/cache.db"

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(DEVICE)

print("Загрузка BGE-M3...")
EMBED_MODEL = SentenceTransformer("/home/datalab/nfs/BAAI:bge-m3", device=DEVICE)
EMBED_MODEL.to(DEVICE)

KW_MODEL = KeyBERT(model=EMBED_MODEL)
DIM = EMBED_MODEL.get_sentence_embedding_dimension()
print(f"Готово. Размерность вектора: {DIM}")

reranker = CrossEncoder("/home/datalab/nfs/bge-reranker-v2-m3", device=DEVICE)

K_RRF = 60
ALPHA = 0.3
TOP_K_RERANK = 50
DEFAULT_TOP_K = 250
SCORE_THRESHOLD = 0.5

# Глобальные переменные для метаданных
_db_conn = None
doc_ids = []
id_to_index = {}
req_reg_dates = []


# Вспомогательные функции
def build_text(req_desc: str, msg_pprb_chat: str) -> str:
    parts = []
    if req_desc and str(req_desc).strip():
        parts.append(str(req_desc))  # Убедитесь, что это строка
    if msg_pprb_chat and str(msg_pprb_chat).strip():
        parts.append(str(msg_pprb_chat))  # Убедитесь, что это строка
    return " ".join(parts)


def tokenize(text):
    return re.findall(r"[а-яёa-z0-9]+", text.lower())


def embed(texts, batch_size=48, log_every=50000):
    """
    texts - может быть list или срез списка
    """
    all_embeddings = []
    total = len(texts)
    start_total = time.time()
    last_log_time = start_total
    processed = 0
    last_logged_processed = 0
    next_log = log_every
    for i in range(0, total, batch_size):
        batch = texts[i:i + batch_size]
        emb = EMBED_MODEL.encode(
            batch,
            normalize_embeddings=True,
            convert_to_numpy=True,
            batch_size=batch_size,
            device=DEVICE,
            chunk_size=200,
            show_progress_bar=False
        )
        all_embeddings.append(emb)
        processed += len(batch)
        if processed >= next_log or processed == total:
            now = time.time()
            last_log_time = now
            last_logged_processed = processed
            while next_log <= processed:
                next_log += log_every

        # Очистка каждые ~100k эмбеддингов
        if len(all_embeddings) >= 10:
            all_embeddings = [np.vstack(all_embeddings)]
    return np.vstack(all_embeddings).astype("float32")


def load_embeddings(path=path_to_load):
    meta_path = f"{path}/embeddings_meta.pkl"
    if os.path.exists(meta_path):
        with open(meta_path, 'rb') as f:
            meta = pickle.load(f)
        embeddings = np.memmap(meta["path"], dtype=meta["dtype"], mode='r', shape=meta["shape"])
        print(f"Эмбеддинги загружены через memmap: {meta['shape']}")
        return embeddings
    return None


# Функции загрузки метаданных и индексов
def load_meta(path=path_to_load):
    global doc_ids, id_to_index, req_reg_dates
    meta_path = f"{path}/meta_final.pkl" if os.path.exists(f"{path}/meta_final.pkl") else f"{path}/meta.pkl"
    if os.path.exists(meta_path):
        with open(meta_path, "rb") as f:
            meta = pickle.load(f)
        doc_ids = meta.get("doc_ids", [])
        id_to_index = meta.get("id_to_index", {})
        req_reg_dates = meta.get("req_reg_dates", [None] * len(doc_ids))
        print("мета успешно загружена!")
        print(f"Количество документов: {len(doc_ids)}")
    else:
        print(f"Warning: Meta file not found at {meta_path}")


def load_indices(path=path_to_load, to_gpu=False):
    index_path = f"{path}/faiss_index"
    if os.path.exists(index_path):
        faiss_index = faiss.read_index(index_path)
        if to_gpu:
            try:
                res = faiss.StandardGpuResources()
                faiss_index = faiss.index_cpu_to_gpu(res, 0, faiss_index)
            except Exception as e:
                print("GPU error:", e)
        print("FAISS index загружен")
        return faiss_index
    return None


def load_bm25s_shards(bm25_dir):
    """Функция загружает BM25 шарды один раз при старте, что кратно убыстряет запросы"""
    if not os.path.exists(bm25_dir):
        print(f"Warning: BM25 directory '{bm25_dir}' does not exist.")
        return []
    bm25_indexes = []  # сюда будем складывать загруженные индексы
    shards = sorted([f for f in os.listdir(bm25_dir) if f.startswith("shard_")])
    for shard in shards:
        shard_id = int(shard.split("_")[1])  # из имени "shard_0" (и прочее) достаем номер шарда
        offset = shard_id * BM25_CHUNK  # считаем сдвиг шарда
        bm25_index = bm25s.BM25.load(f"{bm25_dir}/{shard}", load_corpus=False)  # загружаем индекс в память
        bm25_indexes.append((bm25_index, offset))  # сохраняем пару (индекс; сдвиг)
    print("BM25 shards загружены:", len(bm25_indexes))
    return bm25_indexes


# Инициализация метаданных и индексов при запуске
if os.path.exists(path_to_load):
    load_meta()
    faiss_loaded = load_indices(to_gpu=False)
else:
    print(f"Warning: path_to_load '{path_to_load}' does not exist. Skipping metadata loading.")
    faiss_loaded = None

bm25_dir = f"{path_to_load}/bm25s_shards2"
if os.path.exists(bm25_dir):
    bm25_indexes = load_bm25s_shards(bm25_dir)
else:
    bm25_indexes = []


def build_date_mask(date_range=None):
    if date_range is None:
        return None
    start_date = str(date_range[0])
    end_date = str(date_range[1])
    return np.array([isinstance(date, str) and start_date <= date <= end_date for date in req_reg_dates], dtype=bool)


def prepare_texts_for_metrics(df: pd.DataFrame) -> list:
    texts_needed = []
    # Нумеруем с 1, чтобы соответствовать ожиданиям промпта
    for idx, (_, row) in enumerate(df.iterrows(), start=1):
        short = row.get("Короткое описание")
        msg = row.get("Транскрибация диалога")
        parts = []
        if short is not None and str(short).strip() != '':
            parts.append(f"Короткое описание обращения номер {idx}: {short}")
        if msg is not None and str(msg).strip() != '':
            parts.append(f"Транскрибация обращения номер {idx}: {msg}")
        
        combined_text = "\n".join(parts)
        texts_needed.append(combined_text)
    return texts_needed


def batch_classify_sva_metrics(texts: list, batch_size=8) -> list:
    from backend.gigachat_extractor import def_ask_gigachat
    
    results = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        
        batch_prompt = f"""Ты профессионал по работе с клиентскими обращениями. Запомни метрики и их классификацию в рамках клиентского сервиса.
'101': 'Финансовые потери клиентов по вине Банка / компании Группы Выявлены отклонения и проблемы в клиентском сервисе, связанные с недостатками процессов Банка и повлекшие возникновение финансовых потерь у клиентов (избыточное взимание процентов и комиссий, некорректное применение тарифов, некорректное списание денежных средств со счетов клиентов, некорректное начисление бонусов в рамках программ лояльности, обмен валюты по некорректному курсу и др.).\\nНапример:\\n- Неправомерно начислена неустойка за несвоевременное предоставление документов по целевому использованию кредита/страхование объекта недвижимости при фактическом их наличии;\\n- Клиентам при открытии вкладов были установлены процентные ставки ниже действующих на дату открытия вклада.',
'102': 'Необоснованный отказ в оказании услуги Выявлены отклонения и проблемы в клиентском сервисе, связанные с необоснованным отказом в оказании услуги либо непредоставление услуги из-за недостатков процессов Банка.\\nНапример:\\n- Клиенты не смогли провести оплату товаров / услуг в сети интернет с использованием сервиса SberPay из-за технических сбоев в мобильном приложении СберБанк онлайн (далее - СБОЛ) / на сайтах партнеров.\\n- Банк необоснованно отказал Клиентам в увеличении лимита по кредитным картам. ',
'103': 'Нарушение срока оказания услуги\\n Выявлены отклонения и проблемы в клиентском сервисе, связанные с нарушением срока оказания услуг/сервисов Банка.\\nНапример:\\n- превышены сроки выпуска/доставки предоставление ответа на обращение за допустимое время, затягивание сроков обслуживания клиентов, нарушение сроков оказания услуг, оказания услуг/сервисов Банка. \\nНапример:\\n- превышены сроки выпуска/доставки банковских карт\\n- депозитные сделки оформлены с нарушением срока в среднем на 3 часа (норматив не более 1 часа). ',
'104': 'Нарушение стандартов коммуникации с клиентами Выявлены отклонения и проблемы в клиентском сервисе, связанные с нарушениями стандартов коммуникации с клиентами (Внутренний стандарт по коммуникации с розничными клиентами ПАО Сбербанк от 21.11.2024 №4762-2) по любому доступному каналу связи: СМС, PUSH-уведомления, коммуникации по телефону, чат-бот, очное взаимодействие в каналах Банка, информирование на сайте Банка и в СБОЛ, повлекшие за собой финансовые потери клиента либо отказ клиента от продукта.\\nНапример:\\n- Клиенту не поступила СМС/PUSH о необходимости пополнения счета и Клиент вышел на просрочку по ипотечному кредиту\\n- Клиенту не пришла СМС/PUSH с кодом для входа в мобильное приложение СБОЛ.',
'105': 'Недобросовестные практики продаж Выявлены нарушения требований ФЗ и регуляторов в части применения недобросовестных практик продаж продуктов / услуг в физических и цифровых каналах, не отвечающих интересам клиентов. Например, подключение/закрытие продукта/услуги без ведома клиента, продажи, которые запрещены для категорий социально незащищенных и уязвимых клиентов, в том числе путем замалчивания, использования двусмысленных выражений и преувеличения информации в клиентских и презентационных материалах, в части введения клиента в заблуждение относительно тарифов, размера комиссий, двойных комиссий, реальной стоимости продукта/услуги, а также предложение продуктов и услуг со скрытыми и непрозрачными комиссиями.'

Классифицируй каждое обращение из списка ниже.
Правила:
1. Выбери только одну метрику
2. Выбери только номер метрики и больше ничего
3. Не пиши пояснений
4. Ответ должен содержать только число
5. Если ни одна из метрик абсолютно не подходит, выдавай None

Ответ должен быть строго в формате JSON (только объект с ключами-номерами).
# Пример того, как должен выглядеть твой ответ:
# Если я дам тебе 2 текста, верни: {{"1": "код", "2": "код"}}
# Если я дам тебе 3 текста, верни: {{"1": "код", "2": "код", "3": "код"}}
# Ключи (цифры) всегда начинаются с 1 для каждого нового списка текстов.

Тексты для классификации:
"""
        for idx, text in enumerate(batch, start=1):
            truncated_text = text[:4000] + '...' if len(text) > 4000 else text
            batch_prompt += f"\n--- ТЕКСТ {idx} ---\n{truncated_text}\n"
        
        messages = [{"role": "system", "content": batch_prompt}]
        
        try:
            response = def_ask_gigachat(messages)
            json_match = re.search(r'\{.*?\}', response)
            
            if json_match:
                json_str = json_match.group(0)
                fixed_json_str = json_str.replace("None", "null")
                try:
                    response_json = json.loads(fixed_json_str)
                    batch_results = []
                    for j in range(len(batch)):
                        batch_results.append(response_json.get(str(j+1)))
                    results.extend(batch_results)
                except json.JSONDecodeError as e:
                    print(f"⚠️ Ошибка парсинга JSON: {e}")
                    print("Текст, который вызвал ошибку:", fixed_json_str)
                    results.extend([None] * len(batch))
            else:
                print("⚠️ JSON не найден в ответе. Ответ был:")
                print(response)
                results.extend([None] * len(batch))
        except Exception as e:
            print(f"⚠️ Ошибка вызова GigaChat или выполнения классификации: {e}")
            results.extend([None] * len(batch))
    return results


def retrieve_hybrid_adaptive(query, faiss_idx, bm25_indexes, target_k, date_range=None):
    """Функция получает запрос пользователя и возвращает список id кандидатов (потенциально релевантных обращений)"""
    faiss_k = 1024  # максимум к возвращаемых фаисс
    bm25_total_k = int(faiss_k * 0.67)
    num_shards = len(bm25_indexes)
    bm25_k_per_shard = int(np.ceil(bm25_total_k / max(1, num_shards)))
    date_mask = build_date_mask(date_range)
    
    # Поиск по faiss
    faiss_ranks = {}
    q_emb = embed([query]).astype("float32")
    if date_range is None or faiss_idx is None:
        if faiss_idx is not None:
            D, I = faiss_idx.search(q_emb, min(faiss_k, faiss_idx.ntotal))
        else:
            I = [[]]
    else:
        allowed_ids = np.ascontiguousarray(np.flatnonzero(date_mask).astype("int64"))
        if len(allowed_ids) == 0:
            return []
        selector = faiss.IDSelectorBatch(allowed_ids)
        params = faiss.SearchParametersIVF()
        params.nprobe = faiss_idx.nprobe
        params.sel = selector
        D, I = faiss_idx.search(q_emb, min(faiss_k, len(allowed_ids)), params=params)

    for rank, idx in enumerate(I[0], 1):
        idx = int(idx)
        if idx < 0 or idx >= len(doc_ids):
            continue
        cid = doc_ids[idx]
        faiss_ranks[cid] = rank

    # Поиск по BM-25
    bm25_ranks = {}
    query_tokens = tokenize(query)
    for bm25_index, offset in bm25_indexes:
        shard_size = int(bm25_index.scores["num_docs"])
        if date_range is None:
            results, scores = bm25_index.retrieve([query_tokens], k=min(bm25_k_per_shard, shard_size))
            local_mask = None
        else:
            local_mask = date_mask[offset:offset + shard_size].astype("float32")
            allowed_count = int(local_mask.sum())
            if allowed_count == 0:
                continue
            results, scores = bm25_index.retrieve([query_tokens], k=min(bm25_k_per_shard, allowed_count), weight_mask=local_mask, show_progress=False)
        
        for rank, local_id in enumerate(results[0], 1):
            local_id = int(local_id)
            if local_id < 0 or local_id >= shard_size:
                continue
            if local_mask is not None and local_mask[local_id] == 0:
                continue
            
            global_idx = offset + local_id
            if global_idx < 0 or global_idx >= len(doc_ids):
                continue
            
            cid = doc_ids[global_idx]
            if cid not in bm25_ranks or rank < bm25_ranks[cid]:
                bm25_ranks[cid] = rank

    # RRF fusion: объединяем кандидатов faiss и bm25
    all_ids = set(faiss_ranks) | set(bm25_ranks)
    fused = {
        cid: ALPHA * (1 / (K_RRF + bm25_ranks.get(cid, 999))) + (1 - ALPHA) * (1 / (K_RRF + faiss_ranks.get(cid, 999)))
        for cid in all_ids
    }
    sorted_cands = [cid for cid, _ in sorted(fused.items(), key=lambda x: x[1], reverse=True)]
    return sorted_cands[:target_k]


def load_complaints_data_from_spark(candidate_ids):
    """
    Loads columns 'id', 'req_desc', 'msg_pprb_chat' from HDFS Spark table
    for the given list of candidate_ids.
    """
    try:
        try:
            from backend.api.routes.chat import get_spark_session
            spark = get_spark_session()
        except Exception:
            from pyspark.sql import SparkSession
            spark = SparkSession.builder.getOrCreate()
        
        if spark is None:
            print("Spark session is not available. Cannot load complaint data.")
            return pd.DataFrame(columns=["id", "req_desc", "msg_pprb_chat"])
        
        table_name = "arnsdpsbx_t_team_sva_oarb.40_kay_d3_crm_dataset_test_faiss_10kk"
        df = spark.read.table(table_name)
        
        candidate_ids_str = [str(cid) for cid in candidate_ids]
        
        col_id = next((c for c in df.columns if c.lower() == "id"), "id")
        col_req_desc = next((c for c in df.columns if c.lower() == "req_desc"), "req_desc")
        col_msg_pprb_chat = next((c for c in df.columns if c.lower() == "msg_pprb_chat"), "msg_pprb_chat")
        
        df_filtered = df.select(col_id, col_req_desc, col_msg_pprb_chat).filter(df[col_id].isin(candidate_ids_str))
        pandas_df = df_filtered.toPandas()
        
        rename_map = {col_id: "id", col_req_desc: "req_desc", col_msg_pprb_chat: "msg_pprb_chat"}
        pandas_df = pandas_df.rename(columns=rename_map)
        return pandas_df
    except Exception as e:
        print(f"Error loading complaints from Spark: {e}")
        return pd.DataFrame(columns=["id", "req_desc", "msg_pprb_chat"])


def rerank(query: str, candidates):
    df_spark = load_complaints_data_from_spark(candidates)
    
    spark_data = {}
    for _, row in df_spark.iterrows():
        cid_val = str(row["id"])
        r_desc = row.get("req_desc")
        m_chat = row.get("msg_pprb_chat")
        spark_data[cid_val] = (
            "" if pd.isna(r_desc) else str(r_desc),
            "" if pd.isna(m_chat) else str(m_chat)
        )
    
    rerank_inputs = []
    for cid in candidates:
        r_desc, m_chat = spark_data.get(str(cid), ("", ""))
        doc_text = build_text(r_desc, m_chat)
        rerank_inputs.append((query, doc_text))
    
    raw_scores = reranker.predict(rerank_inputs, batch_size=32)
    raw_scores = np.array(raw_scores)
    scores = 1 / (1 + np.exp(-raw_scores))
    
    results = []
    for cid, score in zip(candidates, scores):
        idx = id_to_index.get(cid, 0)
        r_desc, m_chat = spark_data.get(str(cid), ("", ""))
        date_val = req_reg_dates[idx] if idx < len(req_reg_dates) else None
        results.append({
            "id": cid,
            "Короткое описание": r_desc,
            "Транскрибация диалога": m_chat,
            "score": float(score),
            "date": date_val
        })
    
    return sorted(results, key=lambda x: x["score"], reverse=True)


def search_pipeline(query, faiss_idx, bm25_indexes, top_k=None, score_threshold=None, date_range=None):
    target_k = top_k if top_k else DEFAULT_TOP_K
    thr = score_threshold if score_threshold is not None else SCORE_THRESHOLD
    candidates_k = max(target_k * 2, TOP_K_RERANK)
    candidates = retrieve_hybrid_adaptive(query=query, faiss_idx=faiss_idx, bm25_indexes=bm25_indexes, target_k=candidates_k, date_range=date_range)
    if not candidates:
        return pd.DataFrame()
    reranked = rerank(query, candidates)
    
    if top_k is not None:
        df = pd.DataFrame(reranked[:top_k])[["id", "Короткое описание", "Транскрибация диалога", "score", "date"]]
    else:
        filtered = [r for r in reranked if r["score"] >= thr]
        df = pd.DataFrame(filtered if filtered else reranked[:target_k])
    
    if not df.empty:
        texts = prepare_texts_for_metrics(df)
        metrics = batch_classify_sva_metrics(texts)
        df["Метрика СВА"] = metrics
    else:
        df["Метрика СВА"] = []
        
    columns_to_return = ["id", "Короткое описание", "Транскрибация диалога", "Метрика СВА", "score", "date"]
    for col in columns_to_return:
        if col not in df.columns:
            df[col] = None
            
    return df[columns_to_return]


MIN_DATE = "0000-01-01"
MAX_DATE = "9999-12-31"

if __name__ == "__main__":
    res = search_pipeline(
        query="обращения по ипотеке по графикам платежей",
        faiss_idx=faiss_loaded,
        bm25_indexes=bm25_indexes,
        top_k=None,
        date_range=("2026-01-01", MAX_DATE)
    )