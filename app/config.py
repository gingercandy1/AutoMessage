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
    telegram_channel_id: int = Field(
        default=0,
        description="Telegram Channel ID"
    )
    review_chat_id: int = Field(
        default=0,
        description="审查群组 Chat ID（AI 摘要先推到这里人工审查，Q4）"
    )
    public_channel_id: int = Field(
        default=0,
        description="公开频道 Chat ID（审查通过后才转发到这里，Q4）"
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
    default_llm_provider: str = Field(
        default="ollama",
        description="Default LLM provider: ollama, openrouter, openai"
    )

    # OpenRouter
    openrouter_api_key: str = Field(
        default="",
        description="OpenRouter API Key"
    )
    openrouter_model: str = Field(
        default="anthropic/claude-3.5-sonnet",
        description="OpenRouter Model"
    )

    # OpenAI
    openai_api_key: str = Field(
        default="",
        description="OpenAI API Key"
    )
    openai_model: str = Field(
        default="gpt-4o-mini",
        description="OpenAI Model"
    )

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
        default="https://hn.algolia.com/api/v1/search_by_date?tags=story&query=AI,https://techcrunch.com/category/artificial-intelligence/feed/,https://venturebeat.com/category/ai/feed/,https://www.theverge.com/ai-artificial-intelligence/rss/index.xml,https://export.arxiv.org/rss/cs.AI,https://export.arxiv.org/rss/cs.LG,https://export.arxiv.org/rss/cs.CL",
        description="Comma-separated RSS feed URLs"
    )
    news_collection_limit: int = Field(
        default=100,
        description="Max news items to collect per run"
    )
    news_dedup_enabled: bool = Field(
        default=True,
        description="Enable deduplication for news"
    )

    class Config:
        # 环境变量前缀
        env_prefix = ""
        # 大小写不敏感
        case_sensitive = False

        # .env 文件位置
        env_file = root_dir / ".env" if os.getenv("APP_ENV") == "production" else root_dir / ".env.local"
        env_file_encoding = "utf-8"
        # 其他配置
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
        logger.info(f"🤖 Default LLM Provider: {self.default_llm_provider}")
        if self.default_llm_provider == "openrouter":
            logger.info(f"🔑 OpenRouter Model: {self.openrouter_model}")
            logger.info(f"🔑 OpenRouter Key: {self.openrouter_api_key[:10]}...{self.openrouter_api_key[-4:] if self.openrouter_api_key else 'NOT SET'}")
        elif self.default_llm_provider == "openai":
            logger.info(f"🔑 OpenAI Model: {self.openai_model}")
            logger.info(f"🔑 OpenAI Key: {self.openai_api_key[:10]}...{self.openai_api_key[-4:] if self.openai_api_key else 'NOT SET'}")
        else:
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