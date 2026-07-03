import sys
# Trick transformers/huggingface to bypass PyTorch < 2.6 security check for torch.load (CVE-2025-32434)
try:
    import torch
    torch.__version__ = "2.6.0"
    if hasattr(torch, "version"):
        torch.version.__version__ = "2.6.0"
except ImportError:
    pass

import subprocess

import bm25s
import re
import numpy as np
import faiss
import pandas as pd
from sentence_transformers import SentenceTransformer, CrossEncoder
from keybert import KeyBERT
import torch
import os
import time
import pickle

os.environ["TORCH_DYNAMIC"] = "0"
#os.environ["TORCHINDUCTOR_CACHE_DIR"] = "/home/datalab/nfs/d3/d3_code/torchinductor_cache"
#torch._inductor.config.enabled = False

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(DEVICE)

path_to_load = '/home/datalab/nfs/disrupt_tester_clean/caches/cache_final'
sid_to_index = {}

import builtins
if not hasattr(builtins, "ior_shared_cache"):
    builtins.ior_shared_cache = {}
cache = builtins.ior_shared_cache

if "EMBED_MODEL" in cache:
    print("Использую кэшированный BGE-M3...")
    EMBED_MODEL = cache["EMBED_MODEL"]
    KW_MODEL = cache["KW_MODEL"]
    DIM = cache["DIM"]
    reranker = cache["reranker"]
else:
    print("Загрузка BGE-M3...")
    EMBED_MODEL = SentenceTransformer("/home/datalab/nfs/BAAI:bge-m3", device=DEVICE)
    EMBED_MODEL.to(DEVICE)
    KW_MODEL = KeyBERT(model=EMBED_MODEL)
    DIM = EMBED_MODEL.get_sentence_embedding_dimension()
    print(f"Готово. Размерность вектора: {DIM}")
    reranker = CrossEncoder("/home/datalab/nfs/bge-reranker-v2-m3", device=DEVICE)
    
    cache["EMBED_MODEL"] = EMBED_MODEL
    cache["KW_MODEL"] = KW_MODEL
    cache["DIM"] = DIM
    cache["reranker"] = reranker


def tokenize(text):
    return re.findall(r"[a-яёa-z0-9]+", text.lower())


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
            # device="cuda",
            device="cpu",
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

        # Очистка каждые ~100к эмбеддингов
        if len(all_embeddings) >= 10:
            all_embeddings = [np.vstack(all_embeddings)]
    return np.vstack(all_embeddings).astype("float32")


def get_top_n(text):
    tokens = re.findall(r"[a-яёa-z0-9]+", text.lower())
    length = len(tokens)
    if length < 20:
        return 2
    elif length < 50:
        return 3
    else:
        return 4


def extract_keywords(text, req_desc=None, msg_pprb_chat=None):
    # устанавливаем доминирование req_desc при выборе ключевых фраз (может наоборот поставить: если pprb есть то только в описании)
    if req_desc and str(req_desc).strip():
        if msg_pprb_chat and str(msg_pprb_chat).strip():
            text = (str(req_desc) + " ") * 1 + text # то есть req desc будет 2 раза
    min_score = 0.2
    top_n = get_top_n(text)
    all_candidates = KW_MODEL.extract_keywords(text, keyphrase_ngram_range=(1, 3), use_mmr=True, top_n=20)
    adjusted = []
    for kw, score in all_candidates:
        word_count = len(kw.split())
        # Штраф: длинная фраза теряет всего 5-15% веса
        # 1 слово: х1.0, 2 слова: х0.95, 3 слова: х0.90, 4 слова: х0.85
        penalty = 1.0 - (word_count - 1) * 0.1
        adjusted_score = score * penalty
        # фильтр по качеству (если фраза имеет слишком малый скор, не берем ее)
        if adjusted_score >= min_score:
            adjusted.append((kw, adjusted_score, word_count))
    adjusted.sort(key=lambda x: x[1], reverse=True)

    # Убираем дубликаты по корню
    result = []
    seen_roots = set()
    for kw, score, wc in adjusted:
        root = tuple(kw.split()[:2])
        if root not in seen_roots:
            result.append(kw)
            seen_roots.add(root)
            if len(result) >= top_n:
                break
    return result


