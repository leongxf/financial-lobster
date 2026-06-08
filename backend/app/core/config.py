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


    # 额外推送：任何人给机器人发消息时，主动单聊推一条提醒给管理员（你）。
    # receive_id 留空则关闭该推送，不影响原有回复逻辑。
    feishu_admin_receive_id: str = ""
    feishu_admin_receive_id_type: str = "open_id"
    
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

    # 追问问答（多轮对话）相关配置。
    conversation_storage_dir: str = "storage/conversations"
    qa_recent_files_max: int = 5  # 每个用户最多记住最近 N 个文件，超出按 LRU 淘汰。
    qa_retrieve_top_k: int = 5  # 文件内按页检索时取相关度最高的前 K 页。
    qa_context_max_chars: int = 24_000  # 单次喂给模型的检索片段字符预算上限。
    qa_history_max_turns: int = 5  # 单文件追问携带的最近对话轮数上限。
    qa_prompt_version: str = "material_qa:v1"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
