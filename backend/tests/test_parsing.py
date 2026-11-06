import hashlib
import json
import logging
import resource
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO, TextIOWrapper
from pathlib import Path
from threading import Event
from typing import BinaryIO
from unittest.mock import MagicMock, call
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from docx import Document as WordDocument
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from test_profiles import NOW, ORIGIN
from test_profiles import owners as profile_owners

from job_hunter.app import create_app
from job_hunter.application.documents import DocumentUploads
from job_hunter.application.worker import RetryableTaskError
from job_hunter.domain.documents import PARSER_VERSION, TYPES, ParseResult
from job_hunter.domain.tasks import StaleLease
from job_hunter.infrastructure import parsing
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.documents import (
    DocumentParsing,
    DocumentRecord,
    LocalStorage,
    ParsedDocumentRecord,
    PostgresDocuments,
    StorageSettings,
)
from job_hunter.infrastructure.parsing import IsolatedParser, ParseLimits, parse
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.infrastructure.worker import PostgresWorkerStore
from job_hunter.settings import Settings

owners = profile_owners
DOCX = list(TYPES)[1]


def pdf(
    *,
    scanned: bool = False,
    encrypted: bool = False,
    pages: int = 1,
    empty: bool = False,
) -> bytes:
    writer = PdfWriter()
    writer.root_object[NameObject("/Lang")] = TextStringObject("fr")
    for _ in range(pages):
        page = writer.add_blank_page(width=72, height=72)
        if empty:
            continue
        content = DecodedStreamObject()
        if scanned:
            image = DecodedStreamObject()
            image.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(1),
                    NameObject("/Height"): NumberObject(1),
                    NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                }
            )
            image.set_data(b"\0\0\0")
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/XObject"): DictionaryObject(
                        {NameObject("/Scan"): writer._add_object(image)}
                    )
                }
            )
            content.set_data(b"q 50 0 0 50 0 0 cm /Scan Do Q")
        else:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): writer._add_object(font)}
                    )
                }
            )
            content.set_data(b"BT /F1 12 Tf 0 40 Td (Bonjour Python) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content.flate_encode())
    if encrypted:
        writer.encrypt("synthetic-password")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def docx() -> bytes:
    document = WordDocument()
    document.core_properties.language = "fr-FR"
    document.add_paragraph("Développeur")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "Python"
    document.add_paragraph("Projet")
    output = BytesIO()
    document.save(output)
    return output.getvalue()


@pytest.mark.parametrize(
    ("media_type", "content", "language", "locations"),
    [
        ("application/pdf", pdf(pages=2), "fr", ["page:1", "page:2"]),
        (DOCX, docx(), "fr-FR", ["paragraph:1", "paragraph:2", "paragraph:3"]),
        (
            "text/plain",
            "\ufeffDéveloppeur\n\nPython".encode(),
            None,
            ["paragraph:1", "paragraph:2", "paragraph:3"],
        ),
    ],
)
def test_formats_locations_language_hashes_and_real_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    media_type: str,
    content: bytes,
    language: str | None,
    locations: list[str],
) -> None:
    direct = parse(BytesIO(content), media_type, ParseLimits())
    assert direct.error is None and direct.language == language
    assert [segment.locator for segment in direct.segments] == locations
    assert "Python" in direct.text
    for segment in direct.segments:
        assert direct.text[segment.start : segment.end] == segment.text
    assert direct.source_hash == hashlib.sha256(content).hexdigest()
    assert direct.content_hash == hashlib.sha256(direct.text.encode()).hexdigest()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    launch = MagicMock(wraps=subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", launch)
    with tempfile.TemporaryFile() as file:
        file.write(content)
        file.seek(0)
        assert IsolatedParser()(file, media_type) == direct
    assert launch.call_args.kwargs["env"] == {"LANG": "C.UTF-8"}
    assert launch.call_args.kwargs["start_new_session"] is True
    assert "-I" in launch.call_args.args[0]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("content", "media_type", "limits", "error"),
    [
        (b"%PDF-1.7\n%%EOF", "application/pdf", {}, "invalid_document"),
        (pdf(encrypted=True), "application/pdf", {}, "encrypted_document"),
        (pdf(scanned=True), "application/pdf", {}, "ocr_required"),
        (pdf(empty=True), "application/pdf", {}, "empty_document"),
        (pdf(pages=2), "application/pdf", {"pages": 1}, "limits_exceeded"),
        (pdf(), "application/pdf", {"expansion_bytes": 10}, "limits_exceeded"),
        (pdf(), "application/pdf", {"characters": 5}, "limits_exceeded"),
        (b" \n", "text/plain", {}, "empty_document"),
        (b"\xff", "text/plain", {}, "invalid_document"),
        (b"private\0canary", "text/plain", {}, "invalid_document"),
        (b"text", "image/png", {}, "unsupported_format"),
        (b"text", "text/plain", {"input_bytes": 1}, "limits_exceeded"),
        (b"text", "text/plain", {"characters": 1}, "limits_exceeded"),
        (b"bad ZIP", DOCX, {}, "invalid_document"),
    ],
)
def test_safe_parser_failures(
    content: bytes, media_type: str, limits: dict[str, int], error: str
) -> None:
    result = parse(BytesIO(content), media_type, ParseLimits(**limits))
    assert result.error == error and result.segments == ()
    assert "canary" not in result.model_dump_json()