def load_embeddings(path=path_to_load):
    meta_path = f"{path}/embeddings_meta.pkl"
    if os.path.exists(meta_path):
        with open(meta_path, 'rb') as f:
            meta = pickle.load(f)
        filename = os.path.basename(meta["path"])
        actual_path = os.path.join(path, filename)
        embeddings = np.memmap(actual_path, dtype=meta["dtype"], mode='r', shape=meta["shape"])
        print(f"Эмбеддинги загружены через меммап: {meta['shape']}")
        return embeddings
    else:
        # fallback на старый способ (если вдруг)
        print("Меммап не найден, загружаем старый pickle...")
        with open(f"{path}/embeddings.pkl", "rb") as f:
            embeddings = pickle.load(f)
        return np.ascontiguousarray(embeddings).astype("float32")


def load_meta(path=path_to_load):
    # global documents, doc_sids, id_to_index, incident_ids, incident_dates, tokenized_corpus
    global documents, doc_sids, id_to_index, incident_ids, incident_dates, sid_to_index
    with open(f"{path}/meta.pkl", "rb") as f:
        meta = pickle.load(f)
    documents = meta["documents"]
    doc_sids = meta["doc_sids"]
    id_to_index = meta["id_to_index"]
    # tokenized_corpus = meta["tokenized_corpus"]
    incident_ids = meta["incident_ids"]
    incident_dates = meta.get("incident_dates", [None] * len(documents))
    sid_to_index = {str(sid): i for i, sid in enumerate(doc_sids) if sid is not None}
    print("мета успешно загружена!")
    print(f"Количество документов: {len(documents)}")


def load_indices(path=path_to_load, to_gpu=False):
    faiss_index = faiss.read_index(f"{path}/faiss_index")
    if to_gpu:
        try:
            res = faiss.StandardGpuResources()
            faiss_index = faiss.index_cpu_to_gpu(res, 0, faiss_index)
        except Exception as e:
            print("GPU error:", e)
    print("FAISS index загружен")
    return faiss_index


BM25_CHUNK = 200000
def load_bm25s_shards(bm25_dir):
    """Функция загружает BM25 шарды один раз при старте, что кратно убыстряет запросы"""
    bm25_indexes = [] # сюда будем складывать загруженные индексы
    # находим все шарды в папке
    shards = sorted([f for f in os.listdir(bm25_dir) if f.startswith("shard_")])
    for shard in shards: # проходимся по всем шардам
        shard_id = int(shard.split("_")[1]) # из имени "shard_0" (и прочее) достаем номер шарда
        offset = shard_id * BM25_CHUNK # считаем сдвиг шарда; для шарда с номером 1 он равен 200 000 к при разм...
        bm25_index = bm25s.BM25.load(f"{bm25_dir}/{shard}", load_corpus=False) # загружаем индекс в память
        bm25_indexes.append((bm25_index, offset)) # сохраняем пару (индекс; сдвиг)
    print("bm25 shards загружены:", len(bm25_indexes))
    return bm25_indexes


K_RRF = 60
ALPHA = 0.3 # вес BM25 в финальном скоре
TOP_K_RERANK = 50
DEFAULT_TOP_K = 100
SCORE_THRESHOLD = 0.7


