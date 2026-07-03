from __future__ import annotations

from datetime import date
from typing import Optional

from backend.agent.tools.base import Tool, ToolResult
from backend.agent.tools.registry import REGISTRY


async def run_query_spec(ctx, spec: Optional[dict] = None, **kwargs) -> ToolResult:
    """Скомпилировать QuerySpec IR в выгрузку. ctx = SessionState."""
    from backend.agent.query_spec import CompileContext, compile_query_spec
    from backend.agent.schema import get_schema

    if spec is None:
        # LLM иногда кладёт IR на верхний уровень args вместо args.spec
        spec = kwargs or {}

    cctx = CompileContext(ctx=ctx, emit=getattr(ctx, "emit", None),
                          schema=get_schema(), now=date.today())
    res = await compile_query_spec(cctx, spec)
    if not res.ok:
        return ToolResult(ok=False, error=res.error)
    
    out = {
        "df_id": res.df_id,
        "file_id": res.file_id,
        "spec_resolved": res.spec_resolved,
        "lineage": res.lineage,
        "funnel": res.funnel,
        "warnings": res.warnings,
    }
    summary = (f"QuerySpec -> {res.file_id or res.df_id}"
               + (f" ({len(res.warnings)} warnings)" if res.warnings else ""))
    return ToolResult(ok=True, output=out, summary=summary)


REGISTRY.register(Tool(
    name="run_query_spec",
    description=(
        "Скомпилировать декларативный QuerySpec (JSON-IR одной выгрузки) в файл. "
        "ИСПОЛЬЗУЙ для табличных выгрузок с join/агрегатами/деньгами/окнами — "
        "детерминированный компилятор сам строит lineage (query->pre_aggregate->"
        "join->aggregate->derived->window->sort->export). Деньги — ТОЛЬКО через join "
        "к fin_impact/recovery (суммы main заполнены ~2.26%). Период — intent "
        "(filters[kind=period]), границы считает компилятор."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "spec": {"type": "object", "description": "QuerySpec v1 (см. §2.2)"},
        },
        "required": ["spec"],
    },
    returns="{df_id, file_id, spec_resolved, lineage}",
    run=run_query_spec,
    category="query",
))