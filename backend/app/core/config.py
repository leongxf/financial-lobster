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
    # 飞书事件去重标记目录：按 message_id 记录已处理事件，避免重推导致重复分析。
    event_dedup_dir: str = "storage/events"

    # 上传门禁：限制单文件大小与可接受的文件类型，下载前先拦截，避免浪费带宽/磁盘。
    max_file_size_mb: int = 20
    allowed_file_extensions: str = ".pdf,.docx,.csv,.xlsx"

    llm_provider: str = "qwen"
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen-plus"
    # 备用模型列表（逗号分隔，同账号）：主模型额度耗尽时按序切换，让缓存跨模型保持命中。
    # 留空 = 关闭 fallback，额度耗尽直接报错（切到付费固定模型后清空即可）。
    llm_fallback_models: str = ""
    llm_timeout_ms: int = 180_000
    llm_max_tokens: int = 4000
    llm_temperature: float = 0.2
    # 部分模型（如 glm-5）仅支持 stream=true；开启后 chat 走 SSE 流式并拼成完整回复。
    llm_stream: bool = False
    llm_chunk_chars: int = 18_000
    # Map 阶段单分片 LLM 失败时的重试次数（仅 rate_limit/server/传输层错误）。
    llm_map_chunk_retries: int = 3
    # 分析页数上限：绑定约束，超出则截断并在报告中显式提示，避免静默丢页。
    llm_max_pages: int = 200
    # 分层归并时每组合并的分片笔记数；笔记超过该数则先分组归并再终合，避免一次性 reduce 爆上下文。
    llm_reduce_group_size: int = 8
    # 单次归并/合成调用喂给模型的笔记字符预算上限：分组时除条数外再按此预算封顶，
    # 防止 group_size 段笔记拼起来超出模型输入长度上限（如 dashscope 30720）。
    llm_reduce_max_chars: int = 24_000
    # map 阶段并发调用数（qwen 接口有速率限制，默认保守）。
    llm_map_concurrency: int = 4
    # 防跑飞硬上限：页数才是真正的限制，这里给一个高位兜底。
    llm_max_chunks: int = 200
    prompt_version: str = "material_financial_summary:v1"

    # 追问问答（多轮对话）相关配置。
    conversation_storage_dir: str = "storage/conversations"
    qa_recent_files_max: int = 5  # 每个用户最多记住最近 N 个文件，超出按 LRU 淘汰。
    qa_retrieve_top_k: int = 5  # 文件内检索时取相关度最高的前 K 个片段。
    qa_context_max_chars: int = 24_000  # 单次喂给模型的检索片段字符预算上限。
    qa_history_max_turns: int = 5  # 单文件追问携带的最近对话轮数上限。
    qa_prompt_version: str = "material_qa:v1"
    # 用户会话记忆（ConversationStore）保留天数：按 last_active_at 滑动过期，到期自动清理。
    user_memory_ttl_days: int = 5
    user_memory_cleanup_enabled: bool = True
    user_memory_cleanup_interval_hours: int = 6  # worker 后台 sweep 间隔（小时）。
    # 飞书机器人自定义菜单 event_key：点击后发确认卡，用户确认后清理该用户会话记忆。
    clear_memory_menu_event_key: str = "clear_memory"

    # 向量检索（embedding）相关配置。中英混排材料靠多语言 embedding 做语义检索。
    # embedding 可独立于 chat 走另一平台：以下两项留空则复用 llm_base_url/llm_api_key（同账号），
    # 填写则 embedding 请求改用该平台（仍需 OpenAI 兼容 /embeddings）。chat 不受影响。
    # 用途：chat 平台额度耗尽或想把向量算力分流到免费/独立账号时，只改这两项 + 模型名即可。
    qa_embedding_base_url: str = ""
    qa_embedding_api_key: str = ""
    qa_embedding_model: str = "text-embedding-v3"  # dashscope 多语言向量模型。
    # 备用 embedding 模型（逗号分隔，同账号）：入库时主模型额度耗尽则整文件改用下一个重算，
    # 并把实际所用模型记入缓存；查询时强制用该文件入库时的模型，避免跨模型向量空间错配。
    # 留空 = 关闭。注意：换 embedding 模型只影响新入库文件，旧缓存仍用各自记录的模型查询。
    qa_embedding_fallback_models: str = ""
    qa_embedding_chunk_chars: int = 700  # 切块字符数：检索粒度，小于整页以提升命中精度。
    qa_embedding_chunk_overlap: int = 120  # 相邻块重叠字符，避免答案被切在块边界。
    qa_embedding_batch_size: int = 10  # 单次 embedding 请求的最大文本条数（接口上限）。
    qa_embedding_cache_dir: str = "storage/embeddings"  # 向量缓存目录，按 file_hash 命名。

    # 联网检索（web search）独立配置：此前原型偷用 qa_embedding_api_key，现拆开。
    search_provider: str = "zhipu"
    search_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    search_api_key: str = ""
    search_engine: str = "search_std"
    search_max_sources: int = 8
    search_queries_per_section: int = 3

    # 行业研究模板目录：扫描该目录下的 *.docx 作为可选模板；填充配置为同目录下 research_fill_config.json。
    research_template_dir: str = "storage/templates"

    # 会话状态 / 卡片回调去重
    session_storage_dir: str = "storage/sessions"
    card_event_dedup_dir: str = "storage/card_events"

    # 技能开关（关闭则不出现在能力卡 / 拒绝触发）
    enable_industry_research: bool = True
    enable_template_rewrite: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def allowed_extensions(self) -> set[str]:
        return {
            ext.strip().lower()
            for ext in self.allowed_file_extensions.split(",")
            if ext.strip()
        }

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]

    @property
    def embedding_base_url(self) -> str:
        """embedding 请求的 base_url：未单独配置则复用 chat 的 llm_base_url。"""
        return self.qa_embedding_base_url.strip() or self.llm_base_url

    @property
    def embedding_api_key(self) -> str:
        """embedding 请求的 api_key：未单独配置则复用 chat 的 llm_api_key。"""
        return self.qa_embedding_api_key.strip() or self.llm_api_key

    @property
    def embedding_model_chain(self) -> list[str]:
        """入库时尝试的 embedding 模型顺序：主模型在前，备用依次在后（去重）。"""
        chain = [self.qa_embedding_model]
        for model in self.qa_embedding_fallback_models.split(","):
            model = model.strip()
            if model and model not in chain:
                chain.append(model)
        return chain

    @property
    def search_key(self) -> str:
        """web search 的 key：未单独配置则回退到此前偷用的 qa_embedding_api_key。"""
        return self.search_api_key.strip() or self.qa_embedding_api_key.strip()

    @property
    def search_endpoint(self) -> str:
        return (
            self.search_base_url.strip()
            or self.qa_embedding_base_url.strip()
            or "https://open.bigmodel.cn/api/paas/v4"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
