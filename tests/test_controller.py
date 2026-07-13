"""B.3 – офлайн-тесты ReAct-контроллера: route_path, протокол действия, цикл run_agent_v2.

Цикл прогоняется со СКРИПТОВАННЫМ FakeLLM + фейковым реестром (DI) – без боевых
get_llm/REGISTRY/store. Проверяем: happy-path (run_query_spec→final), все три
термирования (dup / invalid-JSON / no-progress не нужен тут), гард пустого export,
и period-без-года → ask_user.

Запуск: PYTHONPATH=. python3 tests/test_controller.py
"""
import asyncio

import pandas as pd

from backend.agent.controller import route_path, run_agent_v2
from backend.agent.toolvalidate import (canon_args, compile_action,
                                          parse_controller_json, validate_tool_call)
from backend.agent.schema import get_schema

SCHEMA = get_schema()

# —— фейки ——
class _TR:
    def __init__(self, ok=True, output=None, error=None, summary=""):
        self.ok, self.output, self.error, self.summary = ok, output, error, summary


class FakeTool:
    def __init__(self, required=None):
        self.args_schema = {"required": required or []}


class FakeRegistry:
    def __init__(self, results=None):
        self._tools = {
            "query": FakeTool(["table"]),
            "search_values": FakeTool(["query"]),
            "export_excel": FakeTool(["df_id"]),
            "run_query_spec": FakeTool(["spec"]),
        }
        self._results = results or {}

    def names(self):
        return list(self._tools)

    def get(self, n):
        return self._tools.get(n)

    def llm_catalog_compact(self):
        return ", ".join(self._tools)

    async def execute(self, name, args, ctx):
        r = self._results.get(name)
        return r if r is not None else _TR(ok=True, output={}, summary=f"{name} ok")


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.i = 0

    def invoke(self, messages, temperature=None):
        r = self.responses[self.i] if self.i < len(self.responses) else self.responses[-1]
        self.i += 1
        return r


class FakeState:
    def __init__(self):
        self.dataframes = {}
        self._n = 0

    def register_dataframe(self, df, description="", created_by="", df_id=None):
        if df_id is None:
            self._n += 1
            df_id = f"df_{self._n}"
        self.dataframes[df_id] = df
        import types
        return types.SimpleNamespace(df_id=df_id)

    def get_df(self, df_id):
        return self.dataframes[df_id]


def _act(action, **args):
    import json
    return json.dumps({"thought": "t", "action": action, "args": args}, ensure_ascii=False)


def _run(user_msg, responses, registry=None, state=None):
    state = state or FakeState()
    reg = registry or FakeRegistry()
    return asyncio.run(run_agent_v2(state=state, user_msg=user_msg, emit=None,
                                      llm=FakeLLM(responses), registry=reg)), state

# —— route_path ——
def test_route_money_agg_to_spec():
    assert route_path("выгрузи прямые потери по процессам") == "spec_required"
    assert route_path("сумма возмещений сгруппировать по ТБ") == "spec_required"


def test_route_single_slice():
    assert route_path("выгрузи ИОР за 2025 по СЗБ") == "run_query"


def test_route_free():
    assert route_path("что ты умеешь") == "free"


# —— протокол действия ——
def test_compile_action_unwraps_tool_key():
    a, e = compile_action({"tool": "search_values", "args": {"query": "x"}}, {"search_values"})
    assert e is None and a.action == "search_values" and a.args["query"] == "x"


def test_compile_action_unknown_is_observation_not_crash():
    a, e = compile_action({"action": "get_data", "args": {}}, {"query"})
    assert a is None and e and "get_data" in e and "Доступны" in e


def test_compile_action_spread_args():
    a, e = compile_action({"action": "query", "table": "t", "limit": 5}, {"query"})
    assert e is None and a.args.get("table") == "t" and a.args.get("limit") == 5


def test_validate_missing_required():
    errs = validate_tool_call("export_excel", {}, SCHEMA, {"export_excel": {"required": ["df_id"]}})
    assert any("df_id" in x for x in errs)


def test_validate_bad_table():
    errs = validate_tool_call("query", {"table": "несуществует"}, SCHEMA)
    assert any("несуществует" in x for x in errs)


def test_canon_args_normalizes_numbers():
    assert canon_args({"n": 100}) == canon_args({"n": 100.0})
    assert canon_args({"a": 1, "b": 2}) == canon_args({"b": 2, "a": 1})  # порядок ключей


