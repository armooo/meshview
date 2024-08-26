from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine

from meshview import models


def init_database(database_connetion_string):
    global engine, async_session
    kwargs = {}
    if not database_connetion_string.startswith('sqlite'):
        kwargs['pool_size'] = 20
        kwargs['max_overflow'] = 50
    engine = create_async_engine(database_connetion_string, echo=False, **kwargs)
    async_session = async_sessionmaker(engine, expire_on_commit=False)


async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
