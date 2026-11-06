import re
import warnings
from importlib.metadata import version
from platform import python_version
from uuid import UUID, uuid4

from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, Tag
from bs4.exceptions import ParserRejectedMarkup
from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.domain.errors import DomainError
from job_hunter.domain.preparation import (
    MAX_CHARACTERS,
    MAX_PARAGRAPHS,
    Paragraph,
    Preparation,
    digest,
    paragraph_id,
)
from job_hunter.domain.snapshots import FetchedSnapshot
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.snapshots import SnapshotRecord

PREPARATION_VERSION = (
    f"html-text/1;beautifulsoup4/{version('beautifulsoup4')};python/{python_version()}"
)
INPUT_BYTES = 1024 * 1024
SKIP = set(
    "head script style nav footer aside form template iframe object embed "
    "svg math canvas input button select textarea".split()
)
BLOCKS = set(
    "p div article section main h1 h2 h3 h4 h5 h6 li ul ol dl dt dd "
    "blockquote pre br hr tr td th".split()
)


def html_parts(text: str) -> tuple[list[str], str | None]:
    with warnings.catch_warnings(action="ignore"):
        soup = BeautifulSoup(text, "html.parser")
    html = soup.find("html")
    language = html.get("lang") if isinstance(html, Tag) else None
    if (
        not isinstance(language, str)
        or len(language) > 80
        or any(ord(char) < 32 for char in language)
    ):
        language = None
    parts: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        value = " ".join("".join(buffer).split())
        buffer.clear()
        if value:
            parts.append(value)

    stack: list[tuple[PageElement, bool, int]] = [(soup.body or soup, False, 0)]
    nodes = 0
    while stack:
        node, closing, depth = stack.pop()
        nodes += 1
        if nodes > 20000 or depth > 128:
            raise OverflowError()
        if isinstance(node, Tag):
            if node.name in SKIP or node.has_attr("hidden") or node.has_attr("inert"):
                continue
            if node.get("aria-hidden") == "true" or node.get("role") in (
                "navigation",
                "banner",
                "contentinfo",
                "complementary",
            ):
                continue
            if node.name in BLOCKS:
                flush()
            if not closing:
                stack.append((node, True, depth))
                stack.extend(
                    (child, False, depth + 1) for child in reversed(node.contents)
                )
        elif type(node) is NavigableString:
            buffer.append(str(node))
    flush()
    return parts, language


def prepare(snapshot_id: UUID, source: FetchedSnapshot) -> Preparation:
    base: dict[str, object] = dict(
        snapshot_id=snapshot_id,
        source_hash=source.content_hash,
        version=PREPARATION_VERSION,
        content_hash=digest(""),
    )

    def unsupported(reason: str) -> Preparation:
        return Preparation.model_validate(
            {**base, "state": "unsupported", "reason": reason}
        )

    if not 200 <= source.status < 300:
        return unsupported("http_status")
    mime = (source.content_type or "").split(";", 1)[0].strip().lower()
    if mime not in {"text/html", "application/xhtml+xml", "text/plain"}:
        return unsupported("content_type")
    if len(source.body) > INPUT_BYTES:
        return unsupported("limits")
    charset = re.search(
        r"charset\s*=\s*[\"']?([\w-]+)", source.content_type or "", re.I
    )
    encoding = charset[1].lower() if charset else "utf-8-sig"
    if encoding not in {"utf-8", "utf-8-sig", "utf8", "iso-8859-1", "windows-1252"}:
        return unsupported("encoding")
    try:
        text = source.body.decode(encoding)
        if "\x00" in text:
            return unsupported("encoding")
        parts, language = (
            (
                [" ".join(line.split()) for line in text.splitlines() if line.strip()],
                None,
            )
            if mime == "text/plain"
            else html_parts(text)
        )
    except UnicodeError:
        return unsupported("encoding")
    except OverflowError, MemoryError, RecursionError:
        return unsupported("limits")
    except ParserRejectedMarkup:
        return unsupported("malformed")
    joined = "\n\n".join(parts)
    if len(joined) > MAX_CHARACTERS or len(parts) > MAX_PARAGRAPHS:
        return unsupported("limits")
    paragraphs, offset = [], 0
    for index, part in enumerate(parts):
        paragraphs.append(
            Paragraph(
                id=paragraph_id(snapshot_id, PREPARATION_VERSION, index, part),
                start=offset,
                end=offset + len(part),
                text=part,
                content_hash=digest(part),
            )
        )
        offset += len(part) + 2
    non_job = joined.casefold() in {
        "404",
        "404 not found",
        "page not found",
        "access denied",
    }
    return Preparation.model_validate(
        {
            **base,
            "state": "non_job" if non_job else "uncertain" if parts else "empty",
            "reason": "non_job_marker" if non_job else None,
            "language": language,
            "paragraphs": paragraphs,
            "content_hash": digest(joined),
        }
    )


class PreparationRecord(Base):
    __tablename__ = "job_preparations"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "version", name="uq_preparation_revision"),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_preparation_payload"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("job_snapshots.id"))
    version: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)


class PostgresPreparation:
    def __init__(self, database: Database) -> None:
        self.database = database

    def run(self, snapshot_id: UUID) -> PreparationRecord:
        with self.database.transaction() as session:
            existing = session.scalar(
                select(PreparationRecord).where(
                    PreparationRecord.snapshot_id == snapshot_id,
                    PreparationRecord.version == PREPARATION_VERSION,
                )
            )
            if existing is not None:
                return existing
            row = session.get(SnapshotRecord, snapshot_id)
            if row is None:
                raise DomainError("not_found")
            source = FetchedSnapshot(
                row.requested_url,
                row.final_url,
                row.status,
                row.content_type,
                row.body,
                row.fetched_at,
            )
        result = prepare(snapshot_id, source)
        with self.database.transaction() as session:
            return self.publish(session, result)

    def publish(self, session: Session, result: Preparation) -> PreparationRecord:
        result = Preparation.model_validate(result.model_dump())
        source_hash = session.scalar(
            select(SnapshotRecord.content_hash).where(
                SnapshotRecord.id == result.snapshot_id
            )
        )
        if source_hash != result.source_hash:
            raise DomainError("conflict")
        session.execute(
            insert(PreparationRecord)
            .values(
                id=uuid4(),
                snapshot_id=result.snapshot_id,
                version=result.version,
                payload=result.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(constraint="uq_preparation_revision")
        )
        return session.scalars(
            select(PreparationRecord).where(
                PreparationRecord.snapshot_id == result.snapshot_id,
                PreparationRecord.version == result.version,
            )
        ).one()
