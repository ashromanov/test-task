from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from payments.config import settings

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=0,
    pool_pre_ping=True,
    connect_args={
        "timeout": 5,
        "command_timeout": 10,
        "server_settings": {"application_name": "async-payments"},
    },
)
session_factory = async_sessionmaker(engine, expire_on_commit=False)