def test_zip_expansion_and_segment_limits() -> None:
    file = BytesIO()
    with ZipFile(file, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * 100000)
    assert len(file.getvalue()) < 1000
    assert (
        parse(file, DOCX, ParseLimits(expansion_bytes=1000)).error == "limits_exceeded"
    )
    assert (
        parse(BytesIO(b"x\n" * 10001), "text/plain", ParseLimits()).error
        == "limits_exceeded"
    )


@pytest.mark.parametrize("cancel", [True, False])
def test_timeout_and_cancellation_kill_child_and_remove_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    ticks = iter([0.0, 11.0])
    with tempfile.TemporaryFile() as file:
        file.write(b"private text")
        file.seek(0)
        result = IsolatedParser(cancelled=lambda: cancel, clock=lambda: next(ticks))(
            file, "text/plain"
        )
    assert result.error == ("cancelled" if cancel else "timed_out")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "interruption",
    ["none", "document", "owner", "lease", "source", "storage", "database"],
)
def test_fenced_publication_owner_tombstones_replay_and_atomic_rollback(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    storage = LocalStorage(tmp_path / "private")
    records = PostgresDocuments(isolated_database, StorageSettings(), lambda: NOW)
    upload = DocumentUploads(records, storage, 1024)
    with upload.receive("resume", "text/plain") as pending:
        pending.write(b"Bonjour Python")
        document, _ = upload.finish(owners[0], pending)
    service = DocumentParsing(isolated_database, storage, IsolatedParser(), lambda: NOW)
    store = PostgresWorkerStore(isolated_database, service.handlers())
    task = store.claim(("document_parse",), NOW, timedelta(seconds=30))
    assert task is not None
    if interruption == "database":
        monkeypatch.setattr(
            isolated_database, "transaction", MagicMock(side_effect=SQLAlchemyError())
        )
        with pytest.raises(RetryableTaskError):
            service.handlers()["document_parse"].execute(task)
        return
    if interruption == "storage":
        monkeypatch.setattr(storage, "open", MagicMock(side_effect=OSError()))
    entered, release = Event(), Event()

    def gated(file: BinaryIO, media_type: str) -> ParseResult:
        entered.set()
        assert release.wait(timeout=5)
        return IsolatedParser()(file, media_type)

    if interruption in {"owner", "document"}:
        service.parser = gated
    else:
        release.set()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.execute, task)
        try:
            if interruption in {"owner", "document"}:
                assert entered.wait(timeout=5)
                assert records.get(owners[0], document.id)[0].status == "processing"
                with isolated_database.transaction() as session:
                    if interruption == "document":
                        session.execute(update(DocumentRecord).values(deleted_at=NOW))
                    else:
                        session.execute(
                            update(UserRecord)
                            .where(UserRecord.id == owners[0])
                            .values(deleted_at=NOW)
                        )
        finally:
            release.set()
        result = future.result(timeout=15)
    assert result is not None
    if interruption == "source":
        with isolated_database.transaction() as session:
            session.execute(update(DocumentRecord).values(content_hash="f" * 64))
    if interruption == "none":
        with pytest.raises(RuntimeError), isolated_database.transaction() as session:
            PostgresTaskQueue(session).complete(task, NOW)
            service.publish(session, task, result, NOW)
            session.flush()
            raise RuntimeError("rollback fixture")
        with isolated_database.transaction() as session:
            assert (
                session.scalar(select(func.count()).select_from(ParsedDocumentRecord))
                == 0
            )
            assert session.scalars(select(TaskRecord.status)).one() == "running"
    if interruption == "lease":
        store.recover(("document_parse",), NOW + timedelta(seconds=31))
        assert service.execute(task) is None
        with pytest.raises(StaleLease):
            store.publish(task, result, NOW + timedelta(seconds=31))
    else:
        store.publish(task, result, NOW)
    with isolated_database.transaction() as session:
        artifact = session.scalar(select(ParsedDocumentRecord))
        row = session.get(DocumentRecord, document.id)
        assert row is not None
        if interruption != "none":
            assert artifact is None
            if interruption in {"source", "storage"}:
                assert row.status == "failed" and row.parse_error is not None
                assert session.scalars(select(TaskRecord.status)).one() == "failed"
            return
        assert (
            artifact is not None and row.status == "ready" and row.parse_error is None
        )
        assert artifact.source_hash == row.content_hash
        assert (
            artifact.content_hash == result.content_hash
            and artifact.parser_version == PARSER_VERSION
        )
        assert ParseResult.model_validate(artifact.data) == result
        artifact_id = artifact.id
        session.execute(update(TaskRecord).values(status="pending"))
    replay = store.claim(("document_parse",), NOW, timedelta(seconds=30))
    assert replay is not None
    service.parser = MagicMock(side_effect=AssertionError("Must reuse parsed artifact"))
    cached = service.execute(replay)
    assert cached == result
    store.publish(replay, cached, NOW)
    with isolated_database.transaction() as session:
        assert session.scalars(select(ParsedDocumentRecord.id)).one() == artifact_id
    with pytest.raises(IntegrityError), isolated_database.transaction() as session:
        values = {
            column.name: getattr(artifact, column.name)
            for column in ParsedDocumentRecord.__table__.columns
        }
        session.add(ParsedDocumentRecord(**{**values, "id": uuid4()}))
        session.flush()


