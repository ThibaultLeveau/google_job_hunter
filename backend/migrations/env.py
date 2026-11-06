from alembic import context

from job_hunter.infrastructure import (
    accounts,  # noqa: F401 -- register account tables
    budgets,  # noqa: F401 -- register private provider budget tables
    chunks,  # noqa: F401 -- register public chunk tables
    cleaning,  # noqa: F401 -- register public cleaning tables
    crawl_policy,  # noqa: F401 -- register crawler policy tables
    crawling,  # noqa: F401 -- register crawler checkpoint tables
    documents,  # noqa: F401 -- register document tables
    embeddings,  # noqa: F401 -- register public vector tables
    job_details,  # noqa: F401 -- register owner retry receipts
    job_metadata,  # noqa: F401 -- register source-verified metadata tables
    jobs,  # noqa: F401 -- register job registry tables
    matches,  # noqa: F401 -- register private match identities and attempts
    preparation,  # noqa: F401 -- register public text preparation tables
    profiles,  # noqa: F401 -- register profile tables
    requirements,  # noqa: F401 -- register full application requirements
    searches,  # noqa: F401 -- register search tables
    snapshots,  # noqa: F401 -- register public snapshot tables
)
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