def test_parse_controller_json_strips_fence():
    assert parse_controller_json('```json\n{"action":"final","args":{}}\n```')["action"] == "final"


# —— цикл run_agent_v2 ——
def test_happy_path_spec_autoterminates_on_file():
    # успешный run_query_spec с file_id → АВТО-завершение (не ждём, пока модель
    # пришлёт final – слабая модель зацикливается). FakeLLM шлёт run_query_spec
    # многократно, но первый же файл завершает turn.
    reg = FakeRegistry(results={"run_query_spec": _TR(
        ok=True, output={"df_id": "df_1", "file_id": "file_1", "rows": 2}, summary="готово")})
    res, _ = _run("выгрузи потери по процессам",
                  [_act("run_query_spec", spec={"version": 1, "source": {"table": "d6_base_of_knowledge_ior"}})],
                  registry=reg)
    assert res.ok
    assert len(res.files) == 1 and res.files[0]["file_id"] == "file_1"


def test_dup_action_terminates():
    res, _ = _run("выгрузи ИОР", [_act("search_values", query="тб")])  # один ответ → повторяется
    assert res.ok is False and "повтор" in (res.ask_user or "").lower()


def test_invalid_json_terminates():
    res, _ = _run("выгрузи ИОР", ["это не json", "снова мусор", "и ещё"])
    assert res.ask_user and "разобрать" in res.ask_user.lower()


def test_empty_export_blocked_then_final():
    state = FakeState()
    state.register_dataframe(pd.DataFrame(), df_id="df_empty")  # пустой df
    res, _ = _run("выгрузи", [_act("export_excel", df_id="df_empty"),
                               _act("final", text="готово")], state=state)
    assert res.ok and res.final_text == "готово"
    assert len(res.files) == 0   # пустой df НЕ выгружен
    assert any(o.action == "export_guard" and not o.ok for o in res.history)


def test_period_without_year_asks_clarification():
    res, _ = _run("выгрузи инциденты за январь", [_act("final", text="x")])
    assert res.ask_user and "год" in res.ask_user.lower()


def test_autoexport_when_final_without_file():
    # модель сделала query (df в state), но завершила без export → авто-экспорт даёт файл
    state = FakeState()
    state.register_dataframe(pd.DataFrame({"a": [1, 2]}), df_id="df_1")
    reg = FakeRegistry(results={"export_excel": _TR(
        ok=True, output={"file_id": "file_9", "name": "x.xlsx", "rows": 2}, summary="файл")})
    res, _ = _run("срез по периоду", [_act("final", text="готово")], registry=reg, state=state)
    assert res.ok
    assert any(f.get("file_id") == "file_9" for f in res.files), res.files


def test_grounding_disambiguation():
    import backend.agent.controller as ctrl
    old_ground_query = ctrl.ground_query
    
    mock_hits = [
        {"phrase": "риски", "column": "funct_block_lvl_3_name", "value": "Блок Риски", "count": 100, "score": 0.9},
        {"phrase": "риски", "column": "org_struct_lvl_3_name", "value": "Блок Риски", "count": 200, "score": 0.9},
        {"phrase": "риски", "column": "proc_lvl_2_name", "value": "Управление рисками", "count": 300, "score": 0.9}
    ]
    ctrl.ground_query = lambda query, *args, **kwargs: mock_hits if "риски" in query else []
    
    try:
        state = FakeState()
        # 1. Первый ход: запрос с неоднозначной фразой "риски"
        # Ожидаем, что контроллер сразу вернет уточняющий вопрос (ask_user)
        res, _ = _run("инциденты по блоку риски", [], state=state)
        assert res.ok
        assert res.ask_user is not None
        assert "разрезах" in res.ask_user
        assert hasattr(state, "ambiguity_pending")
        assert state.ambiguity_pending is not None
        assert state.ambiguity_pending["phrase"] == "риски"
        
        # 2. Второй ход: ответ пользователя (например, "по процессу")
        # Ожидаем, что состояние очистится и запустится стандартный ReAct-цикл (FakeLLM вернет final)
        res2, _ = _run("по процессу", [_act("final", text="отчет готов")], state=state)
        assert res2.ok
        assert state.ambiguity_pending is None
        assert res2.final_text == "отчет готов"
    finally:
        ctrl.ground_query = old_ground_query


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f"  ok  {fn.__name__}"); passed += 1
        except Exception:
            print(f"FAIL  {fn.__name__}"); traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    raise SystemExit(0 if passed == len(fns) else 1)
