import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from app.logger import AppLogger

logger = AppLogger.get(__name__)
root_dir = Path(os.path.dirname(__file__)).parent

class Settings(BaseSettings):
    # ============ Telegram ============
    telegram_bot_token: str = Field(
        default="",
        description="Telegram Bot Token"
    )
    review_chat_id: int = Field(
        default=0,
        description="审查频道 Chat ID（待审新闻推送到此频道人工处理，通过后进公开频道；为 0 时退回 review_admin_ids 私聊推送）"
    )
    review_admin_ids: list[int] = Field(
        default=[],
        description="审查管理员 telegram_id 白名单（审查卡片私聊推送对象，Q4）"
    )
    public_channel_id: int = Field(
        default=0,
        description="公开频道 Chat ID（审查通过后才转发到这里，Q4）"
    )
    review_max_age_days: int = Field(
        default=30,
        description="送审时效窗口（天）：只有 discovered_at 在此窗口内的新闻才会推送审查；超窗的会被标记 review_status='expired' 收尾，不再重复参与送审"
    )

    # ============ Database ============
    database_url: str = Field(
        default="postgresql://dev:dev123@localhost:5432/ai_projects",
        description="Database Connection URL"
    )

    # ============ GitHub ============
    github_token: str = Field(
        default="",
        description="GitHub Personal Token"
    )

    hugging_face_token:str = Field(
        default="",
        description="Hugging Face Token"
    )

    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis Connection URL"
    )

    health_api_key: str = Field(
        default="",
        description="/health /watchdog 接口鉴权 key（X-API-Key 头）；留空则不鉴权（仅限本机调试）"
    )

    # ============ Logging ============
    log_level: str = Field(
        default="INFO",
        description="Logging Level"
    )

    # ============ Bot ============
    bot_name: str = Field(
        default="",
        description="Bot Name"
    )
    bot_version: str = Field(
        default="",
        description="Bot Version"
    )

    # ============ Ollama ============
    ollama_model: str = Field(
        default="llama3.1:8b",
        description="Ollama Model Name"
    )
    ollama_url: str = Field(
        default="http://localhost:11434",
        description="Ollama Url"
    )

    # ============ LLM Providers ============
    # OpenRouter
    openrouter_api_key: str = Field(
        default="",
        description="OpenRouter API Key"
    )
    openrouter_model: str = Field(
        default="anthropic/claude-3.5-sonnet",
        description="OpenRouter Model"
    )

    # OpenAI (also any OpenAI-compatible endpoint via openai_base_url)
    openai_api_key: str = Field(
        default="",
        description="OpenAI API Key (also used by the opencode provider)"
    )
    openai_model: str = Field(
        default="gpt-4o-mini",
        description="OpenAI Model"
    )
    openai_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="OpenAI-compatible base URL (Chat Completions). Override to target any compatible server."
    )

    # OpenCode (https://github.com/sst/opencode) — speaks the OpenAI-compatible protocol
    opencode_base_url: str = Field(
        default="http://localhost:2424/v1",
        description="OpenCode local server base URL (OpenAI-compatible). Default local endpoint."
    )
    opencode_model: str = Field(
        default="",
        description="OpenCode model id (defaults to openai_model if empty)"
    )

    openwebui_model:str = Field(
        default="",
        description="Openwebui model id"
    )
    openwebui_api_key:str = Field(
        default="",
        description="Openwebui model key"
    )
    openwebui_api_url:str = Field(
        default="",
        description="Openwebui model url"
    )
    provider_fallback_order: list[str] = ["agent", "ollama"]

    # ============ News ============
    news_hn_min_score: int = Field(
        default=50,
        description="Minimum Hacker News score to collect"
    )
    news_hn_keywords: str = Field(
        default="AI,ML,LLM,GPT,transformer,neural,deep learning,generative,artificial intelligence,machine learning,large language model",
        description="Comma-separated keywords for HN filtering"
    )
    news_rss_feeds: str = Field(
        default="https://techcrunch.com/category/artificial-intelligence/feed/,https://venturebeat.com/category/ai/feed/,https://www.theverge.com/rss/ai-artificial-intelligence/index.xml,https://simonwillison.net/atom/everything/,https://www.technologyreview.com/topic/artificial-intelligence/feed,https://arstechnica.com/tag/ai/feed/,https://www.theregister.com/software/ai_ml/headlines.atom,https://www.wired.com/feed/tag/ai/latest/rss,https://the-decoder.com/feed/,https://www.marktechpost.com/feed/",
        description="Comma-separated RSS feed URLs"
    )
    news_collection_limit: int = Field(
        # 源从 5 个加到 12 个之后，RSS 那边一轮能抓到的候选量明显变多，100
        # 这个上限会把新源挤掉大半（截断只看 all_items[:limit]，不分源保底）；
        # 调到 200 给新源留出空间。这个值同时也是 hf_papers 的 max_items 上限
        # （复用同一个配置项），一并放宽。
        default=200,
        description="Max news items to collect per run"
    )
    news_dedup_enabled: bool = Field(
        default=True,
        description="Enable deduplication for news"
    )

    # ---- 新新闻源（T02-T05） ----
    product_hunt_token: str = Field(
        default="",
        description="Product Hunt GraphQL developer token (T03). Empty -> source skipped."
    )
    product_hunt_min_votes: int = Field(
        default=20,
        description="Minimum Product Hunt votes to collect (T03)"
    )
    official_blog_feeds: str = Field(
        default="https://openai.com/news/,https://www.anthropic.com/news,https://api-docs.deepseek.com/news/,https://mistral.ai/news/,https://blog.google/technology/ai/",
        description="Comma-separated official model-release blog listing page URLs (T04)"
    )
    hf_papers_limit: int = Field(
        default=50,
        description="Max HF papers to collect per run (T05)"
    )
    hf_papers_min_upvotes: int = Field(
        default=5,
        description="论文实用性筛选阈值：HF 社区点赞数达标，或论文本身带 GitHub 仓库/项目页（可直接用），二者满足其一才收录；都不满足则视为纯理论/太专业，丢弃"
    )
    hf_papers_practicality_threshold: float = Field(
        default=6.0,
        description="LLM 摘要阶段的实用性/应用性打分阈值（0-10）：低于此分的论文视为学术性过强，摘要生成后自动标记 rejected，不进入人工审查/推送队列"
    )

    # ---- LLM 排行榜真 API（T07-T10） ----
    artificial_analysis_api_key: str = Field(
        default="",
        description="Artificial Analysis free Data API key (T07). Empty -> AA source skipped."
    )
    llm_leaderboard_top_n: int = Field(
        default=30,
        description="Per-platform Top N models collected for leaderboards (T07)"
    )
    llm_leaderboard_url: str = Field(
        default="https://arena.ai/leaderboard",
        description="LMArena leaderboard page URL for embedded data extraction (T07)"
    )
    vellum_leaderboard_url: str = Field(
        default="https://www.vellum.ai/llm-leaderboard",
        description="Vellum LLM leaderboard page URL (T09)"
    )
    seal_leaderboard_url: str = Field(
        default="https://scale.com/leaderboard",
        description="Scale AI SEAL leaderboard page URL (T10)"
    )

    # ============ Watchdog / 看门狗 ============
    watchdog_enabled: bool = Field(
        default=True,
        description="是否启用看门狗定时自检"
    )
    watchdog_alert_chat_id: int = Field(
        default=0,
        description="告警接收 Chat ID（0 则回退到 review_admin_ids / review_chat_id）"
    )
    watchdog_interval_minutes: int = Field(
        default=30,
        description="看门狗自检间隔（分钟）"
    )
    watchdog_cooldown_minutes: int = Field(
        default=60,
        description="同一检查项告警冷却（分钟），避免刷屏"
    )
    batch_process_limit: int = Field(
        default=60,
        description="每轮批处理最多取多少条待处理条目（防止一轮把整个积压全排进队列）"
    )
    watchdog_pipeline_success_threshold: float = Field(
        default=80.0,
        description="管线成功率阈值（低于则告警）"
    )
    watchdog_stale_hours: int = Field(
        default=25,
        description="快照/运行记录超时阈值（小时），超过则判为 stale"
    )

    # ============ Scheduler / 开机补跑 ============
    scheduler_timezone: str = Field(
        # 默认按中文用户/中文内容源（量子位等）设为北京时间，且没有夏令时漂移；
        # 如果实际审查/使用的人在别的时区，在 .env 里覆盖 SCHEDULER_TIMEZONE 即可。
        default="Asia/Shanghai",
        description="调度时区（IANA 名）。容器内 TZ 为空跑在 UTC，不显式指定的话 "
                    "CronTrigger(hour=8) 会变成本地 9/10 点（还随夏令时飘）；"
                    "补跑判定「今天」也以这个时区为准"
    )
    catchup_enabled: bool = Field(
        default=True,
        description="是否启用开机补跑（机器关机错过 cron 时，开机后补上当天任务）"
    )
    catchup_interval_minutes: int = Field(
        default=30,
        description="补跑自检间隔（分钟）。启动时会立即先跑一次"
    )
    catchup_grace_minutes: int = Field(
        default=20,
        description="计划时刻后的宽限期（分钟）。机器开着时正常 cron 正在跑，"
                    "宽限期内不补跑，避免和它撞成两份"
    )
    catchup_max_attempts: int = Field(
        default=3,
        description="同一周期内最多尝试几次（含正常 cron 那次）。达上限后不再重试，"
                    "交给看门狗告警——避免永久性故障被无限重跑"
    )
    catchup_retry_backoff_minutes: int = Field(
        default=60,
        description="上一次尝试失败后，隔多久才允许补跑重试（分钟）"
    )

    class Config:
        env_prefix = ""
        case_sensitive = False

        env_file = root_dir / ".env" if os.getenv("APP_ENV") == "production" else root_dir / ".env.local"
        env_file_encoding = "utf-8"
        extra = "ignore"  # 忽略多余的环境变量

    @field_validator("telegram_bot_token")
    def validate_token(cls, v):
        """验证 Token 格式"""
        if not v or v == "":
            raise ValueError("TELEGRAM_BOT_TOKEN 不能为空")

        if not ":" in v:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN 格式错误，应该是 'bot_id:token_secret'"
            )

        parts = v.split(":")
        if len(parts) != 2:
            raise ValueError("TELEGRAM_BOT_TOKEN 格式错误")

        bot_id, token_secret = parts

        if not bot_id.isdigit():
            raise ValueError("Token 的 bot_id 部分应该是数字")

        if len(token_secret) < 20:
            raise ValueError("Token 的 secret 部分太短")

        return v

    def log_settings(self):
        """打印配置（隐藏敏感信息）"""
        logger.info("=" * 50)
        logger.info("⚙️ 应用配置")
        logger.info("=" * 50)
        logger.info(f"📱 Bot Token: {self.telegram_bot_token[:10]}...{self.telegram_bot_token[-10:]}")
        logger.info(f"📦 Database: {self.database_url.split('@')[1] if '@' in self.database_url else 'unknown'}")
        logger.info(f"🔴 Redis: {self.redis_url}")
        logger.info(f"📝 Log Level: {self.log_level}")
        logger.info(f"🤖 LLM Provider 回退顺序: {self.provider_fallback_order}")
        logger.info(f"🔑 OpenRouter Model: {self.openrouter_model}")
        logger.info(f"🔑 OpenAI Model: {self.openai_model}")
        logger.info(f"🔑 OpenAI Base URL: {self.openai_base_url}")
        logger.info(f"🔑 OpenCode Model: {self.opencode_model or self.openai_model}")
        logger.info(f"🔑 OpenCode Base URL: {self.opencode_base_url}")
        logger.info(f"🔑 Ollama Model: {self.ollama_model}")
        logger.info(f"🔑 Ollama URL: {self.ollama_url}")
        logger.info("=" * 50)


@lru_cache()
def get_settings() -> Settings:
    try:
        settings = Settings()
        settings.log_settings()
        return settings
    except Exception as e:
        logger.error(f"❌ 配置加载失败: {e}")
        raise


settings = get_settings()