from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    jwt_secret: str
    problems_dir: Path
    submit_per_minute: int
    token_hours: int
    max_source_kb: int


@lru_cache(maxsize=1)
def get() -> Settings:
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret) < 32:
        # A short or missing secret means anyone can mint admin tokens.
        raise RuntimeError("JWT_SECRET must be set to at least 32 characters")
    return Settings(
        database_url=os.environ.get("DATABASE_URL", "postgresql+psycopg://postgres@localhost/judgebox"),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        jwt_secret=secret,
        problems_dir=Path(os.environ.get("PROBLEMS_DIR", Path(__file__).resolve().parents[1] / "problems")),
        submit_per_minute=int(os.environ.get("SUBMIT_PER_MINUTE", "6")),
        token_hours=int(os.environ.get("TOKEN_HOURS", "12")),
        max_source_kb=int(os.environ.get("MAX_SOURCE_KB", "64")),
    )