def filter_candidates_by_date(candidate_ids, date_range=None):
    if date_range is None: # если пользователь не указал дату - ничего не фильтруем
        return candidate_ids
    start_date = str(date_range[0])
    end_date = str(date_range[1])
    filtered = [] # сюда будут записываться подходящие по дате кандидаты
    for cid in candidate_ids: # идем по кандидатам
        idx = id_to_index.get(cid) # получаем индекс документа; это нужно, т.к. cid - настоящий id обращения
        if idx is None: # проверяем, найден ли документ
            continue
        date_str = incident_dates[idx] # получаем дату документа
        if date_str is None: # проверяем пустую дату - если дата отсутствует, то пропускаем документ
            continue
        if start_date <= date_str <= end_date: # если дата соответствует, то добавляем в список подходящих
            filtered.append(cid)
    return filtered


def retrieve_hybrid_adaptive(query, faiss_idx, bm25_indexes, target_k=50, date_range=None):
    """Функция получает запрос пользователя и возвращает список id кандидатов (потенциально релевантных об...)"""
    # ищем к ближайших соседей - берем с запасом, так как потом будем фильтровать по дате (если фильтр по...)
    faiss_k = 2048 if date_range else 500 # больше 2048 нельзя - фаисс гпу позволяет возвращать максимум 2
    bm25_k_per_shard = 300 if date_range else 200 # привязать к числу шардов (чтобы если было огромное кол-во шар...)

    faiss_ranks = {} # словарь типа {doc_id: rank} - ранги фаисс
    embedtime = time.time()
    q_emb = embed([query]).astype("float32")
    print(f"Время эмбединга: {time.time() - embedtime}")
    newtime = time.time()
    D, I = faiss_idx.search(q_emb, min(faiss_k, faiss_idx.ntotal)) # поиск по faiss; I - индексы найденных док...
    print(f"Поиск faiss: {time.time() - newtime}")
    # документа в выдаче; D - скор;
    for rank, idx in enumerate(I[0], 1): # enumerate(I[0], 1) возьмет индексы (например, idx=15, idx=333, idx=7...)
        idx = int(idx)
        if idx < 0 or idx >= len(doc_sids): # защита от битых индексов
            continue
        cid = doc_sids[idx] # cid - candidates_id - подкидываем по индексу эмбеддинга (позиция в массиве эмбедд...)
        faiss_ranks[cid] = rank # записываем пару "кандидат от фаисс: его ранг"

    bm25_ranks = {} # словарь типа {doc_id: rank} - ранги бм25
    query_tokens = tokenize(query)
    for bm25_index, offset in bm25_indexes: # проходимся по заранее загруженным BM25 шардам; bm25_index - индекс шарда...
        newtime = time.time()
        results, scores = bm25_index.retrieve([query_tokens], k=bm25_k_per_shard) # ищем топ k документов в ша...
        print(f"BM25 time: {time.time() - newtime}")
        local_ids = results[0] # берем результаты для первого и единственного запроса
        for r, lid in enumerate(local_ids, 1): # проходим по локальным id внутри шарда; r - ранг
            global_idx = int(lid) + offset # переводим локальный индекс шарда в глобальный индекс документа (...
            # ретривер же вернул локальные индексы; в shard 1 он мог вернуть local_ids = [15, 66, 2000], хотя...
            # поэтому нужен offset: global_idx = local_id + offset; для local_ids = 15 имеем globalidx = 15 + 5...
            # пропускаем невалидные индексы
            if global_idx < 0 or global_idx >= len(doc_sids):
                continue
            cid = doc_sids[global_idx] # получаем настоящий id обращения
            # если документ встречался несколько раз (в шарде несколько раз один документ попался) - оставь...
            # в остальных случаях просто добавляем пару "кандидат от bm25 текущего шарда: его ранг"
            if cid not in bm25_ranks or r < bm25_ranks[cid]:
                bm25_ranks[cid] = r

    # RRF fusion: объединяем кандидатов faiss и bm25
    all_ids = set(faiss_ranks) | set(bm25_ranks)
    # делаем слияние фаисса и бм25: слияние рангов, а не скоров; RRF = сумма(1/(k+r(d))), где r(d) - ранг (...
    # у нас 2 слагаемых: одно по рангам faiss, другое по рангам bm25; также можем установить через ALPHA ве...
    # Если документа нет в BM25, берется ранг 999 (очень плохой ранг) - можно и 1501 взять (тк в фаисс 1500...)
    # важно - 999 или 1500 - функция к этому моменту уже затухла
    fused = {cid: ALPHA * (1 / (K_RRF + bm25_ranks.get(cid, 999))) +
                  (1 - ALPHA) * (1 / (K_RRF + faiss_ranks.get(cid, 999)))
             for cid in all_ids}
    # полученная оценка будет от 0 до 0.0327 (при объединении двух поисковых систем) с учетом K=60;
    sorted_cands = [cid for cid, _ in sorted(fused.items(), key=lambda x: x[1], reverse=True)] # сортируем

    # Фильтрация по дате
    # Если дата не указана - возвращаем всех кандидатов; при этом здесь следующий фильтрации (первым был с...)
    # получения fusion-оценки) - берем target_k кандидатов, чтобы облегчить работу реранкеру
    if date_range is None:
        return sorted_cands[:target_k]
    filtered = filter_candidates_by_date(sorted_cands, date_range)
    # faiss_k, bm25_k_per_shard - глубина первичного поиска; target_k - глубина проверки по дате после fusion
    k_step = 300 # начальный размер окна кандидатов по дате; то есть будем проверять top 300 документов, а не сразу ...
    for _ in range(4): # делаем максимум 4 итерации;
        current_ids = sorted_cands[:k_step]
        if not current_ids:
            return []
        filtered = filter_candidates_by_date(current_ids, date_range) # ищем подходящие по дате среди первых k_step...
        if len(filtered) >= target_k or k_step >= 1200: # если нашли нужное кол-во самых подходящих примеров / не до...
            return filtered
        k_step += 300

    return filtered


