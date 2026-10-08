"""Application factory.

Building the app through a function (instead of module-level globals) lets
tests create an isolated app with its own database, storage folder and - if
needed - a fake certificate generator.
"""
from fastapi import FastAPI

from app.config import Settings
from app.database import make_engine, make_session_factory
from app.models import Base
from app.routers.jobs import router as jobs_router
from app.services.certificate import generate_certificate_pdf


def create_app(settings: Settings | None = None, generator=None) -> FastAPI:
    settings = settings or Settings()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)

    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)  # fine for this scope; use Alembic in production

    app = FastAPI(
        title="Bulk Certificate Generator",
        version="1.0.0",
        description="Submit a list of recipients, get PDF certificates back.",
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.generator = generator or generate_certificate_pdf

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok"}

    app.include_router(jobs_router)
    return app
