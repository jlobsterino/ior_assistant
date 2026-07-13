"""
controller - итеративный ReAct-агент run_agent_v2 (§3.3) + детерминированный
decision-gate route_path (§3.5-bis). «Видеть данные -> действовать», взамен слепого
одношагового make_plan.

ОФЛАЙН-БЕЗОПАСНЫЙ модуль: на уровне модуля - только stdlib + offline-safe
(ground_query, parse_period, get_schema, toolvalidate, query_spec.is_empty_df).
get_llm()/REGISTRY - ЛЕНИВО внутри функций; для тестов внедряются (DI: llm/registry).

Латентность критична (прод = GigaChat ~6с/вызов, быстрого хостеда нет - см. §3.1);
route_path уводит join/деньги/агрегат в run_query_spec = 1 вызов модели + детерм.
компиляция, а не 6-8 atomic-итераций.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from backend.agent.resolve.grounding import ground_query
from backend.agent.resolve.period_parser import parse_period
from backend.agent.schema import get_schema
from backend import understand_summary, humanize_action
from backend.agent.toolvalidate import (
    Action,
    canon_args,
    compile_action,
    parse_controller_json,
    validate_tool_call,
)

logger = logging.getLogger(__name__)

# Системный промпт контроллера (§3.5). Живёт ЗДЕСЬ, а не в prompts.py: prompts.py
# импортирует tools на уровне модуля (pydantic-небезопасно офлайн), а контроллер
# должен оставаться офлайн-тестируемым.
SYSTEM_CONTROLLER = """Ты — итеративный агент-аналитик аудитора Сбера по базе знаний инцидентов
операционного риска (ИОР). Работаешь ПОШАГОВО: одно действие -> наблюдение ->
следующее действие. НЕ планируй весь путь заранее.

формат ответа — СТРОГО один JSON-объект, без markdown:
{"thought": "что наблюдал и почему этот шаг", "action": "имя действия", "args": {...}}

ПРАВИЛА:
• Перед фильтром по категориальной колонке, если не уверен в колонке — сначала
  search_values/distinct_values (граунд по реальным данным; не угадывай уровень оргструктуры).
• ВЫГРУЗКА В ФАЙЛ И ПРЕСЕТЫ:
  - ПРИОРИТЕТ ГОТОВЫХ СКРИПТОВ (ПРЕСЕТОВ / SKILLS):
    Если запрос пользователя совпадает по смыслу с одним из готовых скриптов/отчетов, ты ОБЯЗАН запустить именно его через run_preset с соответствующим skill_id:
    * Для гипотез, трендов, аномалий, динамики за период (общих или по банкам) -> run_preset с skill_id='ior_hypothesis_v2'.
    * Для возмещений, возвратов, компенсаций или страхования за период -> run_preset с skill_id='vozmeshenie_ior_v2'.
    * Для финансовых последствий, потерь или ущерба за период -> run_preset с skill_id='financial_consequences_ior_v2'.
    * Для нефинансовых (качественных) последствий, жалоб, ущерба репутации за период -> run_preset с skill_id='ior_nonfinancial_consequences_v2'.
    * Для удаленных инцидентов / причин удаления -> run_preset с skill_id='deleted_ior_v2'.
    * Для задолженности / невозможности взыскания по кредитам -> run_preset с skill_id='credit_no_way_collect_debt_v2'.
    * Для детального досье/отчета по конкретному инциденту по его ID (например, EVE-123456) -> run_preset с skill_id='report_period_specific_ior_v2'.
    * Для общих выгрузок инцидентов ИОР за период по ПАО Сбербанк (без сужения темы) -> run_preset с skill_id='ior_period_pao_sberbank_v2'.
  - Если ни один готовый пресет из списка выше не подходит под запрос пользователя (например, требуется сложный JOIN, специфическая группировка или кастомная фильтрация по полям, не входящим в стандартные темы), только тогда используй run_query_spec или run_query.
  - Для досье конкретного инцидента -> get_ior_details или run_preset(skill_id='report_period_specific_ior_v2').
