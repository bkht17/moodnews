"""Application configuration.

Every knob is environment-driven so the same image runs locally, in Docker and
in CI. The LLM section is deliberately provider-agnostic: the app only assumes
an OpenAI-compatible /chat/completions endpoint, so GLM (z.ai), OpenAI, or any
compatible gateway can be swapped in by changing LLM_BASE_URL + LLM_MODEL.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    app_name: str = "MoodNews API"
    debug: bool = False
    # Comma-separated list of allowed browser origins for CORS.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- Database ---
    # Inside Docker this points at the mounted volume (/data) so the SQLite
    # file survives container rebuilds; locally it defaults to ./moodnews.db.
    db_path: str = "moodnews.db"

    # --- News ingestion ---
    # Populate the DB on first boot when it is still empty.
    fetch_on_startup: bool = True
    # Minimum number of articles we want available in the grid.
    min_articles: int = 10
    # Per-feed cap so one chatty feed cannot dominate the grid.
    max_articles_per_feed: int = 5

    # --- LLM ---
    # Which client to talk to the model with:
    #   auto      pick from llm_model / llm_base_url (default)
    #   anthropic native Anthropic SDK - schema-enforced JSON, refusal handling
    #   openai    any OpenAI-compatible /chat/completions endpoint
    llm_provider: str = "auto"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.z.ai/api/paas/v4"
    llm_model: str = "glm-4.6"
    llm_timeout_seconds: float = 90.0
    # Rewriting benefits from a little creativity...
    llm_rewrite_temperature: float = 0.7
    # ...but the fact-checking auditor pass must be deterministic.
    llm_verify_temperature: float = 0.0

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def resolved_llm_provider(self) -> str:
        """The backend to use, resolving "auto" from the model and base URL."""
        provider = (self.llm_provider or "auto").strip().lower()
        if provider in ("anthropic", "claude"):
            return "anthropic"
        if provider == "openai":
            return "openai"
        # auto: a Claude model name, or Anthropic's own host, means Anthropic.
        if self.llm_model.strip().lower().startswith("claude"):
            return "anthropic"
        if "api.anthropic.com" in self.llm_base_url:
            return "anthropic"
        return "openai"


@lru_cache
def get_settings() -> Settings:
    return Settings()
