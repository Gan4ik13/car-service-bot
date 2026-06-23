import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from .models import Base

_engine = None
_session_factory = None

DEFAULT_DB_URL = "sqlite+aiosqlite:///./car_service.db"


async def init_db(db_url: str = None):
    global _engine, _session_factory
    url = db_url or os.getenv("DATABASE_URL") or DEFAULT_DB_URL
    _engine = create_async_engine(url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:  # type: ignore[misc]
    async with _session_factory() as session:
        yield session


async def close_db():
    if _engine:
        await _engine.dispose()