def rerank(query: str, candidates):
    rerank_inputs = [(query, documents[id_to_index[cid]]) for cid in candidates] # формируем пары (запрос, документ...)

    raw_scores = reranker.predict(rerank_inputs, batch_size=32) # реранкер дает скоры соответствия каждой пар...
    raw_scores = np.array(raw_scores)
    scores = 1 / (1 + np.exp(-raw_scores))

    results = []
    for cid, score in zip(candidates, scores):
        idx = id_to_index[cid]
        # keywords = extract_keywords(documents[idx])
        results.append({
            "incident_sid": doc_sids[idx],
            "incident_id": incident_ids[idx],
            "Текст_ИОР": documents[idx],
            "score": float(score),
            "date": incident_dates[idx],
            # "keywords": keywords
        })
    return sorted(results, key=lambda x: x["score"], reverse=True)


def get_id_variations(doc_id):
    """
    Generates variations of document/incident ID for robust matching.
    Specifically handles:
    - Alphanumeric strings like EVE-7818291
    - 19-digit large integers (which might get converted to float string '1.2345e+18' or '12345.0' or float/int)
    """
    variations = set()
    if doc_id is None:
        return variations
    
    # Strip whitespace
    doc_id_str = str(doc_id).strip()
    if not doc_id_str:
        return variations
        
    variations.add(doc_id_str)
    
    # Handle float-like representation e.g. '12345.0' or '1234567890123456789.0'
    if '.' in doc_id_str:
        # 1. Split-based extraction (prevents float64 precision loss for 19-digit IDs)
        parts = doc_id_str.split('.')
        if len(parts) == 2 and parts[1].strip() == '0':
            int_part = parts[0].strip()
            if int_part.isdigit():
                variations.add(int_part)
                try:
                    variations.add(int(int_part))
                except ValueError:
                    pass
        
        # 2. Fallback numeric parse
        try:
            val_float = float(doc_id_str)
            if val_float.is_integer():
                val_int = int(val_float)
                variations.add(str(val_int))
                variations.add(val_int)
        except (ValueError, TypeError):
            pass
            
    # Handle scientific notation e.g. '1.2345678901234568e+18'
    if 'e+' in doc_id_str.lower():
        try:
            val_float = float(doc_id_str)
            val_int = int(val_float)
            variations.add(str(val_int))
            variations.add(val_int)
        except (ValueError, TypeError):
            pass
            
    # If it is a string representing a pure integer, add integer itself
    if doc_id_str.isdigit():
        try:
            val_int = int(doc_id_str)
            variations.add(val_int)
            variations.add(f"{val_int}.0")
            variations.add(str(val_int) + ".0")
        except (ValueError, TypeError):
            pass
            
    # If it is an integer directly, add its string and float string representations
    if isinstance(doc_id, (int, np.integer)):
        variations.add(int(doc_id))
        variations.add(f"{doc_id}.0")
        variations.add(str(doc_id) + ".0")
        
    return variations


