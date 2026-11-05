from alembic import context

from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.tasks import TaskRecord
from job_hunter.settings import load_settings

if context.is_offline_mode():
    context.configure(
        dialect_name="postgresql",
        literal_binds=True,
        target_metadata=TaskRecord.metadata,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    database = Database(load_settings())
    try:
        with database.engine.connect() as connection:
            context.configure(
                connection=connection, target_metadata=TaskRecord.metadata
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        database.close()