• ВНИМАНИЕ: Если в запросе есть даты (периоды) или деньги (потери/возмещения), для кастомных запросов используй run_query_spec (инструмент query ЗАПРЕЩЕН). Но если есть подходящий типовой отчет, всегда выбирай run_preset.
• ПО УМОЛЧАНИЮ ВСЕГДА ДЕЛАЙ ДЕТАЛЬНУЮ ВЫГРУЗКУ (все строки, без aggregate/group_by), чтобы сохранить детальный список инцидентов для последующих вопросов. Делай группировку/агрегацию только если пользователь прямо попросил об этом (например, «в разрезе...», «сгруппируй по...», «суммарно по...»).
• Даты НЕ считай сам — передавай НАМЕРЕНИЕ (period intent), границы посчитает компилятор.
• Деньги — ТОЛЬКО через join к fin_impact/recovery (суммы main заполнены ~2.26%):
  related-таблицы many-per-incident -> агрегируй sum по incdnt_id (pre_aggregate) ПЕРЕД join.
• ТИП ПОТЕРИ ОБЯЗАТЕЛЕН в pre_aggregate.filter, иначе посчитаются ВСЕ потери (ошибка!):
  «прямые потери» -> filter {"fin_impact_type_name":{"eq":"Прямая потеря"}};
  «косвенные» -> "Косвенная потеря"; «нереализовавшиеся» -> "Нереализовавшаяся потеря";
  «третьих лиц» -> "Потеря третьих лиц". Без явного «все потери» — ВСЕГДА ставь фильтр типа.
• Если действие вернуло EMPTY_RESULT: НЕ повторяй тот же фильтр и НЕ иди в export.
  Есть found_in_column/corrections -> повтори с верной колонкой/значением. «Реально пусто» -> ask_user.
• Непонятный/недоступный запрос (нет данных в БД) -> ask_user, а не выдумывай.
• Когда файл готов и вопрос закрыт — action: "final" с коротким текстом-резюме.