@pytest.mark.integration
def test_actionable_failure_state_and_deleted_subject_before_execution(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path / "private")
    records = PostgresDocuments(isolated_database, StorageSettings(), lambda: NOW)
    upload = DocumentUploads(records, storage, 100000)
    with upload.receive("scan", "application/pdf") as pending:
        pending.write(pdf(scanned=True))
        document, _ = upload.finish(owners[0], pending)
    service = DocumentParsing(isolated_database, storage, IsolatedParser(), lambda: NOW)
    store = PostgresWorkerStore(isolated_database, service.handlers())
    task = store.claim(("document_parse",), NOW, timedelta(seconds=30))
    assert task is not None
    store.publish(task, service.execute(task), NOW)
    state = records.get(owners[0], document.id)[0]
    assert state.status == "unsupported" and state.parse_error == "ocr_required"
    monkeypatch.setenv("JOB_HUNTER_DOCUMENT_ROOT", str(storage.root))
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    app.state.sessions.now = lambda: NOW
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set("job_hunter_session", "a" * 43)
        assert (
            client.get("/api/v1/documents/" + str(document.id)).json()["parse_error"]
            == "ocr_required"
        )
    with isolated_database.transaction() as session:
        PostgresTaskQueue(session).retry(task.key, task.generation, NOW)
        session.execute(update(DocumentRecord).values(deleted_at=NOW))
    task = store.claim(("document_parse",), NOW, timedelta(seconds=30))
    assert task is not None and service.execute(task) is None
    store.publish(task, None, NOW)
    with isolated_database.transaction() as session:
        assert (
            session.scalar(select(func.count()).select_from(ParsedDocumentRecord)) == 0
        )


def test_child_installs_hard_resource_limits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install = MagicMock()
    monkeypatch.setattr(resource, "setrlimit", install)
    monkeypatch.setattr(logging, "disable", MagicMock())
    monkeypatch.setattr(
        sys, "argv", ["parser", "text/plain", ParseLimits().model_dump_json()]
    )
    monkeypatch.setattr(sys, "stdin", TextIOWrapper(BytesIO(b"fixture")))
    parsing.main()
    assert json.loads(capsys.readouterr().out)["error"] is None
    assert install.call_args_list == [
        call(resource.RLIMIT_AS, (268435456, 268435456)),
        call(resource.RLIMIT_CPU, (10, 10)),
        call(resource.RLIMIT_FSIZE, (parsing.REPORT_BYTES, parsing.REPORT_BYTES)),
        call(resource.RLIMIT_CORE, (0, 0)),
    ]


@pytest.mark.parametrize(
    "patch",
    [
        {"source_hash": None},
        {"error": "timed_out"},
        {"language": "private\0canary"},
        {"segments": [{"locator": "paragraph:1", "start": 1, "end": 4, "text": "abc"}]},
    ],
)
def test_parsed_result_rejects_incoherent_provenance(patch: dict[str, object]) -> None:
    from pydantic import ValidationError

    result = parse(BytesIO(b"abc"), "text/plain", ParseLimits())
    with pytest.raises(ValidationError) as error:
        ParseResult.model_validate({**result.model_dump(), **patch})
    assert "canary" not in str(error.value)


@pytest.mark.parametrize(
    ("exit_code", "error"), [(-9, "limits_exceeded"), (0, "invalid_document")]
)
def test_failed_child_or_invalid_report_is_safe_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exit_code: int,
    error: str,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    process = MagicMock(returncode=exit_code)
    process.poll.return_value = exit_code
    monkeypatch.setattr(subprocess, "Popen", MagicMock(return_value=process))
    with tempfile.TemporaryFile() as source:
        assert IsolatedParser()(source, "text/plain").error == error
    process.wait.assert_called_once()
    assert list(tmp_path.iterdir()) == []