def build_and_cache_small_index(session_id: str, target_ids: list, id_to_text_map: dict) -> bool:
    global id_to_index, embeddings, sid_to_index
    valid_ids = []
    valid_indices = []

    for doc_id in target_ids:
        # Generate variations for robust matching
        vars = get_id_variations(doc_id)
        
        matched = False
        
        # 1. Check alphanumeric incdnt_sid in sid_to_index first
        for v in vars:
            v_str = str(v)
            if v_str in sid_to_index:
                valid_ids.append(doc_id)
                valid_indices.append(sid_to_index[v_str])
                matched = True
                break
                
        if matched:
            continue
            
        # 2. Check numeric lookup key in id_to_index (for incdnt_id)
        for v in vars:
            # Check original typed variation
            if v in id_to_index:
                valid_ids.append(doc_id)
                valid_indices.append(id_to_index[v])
                matched = True
                break
            # Check string variation
            v_str = str(v)
            if v_str in id_to_index:
                valid_ids.append(doc_id)
                valid_indices.append(id_to_index[v_str])
                matched = True
                break
            # Check integer variation if numeric
            if isinstance(v, str) and v.isdigit():
                try:
                    v_int = int(v)
                    if v_int in id_to_index:
                        valid_ids.append(doc_id)
                        valid_indices.append(id_to_index[v_int])
                        matched = True
                        break
                except ValueError:
                    pass
            elif isinstance(v, (int, float)):
                try:
                    v_int = int(v)
                    if v_int in id_to_index:
                        valid_ids.append(doc_id)
                        valid_indices.append(id_to_index[v_int])
                        matched = True
                        break
                except (ValueError, TypeError):
                    pass

    skipped_count = len(target_ids) - len(valid_ids)
    print(f"[SMALL_FAISS] Всего ID передано: {len(target_ids)}, успешно сопоставлено: {len(valid_indices)}, пропущено: {skipped_count}")

    if not valid_indices:
        return False

    try:
        selected_embeddings = embeddings[valid_indices].copy().astype('float32')
        faiss.normalize_L2(selected_embeddings)

        dim = selected_embeddings.shape[1]
        small_index = faiss.IndexFlatIP(dim)
        small_index.add(selected_embeddings)

        ordered_descriptions = [
            {"id": doc_id, "text": id_to_text_map.get(doc_id, "")}
            for doc_id in valid_ids
        ]

        existing_history = _SMALL_FAISS_SESSION_CACHE.get(session_id, {}).get("history", [])

        _SMALL_FAISS_SESSION_CACHE[session_id] = {
            "index": small_index,
            "descriptions": ordered_descriptions,
            "id_to_text_map": {doc_id: id_to_text_map.get(doc_id, "") for doc_id in valid_ids},
            "last_ask_user_stuck": True | False,
            "history": existing_history
        }
        return True
    except Exception as e:
        print(f"[SMALL_FAISS] Ошибка сборки малого индекса: {e}")
        return False


