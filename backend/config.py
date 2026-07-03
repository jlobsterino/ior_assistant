"""
ИОР-помощник — конфигурация через Pydantic Settings.

Два режима:
  APP_ENV=local — для разработчика на PC: mock-runner, без Spark, без GigaChat
                  (опционально подключается если задан JPY_API_TOKEN).
  APP_ENV=prod  — для корпоративной среды: реальный Papermill + Spark + GigaChat.

Автодетект:
 • Если APP_ENV=local        -> mock_notebook_execution=True по умолчанию.
 • Если APP_ENV=prod         -> mock_notebook_execution=False.
 • Ручной override через .env: MOCK_NOTEBOOK_EXECUTION=true/false.
"""

from __future__ import annotations
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # — App environment —
    app_env: Literal["local", "prod"] = "local"

    # — GigaChat —
    gigachat_api_url: Optional[str] = None
    jpy_api_token: Optional[str] = None
    gigachat_model: str = "GigaChat-3-Ultra"
    gigachat_delay_sec: float = 6.0
    gigachat_temperature: float = 0.01
    gigachat_verify_ssl: bool = False

    # — App —
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_log_level: str = "INFO"

    # — Storage —
    db_url: str = "sqlite+aiosqlite:///data/sessions.db"
    files_dir: str = "data/generated_files"

    # — Knowledge Base —
    kb_dir: str = "knowledge_base"
    kb_scripts_dir: str = "knowledge_base/scripts"
    kb_notebooks_dir: str = "knowledge_base/notebooks"
    kb_mapping_file: str = "knowledge_base/mapping/ИОР_Mapping_разделы.md"
    kb_hot_reload: bool = True

    # — Execution —
    notebook_timeout_sec: int = 600
    excel_file_ttl_days: int = 30

    # — Mock vs real (override через env) —
    mock_notebook_execution: Optional[bool] = None

    # — Spark (только для prod) —
    # В DataLab Spark поднимается ЛОКАЛЬНО в контейнере пользователя
    # (local[*]), а не submit'ится на YARN. SPARK_HOME/sys.path больше
    # не нужны — pyspark ставится обычным pip install.
    spark_master: str = "local[*]"
    spark_app_name: str = "ior-assistant"
    spark_driver_memory: str = "8g"
    spark_executor_memory: str = "8g"

    # — Features —
    feature_hot_reload: bool = True

    # — Paths (computed) —
    @property
    def base_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @property
    def kb_scripts_path(self) -> Path:
        return self.base_dir / self.kb_scripts_dir

    @property
    def kb_notebooks_path(self) -> Path:
        return self.base_dir / self.kb_notebooks_dir

    @property
    def kb_mapping_path(self) -> Path:
        return self.base_dir / self.kb_mapping_file

    @property
    def files_path(self) -> Path:
        return self.base_dir / self.files_dir

    @property
    def db_path(self) -> Path:
        if self.db_url.startswith("sqlite"):
            return self.base_dir / self.db_url.split("///")[-1]
        return self.base_dir / "data" / "sessions.db"

    # — Computed: реальный mock-флаг с учётом env —
    @property
    def use_mock_runner(self) -> bool:
        """Принимает решение: mock или real Papermill."""
        if self.mock_notebook_execution is not None:
            return self.mock_notebook_execution
        return self.app_env == "local"

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def gigachat_available(self) -> bool:
        api = self.gigachat_api_url or os.environ.get("GIGACHAT_API_URL")
        tok = self.jpy_api_token or os.environ.get("JPY_API_TOKEN")
        return bool(api and tok)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()