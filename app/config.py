from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── The shared logins. EMPTY BY DEFAULT, and that is the security property. ──
    #
    # These used to default to "admin123" and "ops123". A default is not a placeholder:
    # it is the value the app actually uses when the variable is absent from .env — and
    # this repository is public, so an unset APP_PASSWORD meant a published admin
    # password on a live box. Measured, not assumed: a blank username with "admin123"
    # returned 303 to `/` with a full admin session, and it did so even with named
    # accounts present, because auth.py only takes the named path when a username is
    # actually typed.
    #
    # Empty now means "this login does not exist" — `login` skips a shared password that
    # is falsy, so a missing variable CLOSES the door instead of opening a known one. The
    # SP-API credentials below already work this way.
    #
    # They are still supported, and deliberately so: this app has no password-reset email
    # and no console, so a shared password is the only way back in if the users table is
    # damaged by a deploy. It just has to be set on purpose.
    app_password: str = ""
    # Second login for the operations employee: reaches only the packing-entry
    # screen (/ops-page) and its documents. Plan editing, verification and GST
    # invoicing stay behind app_password.
    ops_password: str = ""
    # Signs the session cookie. Empty means "not configured", and main.py refuses to
    # start rather than signing with a guessable key — a forged cookie carrying no role
    # resolves to ADMIN by design (that is what keeps every pre-existing session working),
    # so a known signing key is a full authentication bypass, not a downgrade.
    secret_key: str = ""
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

    # ── Amazon Selling Partner API ───────────────────────────────────────────
    #
    # Empty by default, and every SP-API feature checks `spapi_configured` first. A
    # missing credential must read as "not set up" on screen rather than as a 500:
    # the rest of this app has nothing to do with Amazon's API and has to keep
    # working without it.
    #
    # Secrets, so .env only. Note `cookies.txt` is already in git history with a live
    # session token; a refresh token is worth considerably more.
    sp_api_client_id: str = ""
    sp_api_client_secret: str = ""
    sp_api_refresh_token: str = ""
    # A21TJRUUN4KGV is Amazon.in, and India is served from the EU endpoint — not
    # obvious, and worth pinning here rather than rediscovering.
    sp_api_marketplace_id: str = "A21TJRUUN4KGV"
    sp_api_endpoint: str = "https://sellingpartnerapi-eu.amazon.com"
    sp_api_timeout: int = 40

    @property
    def spapi_configured(self) -> bool:
        """All three secrets present. Marketplace and endpoint have working defaults."""
        return bool(
            self.sp_api_client_id
            and self.sp_api_client_secret
            and self.sp_api_refresh_token
        )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        # SP-API credentials sit alongside a dozen unrelated keys in the real .env
        # (AWS, Bedrock, Cognito). Without this, pydantic-settings rejects the whole
        # file as having extra fields and the app will not start.
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