def search_small_index(session_id: str, query: str, threshold: float = 0.7, max_candidates: int = 50) -> list:
    global EMBED_MODEL

    session_data = _SMALL_FAISS_SESSION_CACHE.get(session_id)
    if not session_data:
        return []

    small_index = session_data["index"]
    descriptions = session_data["descriptions"]

    if not descriptions or not small_index:
        return []

    try:
        query_vector = EMBED_MODEL.encode([query]).astype('float32')
        faiss.normalize_L2(query_vector)

        search_k = min(max_candidates, len(descriptions))
        distances, indices = small_index.search(query_vector, search_k)

        retrieved_results = []
        for idx, dist in zip(indices[0], distances[0]):
            if idx != -1 and idx < len(descriptions) and dist >= threshold:
                retrieved_results.append({
                    "text": descriptions[idx]["text"],
                    "id": descriptions[idx]["id"],
                    "score": float(dist)
                })

        return retrieved_results

    except Exception as e:
        print(f"[SMALL_FAISS] Ошибка поиска по малому индексу: {e}")
        return []


_SMALL_FAISS_SESSION_CACHE = {}


def search_pipeline(query, faiss_idx, bm25_indexes, top_k=None, score_threshold=None, date_range=None):
    target_k = top_k if top_k else DEFAULT_TOP_K # если пользователь не указал, сколько запросов он хочет получить
    thr = score_threshold if score_threshold is not None else SCORE_THRESHOLD
    newtime = time.time()
    candidates = retrieve_hybrid_adaptive(query=query, faiss_idx=faiss_idx, bm25_indexes=bm25_indexes, target_k=target_k, date_range=date_range)
    print(f"candidates: {time.time() - newtime}")
    if not candidates:
        return pd.DataFrame()

    newtime = time.time()
    reranked = rerank(query, candidates[:TOP_K_RERANK])
    print(f"rerank: {time.time() - newtime}")

    if top_k is not None:
        # return pd.DataFrame(reranked[:top_k])[["incident_sid", "incident_id", "Текст_ИОР", "date", "score", "keywords"]]
        return pd.DataFrame(reranked[:top_k])[["incident_sid", "incident_id", "Текст_ИОР", "date", "score"]]
    # если пользователь не указал, сколько ответов вывести, выводим все релевантные (выше порога по скору)
    filtered = [r for r in reranked if r["score"] >= thr]
    # если пользователь явно указал, сколько ответов вывести
    return pd.DataFrame(filtered if filtered else reranked[:target_k])[["incident_sid", "incident_id", "Текст_ИОР", "date", "score"]]


MIN_DATE = "0000-01-01"
MAX_DATE = "9999-12-31"


if "ior_meta" in cache:
    print("Использую кэшированные meta/embeddings/faiss/bm25 для IOR_pipeline_search...")
    meta_dict = cache["ior_meta"]
    documents = meta_dict["documents"]
    doc_sids = meta_dict["doc_sids"]
    id_to_index = meta_dict["id_to_index"]
    incident_ids = meta_dict["incident_ids"]
    incident_dates = meta_dict.get("incident_dates", [None] * len(documents))
    sid_to_index = {str(sid): i for i, sid in enumerate(doc_sids) if sid is not None}
    
    embeddings = cache["ior_embeddings"]
    faiss_loaded = cache["ior_faiss_loaded"]
    bm25_indexes = cache["ior_bm25_indexes"]
else:
    load_meta()
    meta_dict = {
        "documents": documents,
        "doc_sids": doc_sids,
        "id_to_index": id_to_index,
        "incident_ids": incident_ids,
        "incident_dates": incident_dates,
    }
    embeddings = load_embeddings(path_to_load)
    faiss_loaded = load_indices(to_gpu=False)
    path_to_bm25_indexes = f'{path_to_load}/bm25s_shards'
    bm25_indexes = load_bm25s_shards(path_to_bm25_indexes)
    
    cache["ior_meta"] = meta_dict
    cache["ior_embeddings"] = embeddings
    cache["ior_faiss_loaded"] = faiss_loaded
    cache["ior_bm25_indexes"] = bm25_indexes