import hashlib
import logging
import math
import os
import resource
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from importlib.metadata import version
from typing import BinaryIO, cast
from zipfile import ZipFile

from pydantic import BaseModel, ConfigDict, Field

from job_hunter.domain.documents import ParseResult, TextSegment

REPORT_BYTES = 16 * 1024 * 1024


class ParseLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    seconds: float = Field(default=10, ge=0.001, le=60)
    memory_bytes: int = Field(
        default=256 * 1024 * 1024, ge=64 * 1024 * 1024, le=1024 * 1024 * 1024
    )
    input_bytes: int = Field(default=50 * 1024 * 1024, ge=1, le=50 * 1024 * 1024)
    expansion_bytes: int = Field(default=50 * 1024 * 1024, ge=1, le=50 * 1024 * 1024)
    characters: int = Field(default=1000000, ge=1, le=1000000)
    pages: int = Field(default=100, ge=1, le=1000)


def parse(file: BinaryIO, media_type: str, limits: ParseLimits) -> ParseResult:
    from docx import Document as WordDocument
    from docx.oxml.ns import qn
    from docx.oxml.text.paragraph import CT_P
    from docx.text.paragraph import Paragraph
    from pypdf import PdfReader, apply_configuration
    from pypdf.errors import LimitReachedError

    try:
        file.seek(0, os.SEEK_END)
        if file.tell() > limits.input_bytes:
            return ParseResult(error="limits_exceeded")
        file.seek(0)
        source = hashlib.sha256()
        while chunk := file.read(65536):
            source.update(chunk)
        digest = source.hexdigest()
        file.seek(0)
        language: str | None = None
        parts: Iterable[tuple[str, str]]
        if media_type == "application/pdf":
            with apply_configuration(
                zlib_maximum_output_length=limits.expansion_bytes,
                stream_decoding_work_maximum_length=limits.expansion_bytes,
            ):
                reader = PdfReader(file, strict=True)
                if reader.is_encrypted:
                    return ParseResult(error="encrypted_document")
                if len(reader.pages) > limits.pages:
                    return ParseResult(error="limits_exceeded")
                language = cast(str | None, reader.root_object.get("/Lang"))
                pages: list[tuple[str, str]] = []
                expanded, characters = 0, 0
                for number, page in enumerate(reader.pages, 1):
                    content = page.get_contents()
                    expanded += len(content.get_data()) if content is not None else 0
                    if expanded > limits.expansion_bytes:
                        return ParseResult(error="limits_exceeded")
                    text = page.extract_text()
                    characters += len(text) + (1 if pages else 0)
                    if characters > limits.characters:
                        return ParseResult(error="limits_exceeded")
                    if not text.strip() and len(page.images):
                        return ParseResult(error="ocr_required")
                    pages.append((f"page:{number}", text))
                parts, engine = pages, "pypdf/" + version("pypdf")
        elif (
            media_type
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ):
            with ZipFile(file) as archive:
                entries = archive.infolist()
                if (
                    len(entries) > 1000
                    or sum(entry.file_size for entry in entries)
                    > limits.expansion_bytes
                ):
                    return ParseResult(error="limits_exceeded")
                if any(entry.flag_bits & 1 for entry in entries):
                    return ParseResult(error="encrypted_document")
            file.seek(0)
            document = WordDocument(file)
            language = document.core_properties.language or None
            parts = (
                (f"paragraph:{number}", Paragraph(cast(CT_P, element), document).text)
                for number, element in enumerate(
                    document.element.body.iter(qn("w:p")), 1
                )
            )
            engine = "python-docx/" + version("python-docx")
        elif media_type == "text/plain":
            text = file.read().decode("utf-8-sig")
            parts = (
                (f"paragraph:{number}", text)
                for number, text in enumerate(text.splitlines(), 1)
            )
            engine = "python/" + sys.version.split()[0]
        else:
            return ParseResult(error="unsupported_format")
        segments: list[TextSegment] = []
        offset = 0
        for locator, text in parts:
            if offset + len(text) > limits.characters or len(segments) >= 10000:
                return ParseResult(error="limits_exceeded")
            segments.append(
                TextSegment(
                    locator=locator, start=offset, end=offset + len(text), text=text
                )
            )
            offset += len(text) + 1
        if not any(segment.text.strip() for segment in segments):
            return ParseResult(error="empty_document")
        return ParseResult(
            segments=tuple(segments),
            language=language,
            engine=engine,
            source_hash=digest,
        )
    except MemoryError, LimitReachedError:
        return ParseResult(error="limits_exceeded")
    except Exception:
        return ParseResult(error="invalid_document")


class IsolatedParser:
    def __init__(
        self,
        limits: ParseLimits | None = None,
        *,
        cancelled: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits, self.cancelled, self.clock = (
            limits or ParseLimits(),
            cancelled,
            clock,
        )

    def __call__(self, file: BinaryIO, media_type: str) -> ParseResult:
        with tempfile.TemporaryDirectory(prefix="job-hunter-parse-") as directory:
            with tempfile.TemporaryFile(dir=directory) as output:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-m",
                        __name__,
                        media_type,
                        self.limits.model_dump_json(),
                    ],
                    stdin=file,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    env={"LANG": "C.UTF-8"},
                    cwd=directory,
                    start_new_session=True,
                )
                deadline = self.clock() + self.limits.seconds
                try:
                    while process.poll() is None:
                        if self.cancelled():
                            return ParseResult(error="cancelled")
                        if self.clock() >= deadline:
                            return ParseResult(error="timed_out")
                        try:
                            process.wait(timeout=0.05)
                        except subprocess.TimeoutExpired:
                            pass
                    if process.returncode != 0:
                        return ParseResult(error="limits_exceeded")
                    output.seek(0)
                    try:
                        return ParseResult.model_validate_json(
                            output.read(REPORT_BYTES)
                        )
                    except ValueError:
                        return ParseResult(error="invalid_document")
                finally:
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    process.wait()


def main() -> None:
    limits = ParseLimits.model_validate_json(sys.argv[2])
    resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
    resource.setrlimit(
        resource.RLIMIT_CPU, (math.ceil(limits.seconds), math.ceil(limits.seconds))
    )
    resource.setrlimit(resource.RLIMIT_FSIZE, (REPORT_BYTES, REPORT_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    logging.disable(logging.CRITICAL)
    result = parse(sys.stdin.buffer, sys.argv[1], limits)
    sys.stdout.write(result.model_dump_json())


if __name__ == "__main__":
    main()
