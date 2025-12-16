from pydantic_settings import BaseSettings
import os

class Settings(BaseSettings):
    app_name: str = "My API"
    debug: bool = os.getenv("DEBUG", "False").lower() == "true"
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./test.db")

    class Config:
        env_file = ".env"

settings = Settings()