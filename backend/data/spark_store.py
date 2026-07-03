"""
SparkHiveStore - production backend, ходит в Hive metastore через PySpark.

Lazy singleton SparkSession, кладётся в `~/.spark-local-dir`, local[2],
1g memory (см. notebook'и для тех же настроек). Spark в этом storage'е
живёт всё время жизни сервера – JVM не пересоздаётся.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import pandas as pd

from backend.agent.schema import get_schema
from backend.data.base import build_where_clauses

logger = logging.getLogger(__name__)


class SparkHiveStore:
    DM_PREFIX = "arnsdpsbx_t_team_sva_oarb_4."

    def __init__(self) -> None:
        self._spark = None
        self._schema = get_schema()

    def _get_spark(self):
        if self._spark is not None and getattr(self._spark.sparkContext, "_jsc", None) is not None:
            return self._spark

        # Импорт внутри – pyspark может быть не установлен в чистом Local
        _SPARK_TMP = os.path.expanduser("~/.spark-local-dir")
        os.makedirs(_SPARK_TMP, exist_ok=True)
        os.environ["SPARK_LOCAL_DIRS"] = _SPARK_TMP

        from pyspark import SparkConf
        from pyspark.sql import SparkSession

        conf = SparkConf().setAppName("ior_agent_store")
        conf.setAll([
            ("spark.ui.enabled", "true"),
            ("spark.master", "local[2]"),
            ("spark.executor.cores", "2"),
            ("spark.executor.memory", "1g"),
            ("spark.driver.memory", "1g"),
            ("spark.driver.maxResultSize", "1g"),
            ("spark.port.maxRetries", "100"),
            ("spark.local.dir", _SPARK_TMP),
        ])
        self._spark = (
            SparkSession.builder.config(conf=conf)
            .enableHiveSupport()
            .getOrCreate()
        )
        logger.info("[SparkHiveStore] Spark ready (singleton, local[2])")
        return self._spark

    def list_tables(self) -> list[str]:
        return self._schema.table_names()

    def get_table_schema(self, table: str) -> dict:
        t = self._schema.get(table)
        if t is None:
            return {}
        return {c.name: c.type for c in t.columns}

    def describe_table(self, table: str) -> list[tuple[str, str]]:
        """Список (имя_колонки, тип) через DESCRIBE из Hive metastore.
        Для schema-кроулера. table – full_name (с DM-префиксом) либо короткое."""
        full = table if "." in table else f"{self.DM_PREFIX}{table}"
        spark = self._get_spark()
        rows = spark.sql(f"DESCRIBE {full}").collect()
        out = []
        for r in rows:
            col = r["col_name"]
            # DESCRIBE возвращает пустые строки + секцию '# Partition' в конце
            if not col or col.startswith("#"):
                break
            out.append((col, str(r["data_type"]).lower()))
        return out

    def list_db_tables(self, prefix: str = "d6_") -> list[str]:
        """Реальные таблицы схемы из Hive metastore с префиксом."""
        spark = self._get_spark()
        db = self.DM_PREFIX.rstrip(".")
        rows = spark.sql(f"SHOW TABLES IN {db}").collect()
        return [r["tableName"] for r in rows if r["tableName"].startswith(prefix)]

    def query(
        self,
        *,
        table: str,
        where: Optional[dict] = None,
        columns: Optional[list[str]] = None,
        order_by: Optional[str] = None,
        order_desc: bool = True,
        limit: int = 100_000,
    ) -> pd.DataFrame:
        t = self._schema.get(table)
        if t is None:
            raise ValueError(f"таблица {table!r} нет в schema")
        col_types = {c.name: c.type for c in t.columns}
        valid = set(col_types)

        # Validate columns
        if columns:
            bad = [c for c in columns if c not in valid]
            if bad:
                raise ValueError(f"колонки {bad} нет в {table}")

        # LLM иногда передаёт "col DESC" одной строкой – split + override order_desc
        if order_by is not None:
            if not isinstance(order_by, str):
                raise ValueError(
                    f"order_by должен быть string (имя колонки), "
                    f"получили {type(order_by).__name__}={order_by!r}"
                )
            parts = order_by.strip().split()
            if len(parts) == 2 and parts[1].upper() in ("DESC", "ASC"):
                order_by = parts[0]
                order_desc = parts[1].upper() == "DESC"
            if order_by not in valid:
                raise ValueError(f"order_by: колонки {order_by!r} нет в {table}")

        try:
            limit = min(
                int(limit) if limit is not None else 100_000, 2_000_000
            )
        except (TypeError, ValueError):
            limit = 100_000

        select_cols = ", ".join(columns) if columns else "*"
        clauses = build_where_clauses(where, col_types)
        where_sql = " AND ".join(clauses) if clauses else "1=1"
        order_sql = ""
        if order_by:
            order_sql = f" ORDER BY {order_by} {'DESC' if order_desc else 'ASC'}"

        sql = (
            f"SELECT {select_cols} FROM {t.full_name} "
            f"WHERE {where_sql}{order_sql} LIMIT {limit}"
        )
        logger.info("[SparkHiveStore] %s", sql)
        spark = self._get_spark()
        df = spark.sql(sql).toPandas()
        try:
            df.columns = [str(c).lower() for c in df.columns]
        except Exception as e:
            logger.warning("[SparkHiveStore] Failed to normalize columns: %s", e)
        return df

    def fetch_distinct_values(
        self,
        *,
        table: str,
        column: str,
        max_values: int = 50,
    ) -> list[str]:
        """SELECT DISTINCT column FROM table LIMIT max_values – реальные
        значения из Hive БД. Используется для динамического enum schema."""
        t = self._schema.get(table)
        if t is None:
            return []
        if column not in {c.name for c in t.columns}:
            return []
        sql = (
            f"SELECT DISTINCT {column} FROM {t.full_name} "
            f"WHERE {column} IS NOT NULL "
            f"ORDER BY {column} LIMIT {int(max_values)}"
        )
        spark = self._get_spark()
        rows = spark.sql(sql).collect()
        return [str(r[0]) for r in rows if r[0] is not None]

    def close(self) -> None:
        # Намеренно НЕ стопим – singleton живёт всю сессию сервера
        pass