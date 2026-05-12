from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    app_password: str = "admin123"
    secret_key: str = "change-me-in-production-use-random-bytes"
    database_url: str = "sqlite+aiosqlite:///./tracker.db"

    scrape_concurrency: int = 10
    scrape_delay_min: float = 1.5
    scrape_delay_max: float = 3.5
    scrape_retry_rounds: int = 3
    scrape_timeout: int = 15

    scheduler_enabled: bool = True
    daily_scrape_hour: int = 6
    daily_scrape_minute: int = 0

    data_retention_days: int = 90

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
