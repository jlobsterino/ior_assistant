"""
Tools package. Импорт этого модуля авто-регистрирует все tool в REGISTRY
через side-effect (каждый модуль вызывает REGISTRY.register() при импорте).

Использование:
    from backend.agent.tools.registry import REGISTRY
    catalog = REGISTRY.llm_catalog_compact()
"""
from backend.agent.tools.registry import REGISTRY

# Side-effect: импорт модулей регистрирует tools
from backend.agent.tools import run_preset
from backend.agent.tools import dataframe_ops
from backend.agent.tools import introspect
from backend.agent.tools import query_spec_tool

__all__ = ["REGISTRY"]