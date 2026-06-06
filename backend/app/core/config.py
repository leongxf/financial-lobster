from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "financial-lobster"
    environment: str = "local"
    log_level: str = "INFO"

    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""

    local_storage_dir: str = "storage/uploads"
    task_storage_dir: str = "storage/tasks"
    analysis_cache_dir: str = "storage/cache"

    llm_provider: str = "qwen"
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: str = ""
    llm_app_id: str = ""
    llm_model: str = "qwen-plus"
    llm_timeout_ms: int = 180_000
    llm_max_tokens: int = 4000
    llm_temperature: float = 0.2
    llm_chunk_chars: int = 18_000
    llm_max_chunks: int = 12
    prompt_version: str = "material_financial_summary:v1"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
