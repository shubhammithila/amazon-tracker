from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    app_password: str = "admin123"
    # Second login for the operations employee: reaches only the packing-entry
    # screen (/ops-page) and its documents. Plan editing, verification and GST
    # invoicing stay behind app_password. Empty string disables the ops login.
    ops_password: str = "ops123"
    secret_key: str = "change-me-in-production-use-random-bytes"
    database_url: str = "sqlite+aiosqlite:///./tracker.db"

    # The product master sheet. Column T ("Active", Y/N) decides which ASINs may
    # appear in a shipment plan — a discontinued product must not reach the
    # packer's sheet or an Amazon upload.
    #
    # Configurable rather than hardcoded because this id will change eventually (a
    # copy of the sheet, a new year, a different account), and when it does the fix
    # should be an env var and not a redeploy.
    product_sheet_id: str = "11dBw92P7Bg0oFyfqramGqdAlLTGhcb2ScjmR_1wtiTM"
    product_sheet_gid: str = "0"
    product_sheet_timeout: int = 20

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
