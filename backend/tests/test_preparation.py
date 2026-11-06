from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Lock
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs
from test_snapshots import page, publish, register

from job_hunter.domain.errors import DomainError
from job_hunter.domain.preparation import Preparation, digest
from job_hunter.domain.snapshots import FetchedSnapshot
from job_hunter.infrastructure import preparation
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.preparation import (
    INPUT_BYTES,
    PostgresPreparation,
    PreparationRecord,
    prepare,
)
from job_hunter.infrastructure.snapshots import SnapshotRecord
from job_hunter.settings import Settings

owners, runs = registry_owners, registry_runs
SNAPSHOT = UUID("12345678-1234-1234-1234-123456789012")


@pytest.mark.parametrize(
    "html,expected",
    [
        (
            "<p>Build <b>reliable</b> APIs.<p>Remote &amp; flexible",
            ["Build reliable APIs.", "Remote & flexible"],
        ),
        (
            "<main><h1>Job</h1><ol><li>Build APIs</li><li>Review code</li></ol></main>",
            ["Job", "Build APIs", "Review code"],
        ),
        ("<nav>Home Jobs</nav><p>Work here</p><nav>Home Jobs</nav>", ["Work here"]),
        (
            "<div role='navigation'>Menu</div><div hidden>Private</div><p>Public</p>",
            ["Public"],
        ),
        (
            "<p>Safe<script>alert(1)</script><style>body{}</style> text</p><iframe src='https://example.org'></iframe>",
            ["Safe text"],
        ),
        (
            "<p>First<br>Second</p><!-- secret --><template>hidden</template>",
            ["First", "Second"],
        ),
        (
            "<article><header><h1>Job title</h1></header><p>Work here</p></article>",
            ["Job title", "Work here"],
        ),
        ("https://example.org", ["https://example.org"]),
        ("<p>Write &lt;script&gt; as plain text</p>", ["Write <script> as plain text"]),
    ],
)
def test_html_extracts_visible_prose_in_document_order(
    html: str, expected: list[str]
) -> None:
    result = prepare(SNAPSHOT, page(body=html.encode()))
    assert result.state == "uncertain"
    assert [part.text for part in result.paragraphs] == expected
    assert result.content_hash == digest("\n\n".join(expected))


def test_multilingual_provenance_is_stable_and_bound_to_snapshot_and_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = page(
        body=(
            '<html lang="fr"><body><p>Développeuse 日本語 👩🏽‍💻</p>'
            "<p>مرحبا</p></body></html>"
        ).encode()
    )
    result = prepare(SNAPSHOT, source)
    assert result.language == "fr" and result.source_hash == source.content_hash
    assert result == prepare(SNAPSHOT, source)
    assert Preparation.model_validate_json(result.model_dump_json()) == result
    for paragraph in result.paragraphs:
        assert result.text[paragraph.start : paragraph.end] == paragraph.text
        assert paragraph.content_hash == digest(paragraph.text)
    changed = prepare(uuid4(), source)
    assert changed.content_hash == result.content_hash
    assert changed.paragraphs[0].id != result.paragraphs[0].id
    monkeypatch.setattr(preparation, "PREPARATION_VERSION", "html-text/next")
    assert prepare(SNAPSHOT, source).paragraphs[0].id != result.paragraphs[0].id


@pytest.mark.parametrize(
    "body,mime,status,state,reason",
    [
        (b" \n\t", "text/plain", 200, "empty", None),
        (b"<script>throw 1</script><nav>Jobs</nav>", "text/html", 200, "empty", None),
        (b"<h1>404 Not Found</h1>", "text/html", 200, "non_job", "non_job_marker"),
        (b"Public text", "application/pdf", 200, "unsupported", "content_type"),
        (b"Public text", None, 200, "unsupported", "content_type"),
        (b"Public text", "text/plain", 403, "unsupported", "http_status"),
        (b"\xff", "text/html", 200, "unsupported", "encoding"),
        (b"a\x00b", "text/plain", 200, "unsupported", "encoding"),
        (b"text", "text/html;charset=utf-7", 200, "unsupported", "encoding"),
        (b"x" * (INPUT_BYTES + 1), "text/html", 200, "unsupported", "limits"),
        (b"<p>" + b"x" * 100001, "text/html", 200, "unsupported", "limits"),
        (b"<p>word</p>" * 2001, "text/html", 200, "unsupported", "limits"),
        (b"<i></i>" * 11000, "text/html", 200, "unsupported", "limits"),
        (b"<div>" * 130 + b"text", "text/html", 200, "unsupported", "limits"),
    ],
)
def test_explicit_conservative_outcomes(
    body: bytes,
    mime: str | None,
    status: int,
    state: str,
    reason: str | None,
) -> None:
    source = replace(page(body=body), content_type=mime, status=status)
    result = prepare(SNAPSHOT, source)
    assert (result.state, result.reason) == (state, reason)
    assert result.source_hash == source.content_hash
    if state in {"empty", "unsupported"}:
        assert not result.paragraphs and not result.text


