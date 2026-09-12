from __future__ import annotations

import os
from dataclasses import dataclass


def _csv_ints(value: str) -> frozenset[int]:
    return frozenset(int(item.strip()) for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    admin_user_ids: frozenset[int]
    database_url: str = ""
    allowed_group_ids: frozenset[int] = frozenset()
    dmit_knowledge_group_ids: frozenset[int] = frozenset()
    dmit_channel_group_ids: frozenset[int] = frozenset()
    dmit_news_channel_url: str = "https://t.me/dmitnews"
    telegram_transport: str = "polling"
    telegram_webhook_url: str = ""
    telegram_webhook_secret: str = ""
    bot_api_host: str = "0.0.0.0"
    bot_api_port: int = 8081
    join_verify_url: str = ""
    turnstile_site_key: str = ""
    turnstile_secret_key: str = ""
    auto_ban_threshold: float = 0.90
    review_threshold: float = 0.60
    llm_mode: str = "rule_based"
    llm_api_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    kimi_api_base_url: str = "https://api.kimi.com/coding/v1"
    kimi_api_key: str = ""
    kimi_model: str = ""
    verification_ttl_minutes: int = 5
    secondary_verification_ttl_minutes: int = 2
    audit_api_host: str = "127.0.0.1"
    audit_api_port: int = 8080
    audit_web_app_url: str = ""
    bot_reply_enabled: bool = True
    log_level: str = "INFO"

    @classmethod
    def from_environment(cls) -> "Settings":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
        transport = os.environ.get("TELEGRAM_TRANSPORT", "polling").lower()
        if transport not in {"polling", "webhook"}:
            raise RuntimeError("TELEGRAM_TRANSPORT must be polling or webhook")
        webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "")
        webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        if transport == "webhook" and not (webhook_url and webhook_secret):
            raise RuntimeError("TELEGRAM_WEBHOOK_URL and TELEGRAM_WEBHOOK_SECRET are required for webhook")
        join_verify_url = os.environ.get("JOIN_VERIFY_URL", "").rstrip("/")
        if join_verify_url:
            join_verify_url += "/"
        return cls(
            telegram_bot_token=token,
            admin_user_ids=_csv_ints(os.environ.get("ADMIN_USER_IDS", "")),
            database_url=os.environ.get("DATABASE_URL", "").strip(),
            allowed_group_ids=_csv_ints(os.environ.get("ALLOWED_GROUP_IDS", "")),
            dmit_knowledge_group_ids=_csv_ints(os.environ.get("DMIT_KNOWLEDGE_GROUP_IDS", "")),
            dmit_channel_group_ids=_csv_ints(os.environ.get("DMIT_CHANNEL_GROUP_IDS", "")),
            dmit_news_channel_url=os.environ.get("DMIT_NEWS_CHANNEL_URL", "https://t.me/dmitnews").strip(),
            telegram_transport=transport,
            telegram_webhook_url=webhook_url,
            telegram_webhook_secret=webhook_secret,
            bot_api_host=os.environ.get("BOT_API_HOST", "0.0.0.0"),
            bot_api_port=int(os.environ.get("BOT_API_PORT", "8081")),
            join_verify_url=join_verify_url,
            turnstile_site_key=os.environ.get("TURNSTILE_SITE_KEY", ""),
            turnstile_secret_key=os.environ.get("TURNSTILE_SECRET_KEY", ""),
            auto_ban_threshold=float(os.environ.get("AUTO_BAN_THRESHOLD", "0.90")),
            review_threshold=float(os.environ.get("REVIEW_THRESHOLD", "0.60")),
            llm_mode=os.environ.get("LLM_MODE", "rule_based"),
            llm_api_base_url=os.environ.get("LLM_API_BASE_URL", ""),
            llm_api_key=os.environ.get("LLM_API_KEY", ""),
            llm_model=os.environ.get("LLM_MODEL", ""),
            kimi_api_base_url=os.environ.get("KIMI_API_BASE_URL", "https://api.kimi.com/coding/v1"),
            kimi_api_key=os.environ.get("KIMI_API_KEY", ""),
            kimi_model=os.environ.get("KIMI_MODEL", ""),
            verification_ttl_minutes=int(os.environ.get("JOIN_VERIFICATION_TTL_MINUTES", "5")),
            secondary_verification_ttl_minutes=int(
                os.environ.get("SECONDARY_VERIFICATION_TTL_MINUTES", "2")
            ),
            audit_api_host=os.environ.get("AUDIT_API_HOST", "127.0.0.1"),
            audit_api_port=int(os.environ.get("AUDIT_API_PORT", "8080")),
            audit_web_app_url=os.environ.get("AUDIT_WEB_APP_URL", "").rstrip("/"),
            bot_reply_enabled=os.environ.get("BOT_REPLY_ENABLED", "true").lower() in {"1", "true", "yes", "on"},
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        )
