from alembic.config import Config
from alembic.script import ScriptDirectory

heads = ScriptDirectory.from_config(Config(toml_file="pyproject.toml")).get_heads()
if len(heads) != 1:
    raise SystemExit(f"Expected exactly one migration head; found {len(heads)}.")
print(f"Migration head: {heads[0]}")
