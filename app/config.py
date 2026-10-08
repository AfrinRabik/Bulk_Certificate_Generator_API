"""Application settings.

Plain dataclass (not a global singleton) so tests can build an app with a
temporary database and storage folder. Defaults come from environment
variables, so the same code runs locally (SQLite) and in production
(e.g. PostgreSQL via DATABASE_URL).
"""
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./certificates.db")
    )
    storage_dir: Path = field(
        default_factory=lambda: Path(os.getenv("STORAGE_DIR", "./storage"))
    )
    # Upper bound for recipients in a single request (protects the server).
    max_recipients: int = field(
        default_factory=lambda: int(os.getenv("MAX_RECIPIENTS", "1000"))
    )

    def __post_init__(self) -> None:
        self.storage_dir = Path(self.storage_dir)
