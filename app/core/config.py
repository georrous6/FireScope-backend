from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

class Settings(BaseSettings):
    APP_NAME: str = "FireScope API"
    APP_VERSION: str = "0.1.0"
    API_V1_PREFIX: str = "/api/v1"

    DATA_DIR: Path = PROJECT_ROOT / "data"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
    )

    @property
    def raw_data_dir(self) -> Path:
        return self.DATA_DIR / "raw"

    @property
    def processed_data_dir(self) -> Path:
        return self.DATA_DIR / "processed"


settings = Settings()