def test_plain_text_and_declared_encoding_share_paragraph_contract() -> None:
    result = prepare(
        SNAPSHOT,
        replace(
            page(body=b"D\xe9velopper\n\nRevoir le code"),
            content_type="text/plain; charset=windows-1252",
        ),
    )
    assert [p.text for p in result.paragraphs] == ["Développer", "Revoir le code"]
    assert result.state == "uncertain" and result.language is None
    assert "text=" not in repr(result.paragraphs[0])


@pytest.mark.parametrize(
    "field,value",
    [
        ("start", 1),
        ("end", 100),
        ("content_hash", "0" * 64),
        ("id", str(uuid4())),
    ],
)
def test_provenance_cannot_be_forged(field: str, value: object) -> None:
    result = prepare(SNAPSHOT, page(body=b"<p>Job prose</p>"))
    data = result.model_dump(mode="json")
    data["paragraphs"][0][field] = value
    with pytest.raises(ValidationError):
        Preparation.model_validate(data)


@pytest.mark.integration
def test_persistence_reuses_immutable_revisions_and_preserves_source(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = register(isolated_database, owners[0], runs[0])
    snapshot = publish(isolated_database, source, page(body=b"<p>First</p>"))
    store = PostgresPreparation(isolated_database)
    record = store.run(snapshot.id)
    assert store.run(snapshot.id).id == record.id
    result = Preparation.model_validate(record.payload)
    assert result.snapshot_id == snapshot.id and result.text == "First"
    changed = publish(isolated_database, source, page(body=b"<p>Second</p>"))
    assert store.run(changed.id).id != record.id
    with isolated_database.transaction() as session:
        preserved = session.get(SnapshotRecord, snapshot.id)
        assert preserved is not None and preserved.body == b"<p>First</p>"
    with pytest.raises(DBAPIError), isolated_database.transaction() as session:
        session.execute(
            update(PreparationRecord)
            .where(PreparationRecord.id == record.id)
            .values(payload={})
        )
    with (
        pytest.raises(DomainError, match="conflict"),
        isolated_database.transaction() as session,
    ):
        store.publish(session, result.model_copy(update={"source_hash": "0" * 64}))
    with pytest.raises(DomainError, match="not_found"):
        store.run(uuid4())
    monkeypatch.setattr(preparation, "PREPARATION_VERSION", "html-text/next")
    next_version = prepare(snapshot.id, page(body=b"<p>First</p>"))
    with pytest.raises(RuntimeError), isolated_database.transaction() as session:
        store.publish(session, next_version)
        raise RuntimeError("rollback")
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(PreparationRecord)) == 2
    assert store.run(snapshot.id).id != record.id
    with pytest.raises(IntegrityError), isolated_database.transaction() as session:
        session.add(
            PreparationRecord(
                id=uuid4(), snapshot_id=uuid4(), version="unknown", payload={}
            )
        )
    with pytest.raises(IntegrityError), isolated_database.transaction() as session:
        session.add(
            PreparationRecord(
                id=uuid4(), snapshot_id=snapshot.id, version="invalid", payload=[]
            )
        )
    config = Config(toml_file="pyproject.toml")
    command.check(config)
    command.downgrade(config, "0021_crawl_worker")
    command.upgrade(config, "head")
    command.check(config)


@pytest.mark.integration
def test_concurrent_preparation_commits_one_artifact_without_locks_during_parsing(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = register(isolated_database, owners[0], runs[0])
    snapshot = publish(isolated_database, source, page())
    database = Database(Settings(database_pool_size=2))
    barrier, original, probe = Barrier(2), preparation.prepare, Lock()

    def concurrent(snapshot_id: UUID, fetched: FetchedSnapshot) -> Preparation:
        with probe, database.transaction() as session:
            assert session.get(
                SnapshotRecord, snapshot_id, with_for_update={"nowait": True}
            )
        barrier.wait(timeout=5)
        return original(snapshot_id, fetched)

    monkeypatch.setattr(preparation, "prepare", concurrent)
    try:
        with ThreadPoolExecutor(2) as pool:
            futures = [
                pool.submit(PostgresPreparation(database).run, snapshot.id)
                for _ in range(2)
            ]
            records = [future.result(timeout=10) for future in futures]
        assert (
            records[0].id == records[1].id and records[0].payload == records[1].payload
        )
        with database.transaction() as session:
            assert (
                session.scalar(select(func.count()).select_from(PreparationRecord)) == 1
            )
    finally:
        database.close()