ФОРМА run_query_spec — СТРОГО так (source, table ОБЯЗАТЕЛЕН; period.intent — ОБЪЕКТ
{"text":...}, НЕ строка; per-incident джойн-алиас и агрегат-алиас — РАЗНЫЕ имена;
деньги related — many-per-incident, агрегируй pre_aggregate sum по incdnt_id ДО join).
Пример для «ИОР за Q1 2026 по ВВ банку, прямые потери >1млн, по процессам, возмещения, чистая потеря»:
{"thought":"join fin_impact+recovery, агрегат по процессу, чистая потеря","action":"run_query_spec","args":{"spec":{
  "version":1,
  "source":{"table":"d6_base_of_knowledge_ior","joins":[
    {"table":"d6_base_of_knowledge_incident_fin_impact","on":"incdnt_id","how":"left","pre_aggregate":{"group_by":["incdnt_id"],"filters":[{"kind":"categorical","column":"fin_impact_type_name","op":"eq","value":"Прямая потеря"}]}},
    {"table":"d6_base_of_knowledge_incident_recovery","on":"incdnt_id","how":"left","pre_aggregate":{"group_by":["incdnt_id"]}}],
  "filters":[
    {"kind":"period","intent":{"text":"Q1 2026"},"column":"incdnt_entry_dt","required":true},
    {"kind":"categorical","column":"org_struct_lvl_3_name","op":"eq","value":"Волго-Вятский банк","grounded":true},
    {"kind":"range","column":"direct_loss","op":"gt","value":1000000}],
  "aggregate":{"group_by":["process_lvl_4_name"],"metrics":[
    {"as":"direct_loss_sum","source":"direct_loss","fn":"sum"},
    {"as":"recovery_sum","source":"recovery","fn":"sum"},
    {"as":"cnt","source":"incdnt_id","fn":"count"}]},
  "derived_metrics":[{"as":"net_loss","expr":"{op:\\"sub\\",left:\\"direct_loss_sum\\",right:\\"recovery_sum\\"}"}],
  "sort":[{"by":"net_loss","desc":true}],
  "select":["process_lvl_4_name","cnt","direct_loss_sum","recovery_sum","net_loss"],
  "output":{"format":"excel","name":"ИОР Q1 2026 ВВБ по процессам"}}}
Значение категориального бери из секции ЗАЗЕМЛЕНИЕ (там реальная колонка/значение).
"""

# — бюджета/термирование (§3.3/§3.7) —
MAX_ITERS = 12
DUP_LIMIT = 2
DUP_WINDOW = 6         # сигнатура в окне последних DUP_WINDOW -> стоп
INVALID_LIMIT = 3      # подряд-невалидных JSON -> ask_user/fail
NO_PROGRESS_LIMIT = 4  # итераций без нового df/файла -> ask_user


@dataclass
class Observation:
    action: str
    ok: bool
    text: str = ""
    payload: dict = field(default_factory=dict)  # summary (ok) либо error/диагностика (fail)

    def to_line(self, idx: int) -> str:
        head = "ok" if self.ok else "FAIL"
        extra = ""
        if self.payload:
            extra = " | " + ", ".join(f"{k}={v}" for k, v in self.payload.items() if v is not None)
        return f"[it{idx}] {self.action} ({head}): {self.text}{extra}"


@dataclass
class AgentTurnResult:
    ok: bool
    final_text: Optional[str] = None      # turn завершился штатно
    ask_user: Optional[str] = None        # текст ответа (action=final)
    stuck: bool = False                   # вопрос пользователю (clarification)
    files: list = field(default_factory=list)
    history: list = field(default_factory=list)
    # result-пакет последней выгрузки (для UI: методология/воронка/превью/правка)
    spec_resolved: Optional[dict] = None
    funnel: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    df_id: Optional[str] = None


# ----- decision-gate route_path (§3.5-bis) ---------------------------------
#
# Эвристики СТРУКТУРНЫЕ (агрегат/деньги/связь), не словарь VALUE-синонимов: какие
# реальные колонки/таблицы задействованы (грауд) + структура запроса. Имена ТБ и
# прочие значения резолвит грауд/LLM, не этот роутер.

_RELATED_PREFIX = ("fin_impact", "recovery", "nonfin", "stts_chng", "recovery_")
_MONEY_RE = re.compile(
    r"(?:"
    r"потер|возмещен|ущерб|убыт|fin_impact|recovery|денежн|штраф|прибыл|сумм\w*|финансов|последств|\$+"
    r"|(?::потер|возмещ|ущерб|убыт)"
    r")",
    re.IGNORECASE
)
_AGG_RE = re.compile(
    r"группир|сгруппир|в\s+разрез|помесячн|поквартальн|по\s+годам|"
    r"топ[-\s]?\d*|крупнейш|суммарн|итог\w*|количеств|сколько\b|средн\w*|"
    r"\bс\s+каждым|по\s+процесс|по\s+тб\b|по\s+банк|распредел",
    re.IGNORECASE
)
_SLICE_RE = re.compile(r"выгруз|список|вывед|покажи\s+иор|инцидент", re.IGNORECASE)


def _grounded_related(grounding) -> bool:
    return any(str(h.get("column", "")).startswith(_RELATED_PREFIX) for h in (grounding or []))


def _money_intent(grounding, low: str) -> bool:
    return bool(_MONEY_RE.search(low)) or _grounded_related(grounding)


def _agg_intent(low: str) -> bool:
    return bool(_AGG_RE.search(low))


def _single_slice(low: str) -> bool:
    return bool(_SLICE_RE.search(low))


def _mentions_month_or_quarter(text: str) -> bool:
    low = (text or "").lower()
    months = ("январ", "феврал", "март", "апрел", "ма[йя]", "июн", "июл", "август",
              "сентябр", "октябр", "ноябр", "декабр")
    return (bool(re.search(r"кварт|\bq[1-4]\b|[1-4]\s*кв\b", low))
            or any(re.search(m, low) for m in months))


def route_path(user_msg: str, grounding=None, period=None) -> str:
    """'spec_required' | 'run_query' | 'free' – детерминированная подсказка пути.
    
    join/деньги/агрегат -> spec_required (ОБЯЗАН run_query_spec: 1 вызов модели +
    детерм. компиляция, гарды денег/fan-out гарантированы). Одиночный срез ИОР ->
    run_query. Иначе free (контроллер выбирает: досье/atomic/ask_user).
    """
    low = (user_msg or "").lower()
    if _money_intent(grounding, low) or _grounded_related(grounding) or _agg_intent(low):
        return "spec_required"
    if _single_slice(low):
        return "run_query"
    return "free"


# ----- гард пустого df перед экспорт (§3.8, дублирует тул-гард – ранний рубеж) -----

def empty_export_guard(df_id, state) -> Optional[str]:
    from backend.agent.query_spec import is_empty_df  # offline-safe
    if not df_id:
        return None
    df = state.get_df(df_id) if df_id in getattr(state, "dataframes", {}) else None
    if is_empty_df(df):
        return ("EMPTY_RESULT: Финальный df пуст – нечего выгружать. Проверь гард-порог/"
                "категориальное значение/период, либо честно сообщи пользователю (ask_user).")
    return None


# ----- контекст итерации (§3.4) -------------------------------------------

def _period_label(period) -> Optional[str]:
    return getattr(period, "label", None) if period is not None else None


def build_controller_context(user_msg, state, grounding, period_label,
                             history, route_hint, registry) -> str:
    parts = []
    names = sorted(set(registry.names()) | {"run_query_spec", "run_query", "final", "ask_user"})
    parts.append("# ДОСТУПНЫЕ ДЕЙСТВИЯ: " + ", ".join(names))
    parts.append(f"\n# ПОДСКАЗКА ПУТИ (детерминированная): {route_hint}")
    if route_hint == "spec_required":
        parts.append("-> Для ЭТОГО запроса используй run_preset (если есть подходящий готовый отчет/скрипт) или run_query_spec (для кастомного запроса). Голый join_dfs/"
                     "group_by по money-колонке main здесь запрещён (тул-гард вернёт ошибку).")
    if grounding:
        parts.append("\n# ЗАЗЕМЛЕНИЕ (реальные колонки/значения витрины – бери ОТСЮДА):")
        for h in grounding[:10]:
            parts.append(f" * '{h['phrase']}' -> {h['column']} = '{h['value']}' ({h['count']} строк)")
    if period_label:
        parts.append(f"\n# ПЕРИОД (посчитан детерминированно): {period_label}")
    last_spec = getattr(state, "last_spec", None)
    if last_spec:
        try:
            parts.append(
                "\n# ПРОШЛАЯ ВЫГРУЗКА (спец). Если пользователь просит ПОПРАВИТЬ/ДОБАВИТЬ/"
                "ИНАЧЕ/ТОП-N/ДРУГОЙ ПЕРИОД – верни run_query_spec с ИЗМЕНЁННЫМ этим спецом...\n"
                "НЕ собирай с нуля:\n" + json.dumps(last_spec, ensure_ascii=False)[:1000]
            )
        except Exception:  # noqa: BLE001
            pass
    snap = state.llm_snapshot() if hasattr(state, "llm_snapshot") else {}
    if snap.get("dataframes"):
        parts.append("\n# df ИЗ ПРОШЛЫХ ШАГОВ/turn'ов:")
        for d in snap["dataframes"]:
            parts.append(f" * {d.get('df_id')}: {d.get('desc', '')} ({d.get('rows', '?')} rows)")
    if history:
        parts.append("\n# ИСТОРИЯ ШАГОВ ЭТОГО turn'а:")
        for i, o in enumerate(history[-12:]):
            parts.append(" " + o.to_line(i))
    parts.append(f"\n# ЗАПРОС ПОЛЬЗОВАТЕЛЯ:\n{user_msg}")
    parts.append('\nВерни СТРОГО {"thought":"...", "action":"...", "args":{...}}. ОДНО ДЕЙСТВИЕ.')
    return "\n".join(parts)


def to_observation(act: Action, res) -> Observation:
    ok = bool(getattr(res, "ok", False))
    if ok:
        out = getattr(res, "output", None) or {}
        payload = {k: out.get(k) for k in ("df_id", "file_id", "rows")
                   if isinstance(out, dict) and k in out}
        return Observation(act.action, True, getattr(res, "summary", "") or "", payload)
    return Observation(act.action, False, getattr(res, "error", "") or "ошибка", {})


def _collect_files(res, files: list) -> None:
    out = getattr(res, "output", None) or {}
    if isinstance(out, dict) and out.get("file_id"):
        files.append({"file_id": out.get("file_id"), "name": out.get("name"),
                      "rows": out.get("rows")})


def _arg_schemas(registry) -> dict:
    out = {}
    for n in registry.names():
        t = registry.get(n) if hasattr(registry, "get") else None
        if t is not None and getattr(t, "args_schema", None):
            out[n] = t.args_schema
    return out


async def _emit(emit, ev: str, payload: dict) -> None:
    if emit is None:
        return
    try:
        r = emit(ev, payload)
        if hasattr(r, "__await__"):
            await r
    except Exception:  # noqa: BLE001
        pass


async def _ensure_file(state, files, registry, emit) -> None:
    """Гарантия файла-выгрузки: если turn завершается, а файла НЕТ, но в state есть
    данные (модель сделала query/filter, но забыла export) ~ авто-экспортим последний
    df. Для пользователя выгрузка = файл; пустой df отсечёт гард в export_excel.
    """
    if files:
        return
    dfs = getattr(state, "dataframes", {})
    if not dfs:
        return
    last_df_id = list(dfs.keys())[-1]
    try:
        res = await registry.execute("export_excel", {"df_id": last_df_id}, state)
    except Exception:  # noqa: BLE001
        return
    if getattr(res, "ok", False):
        await _emit(emit, "step_done", {"step_id": "auto_export", "tool": "export_excel",
                                        "summary": getattr(res, "summary", "")})
        _collect_files(res, files)


# ----- главный цикл run_agent_v2 (§3.3) -----------------------------------

async def run_agent_v2(*, state, user_msg, emit=None, llm=None, registry=None,
                       max_iters: int = MAX_ITERS, clarify_strikes: int = 0) -> AgentTurnResult:
    """ReAct-цикл «наблюдай+действуй». clarify_strikes: int = 0 -> AgentTurnResult:
    по умолчанию – боевые get_llm(role='controller')/REGISTRY (ленивые импорты)."""
    if registry is None:
        from backend.agent.tools import REGISTRY as registry  # noqa: N806
    if llm is None:
        from backend.core.llm import get_llm
        llm = get_llm()

    schema = get_schema()
    
    # --- Шаг проверки и снятия неоднозначности заземления (Grounding Disambiguation) ---
    ambiguity_pending = getattr(state, "ambiguity_pending", None)
    if ambiguity_pending:
        choice = user_msg.lower()
        options = ambiguity_pending["options"]
        selected_opt = None
        
        # 1. Поиск по цифре
        num_match = re.search(r"\b(\d+)\b", choice)
        if num_match:
            idx = int(num_match.group(1)) - 1
            if 0 <= idx < len(options):
                selected_opt = options[idx]
                
        # 2. Поиск по имени/описанию колонки или значению
        if not selected_opt:
            for opt in options:
                desc = opt["description"].lower()
                col = opt["column"].lower()
                val = opt["value"].lower()
                if col in choice or desc in choice or val in choice:
                    selected_opt = opt
                    break
                    
        # 3. Поиск по вхождениям ключевых корней
        if not selected_opt:
            if "процесс" in choice:
                for opt in options:
                    if "proc" in opt["column"].lower():
                        selected_opt = opt
                        break
            elif "функциональн" in choice or "функ" in choice:
                for opt in options:
                    if "funct" in opt["column"].lower():
                        selected_opt = opt
                        break
            elif "орг" in choice or "структур" in choice or "тб" in choice:
                for opt in options:
                    if "org" in opt["column"].lower() or "tb" in opt["column"].lower():
                        selected_opt = opt
                        break
                        
        # 4. Fallback через локальный Qwen
        if not selected_opt:
            try:
                from local_qwen import ask_local_qwen
                qwen_prompt = f"Пользователь выбрал вариант из списка в ответ на вопрос. Ответ пользователя: '{user_msg}'.\nВарианты:\n"
                for i, opt in enumerate(options):
                    qwen_prompt += f"{i+1}. {opt['description']} (колонка {opt['column']})\n"
                qwen_prompt += f"\nВыведи строго одну цифру номера выбранного варианта (от 1 до {len(options)}). Ничего другого не пиши."
                
                qwen_res = ask_local_qwen([{"role": "user", "content": qwen_prompt}], max_tokens=10)
                match = re.search(r"\b(\d+)\b", qwen_res)
                if match:
                    idx = int(match.group(1)) - 1
                    if 0 <= idx < len(options):
                        selected_opt = options[idx]
            except Exception:
                pass
                
        if not selected_opt:
            selected_opt = options[0]
            
        original_query = ambiguity_pending["original_query"]
        phrase_to_resolve = ambiguity_pending["phrase"].lower()
        
        # Пересчитываем заземление для исходного запроса
        grounding = ground_query(original_query)
        filtered_grounding = []
        for h in grounding:
            if h["phrase"].lower() == phrase_to_resolve:
                if h["column"] == selected_opt["column"] and h["value"] == selected_opt["value"]:
                    filtered_grounding.append(h)
            else:
                filtered_grounding.append(h)
        grounding = filtered_grounding
        user_msg = original_query
        state.ambiguity_pending = None
        period = parse_period(user_msg)
        route_hint = route_path(user_msg, grounding, period)
    else:
        # Обычное заземление
        grounding = ground_query(user_msg)
        period = parse_period(user_msg)
        route_hint = route_path(user_msg, grounding, period)
        
        # Поиск новых неоднозначностей
        low_msg = user_msg.lower()
        by_phrase = {}
        for h in grounding:
            col = h["column"]
            if col in ("incdnt_sum", "recovery"):
                continue
            col_desc = col
            for table in schema.tables.values():
                for c in table.columns:
                    if c.name == col:
                        col_desc = c.description or col
                        break
            h_with_desc = dict(h)
            h_with_desc["description"] = col_desc
            by_phrase.setdefault(h["phrase"].lower(), []).append(h_with_desc)
            
        ambiguous_phrase = None
        ambiguous_options = []
        for phrase, opts in by_phrase.items():
            unique_cols = set(opt["column"] for opt in opts)
            if len(unique_cols) > 1:
                # Пробуем снять неоднозначность по ключевым словам в запросе
                resolved_opts = []
                if "процесс" in low_msg or "продукт" in low_msg:
                    resolved_opts = [opt for opt in opts if "proc" in opt["column"].lower()]
                elif "функциональн" in low_msg or "функ" in low_msg:
                    resolved_opts = [opt for opt in opts if "funct" in opt["column"].lower()]
                elif "оргструктур" in low_msg or "орг.структур" in low_msg or "территориальн" in low_msg or "тб" in low_msg:
                    resolved_opts = [opt for opt in opts if "org" in opt["column"].lower() or "tb" in opt["column"].lower()]
                    
                if len(resolved_opts) == 1:
                    new_grounding = []
                    for h in grounding:
                        if h["phrase"].lower() == phrase:
                            if h["column"] == resolved_opts[0]["column"] and h["value"] == resolved_opts[0]["value"]:
                                new_grounding.append(h)
                        else:
                            new_grounding.append(h)
                    grounding = new_grounding
                    continue
                    
                ambiguous_phrase = phrase
                ambiguous_options = opts
                break
                
        if ambiguous_phrase:
            state.ambiguity_pending = {
                "original_query": user_msg,
                "phrase": ambiguous_phrase,
                "options": ambiguous_options
            }
            q = f"Вы указали '{ambiguous_phrase}', но это значение встречается в нескольких разрезах:\n"
            for i, opt in enumerate(ambiguous_options):
                q += f"{i+1}. **{opt['description']}** (значение: '{opt['value']}')\n"
            q += "По какому из них выполнить фильтрацию? Пожалуйста, выберите номер или напишите название разреза."
            
            # Стримим уточнение
            history = []
            files = []
            await _emit(emit, "clarification", {"question": q})
            return AgentTurnResult(ok=True, ask_user=q, stuck=False, files=files, history=history)

    # прокидываем emit в компилятор (run_query_spec -> CompileContext.emit), чтобы
    # его под-шаги (загрузка, join, агрегат) уходили в премиальную ленту статусов.
    try:
        state.emit = emit
        state.cancelled = False  # сбрасываем флаг отмены прошлого turn'а (П6)
        state.current_period = period
    except Exception:  # noqa: BLE001
        pass

    async def activity(aid, kind, title, detail=None, status="active"):
        await _emit(emit, "activity", {"id": aid, "kind": kind, "title": title,
                                       "detail": detail, "status": status})

    # ранний сигнал «что понял» – ещё до первого вызова модели
    await activity("understand", "understand", "Разобрал запрос",
                   understand_summary(grounding, _period_label(period), route_hint), "done")

    # период без явного года, но месяц/квартал упомянут -> НЕ молчим (инв. 1+3)
    if period is None and _mentions_month_or_quarter(user_msg):
        q = "За какой год? В запросе есть период (месяц/квартал), но без года."
        await _emit(emit, "clarification", {"question": q})
        return AgentTurnResult(ok=True, ask_user=q, stuck=False)

    valid_tools = set(registry.names())
    arg_schemas = _arg_schemas(registry)
    sys = _build_system(schema, registry)

    history: list[Observation] = []
    files: list = []
    pkg = {"spec_resolved": None, "funnel": [], "warnings": [], "df_id": None}
    sig_window = collections.deque(maxlen=DUP_WINDOW)
    invalid_streak = 0
    iters_since_progress = 0
    last_count = 0

    for it in range(max_iters):
        if getattr(state, "cancelled", False):  # пользователь нажал Stop (П6)
            await activity("think", "thinking", "Остановлено", None, "done")
            return AgentTurnResult(ok=False, ask_user="Остановлено пользователем.",
                                   files=files, history=history, **pkg)
        # «думаю» перед каждым вызовом модели – закрываем паузу GigaChat (~6c)
        await activity("think", "thinking", "Думаю над следующим шагом", None, "active")
        ctx = build_controller_context(user_msg, state, grounding,
                                       _period_label(period), history, route_hint, registry)
        import uuid
        salt = f"\n\n[Session: {getattr(state, 'session_id', 'default_session')}, Request ID: {uuid.uuid4().hex[:8]}, Iteration: {it}]"
        raw = await asyncio.to_thread(
            llm.invoke, [{"role": "system", "content": sys + salt},
                         {"role": "user", "content": ctx}]
        )
        act, err = compile_action(parse_controller_json(raw), valid_tools)
        logger.info(f"[DEBUG_ACT] iter={it} action={act.action if act else None} err={err}")
        if err:
            invalid_streak += 1
            if invalid_streak >= INVALID_LIMIT:
                q = "Не удаётся разобрать ответ модели – переформулируй запрос."
                await _emit(emit, "clarification", {"question": q})
                return AgentTurnResult(ok=True, ask_user=q, stuck=True, files=files, history=history)
            history.append(Observation("{invalid}", False, err))
            continue
        invalid_streak = 0

        if act.action == "final":
            await activity("think", "thinking", "Готово", None, "done")
            await activity("answer", "answer", "Готовлю ответ", None, "active")
            # модель может завершить, забыв export – гарантируем файл из последнего df
            await _ensure_file(state, files, registry, emit)
            return AgentTurnResult(ok=True, final_text=act.args.get("text", ""),
                                   files=files, history=history, **pkg)

        if act.action == "ask_user":
            q = act.args.get("question", "Уточни запрос.")
            if clarify_strikes >= 1:
                return AgentTurnResult(ok=False, ask_user=None, stuck=True, files=files, history=history)
            await activity("think", "thinking", "Нужно уточнение", None, "done")
            await _emit(emit, "clarification", {"question": q})
            return AgentTurnResult(ok=True, ask_user=q, stuck=False, files=files, history=history)

        sig = (act.action, canon_args(act.args))
        if sig_window.count(sig) >= DUP_LIMIT:
            return AgentTurnResult(ok=False, ask_user="Действие повторяется без прогресса...",
                                   stuck=True, files=files, history=history)
        sig_window.append(sig)

        cur_count = len(getattr(state, "dataframes", {})) + len(files)
        is_search = act.action in ("search_values", "distinct_values")
        iters_since_progress = 0 if (cur_count > last_count or is_search) else iters_since_progress + 1
        last_count = cur_count
        if iters_since_progress >= NO_PROGRESS_LIMIT:
            q = "Не получается продвинуться – уточни запрос."
            if clarify_strikes >= 1:
                return AgentTurnResult(ok=False, ask_user=None, stuck=True, files=files, history=history)
            await _emit(emit, "clarification", {"question": q})
            return AgentTurnResult(ok=True, ask_user=q, stuck=True, files=files, history=history)

        verrs = validate_tool_call(act.action, act.args, schema, arg_schemas)
        if verrs:
            history.append(Observation(act.action, False, "; ".join(verrs)))
            continue

        if act.action in ("export_excel", "export_csv"):
            gerr = empty_export_guard(act.args.get("df_id"), state)
            if gerr:
                history.append(Observation("export_guard", False, gerr))
                continue

        # человеческий статус действия (детерминированный, не из «мыслей» модели)
        a_title, a_detail = humanize_action(act.action, act.args)
        await activity("think", "thinking", "Выбрал действие", None, "done")
        aid = f"act:{it}"
        await activity(aid, "action", a_title, a_detail, "active")

        if act.action == "run_query_spec":
            res = await registry.execute(
                "run_query_spec", {"spec": act.args.get("spec", act.args)}, state
            )
        else:
            res = await registry.execute(act.action, act.args, state)

        history.append(to_observation(act, res))
        if getattr(res, "ok", False):
            await activity(aid, "action", a_title, a_detail, "done")
            _collect_files(res, files)
            out = getattr(res, "output", None) or {}
            if isinstance(out, dict) and out.get("spec_resolved"):
                pkg = {"spec_resolved": out.get("spec_resolved"),
                       "funnel": out.get("funnel") or [],
                       "warnings": out.get("warnings") or [],
                       "df_id": out.get("df_id")}
                try:  # для follow-up: следующий turn правит этот спец, а не собирает заново
                    state.last_spec = out.get("spec_resolved")
                except Exception:  # noqa: BLE001
                    pass
            # АВТО-ЗАВЕРШЕНИЕ: создан файл (run_query_spec/export) -> выгрузка готова.
            # Слабая модель (GigaChat) иначе зацикливается, перевыгружая по 4 раза.
            if isinstance(out, dict) and out.get("file_id"):
                tool_text = out.get("text") or out.get("narrative")
                return AgentTurnResult(ok=True, final_text=tool_text, files=files,
                                       history=history, **pkg)
        else:
            await activity(aid, "action", a_title,
                           "не получилось, пробую иначе", "failed")

    # бюджет исчерпан – но если данные собраны, всё равно отдадим файл
    await _ensure_file(state, files, registry, emit)
    if files:
        return AgentTurnResult(ok=True, final_text=None, files=files,
                               history=history, **pkg)
    return AgentTurnResult(ok=False, ask_user="Не уложился в бюджет шагов - сузь запрос.",
                           files=files, history=history)


def _build_system(schema, registry) -> str:
    """Статичный системный промпт (схема+каталог собираются ОДИН раз вне цикла - §3.6)"""
    try:
        catalog = registry.llm_catalog_compact()
    except Exception:  # noqa: BLE001
        catalog = ", ".join(registry.names())
    return (SYSTEM_CONTROLLER + "\n\n# СХЕМА БД ИОР:\n" + schema.to_llm_snippet()
            + "\n\n# КАТАЛОГ ДЕЙСТВИЙ:\n" + catalog)