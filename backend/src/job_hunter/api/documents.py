import asyncio
from collections.abc import Iterator
from typing import Annotated, cast
from urllib.parse import quote, unquote
from uuid import UUID

from anyio import CancelScope
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
from starlette.types import Send

from job_hunter.api.sessions import verified_user
from job_hunter.application.documents import DocumentUploads, PendingUpload
from job_hunter.application.extraction import Proposals
from job_hunter.domain.accounts import SessionUser
from job_hunter.domain.documents import TYPES, Document, InvalidDocument, UploadTooLarge
from job_hunter.domain.extraction import ProposalPage

router = APIRouter(prefix="/api/v1/documents")
Owner = Annotated[SessionUser, Depends(verified_user)]
BINARY = {"schema": {"type": "string", "format": "binary"}}


def uploads(request: Request) -> DocumentUploads:
    service = cast(DocumentUploads | None, request.app.state.documents)
    if service is None:
        raise HTTPException(503)
    return service


Service = Annotated[DocumentUploads, Depends(uploads)]


def pending(
    owner: Owner,
    service: Service,
    name: Annotated[
        str, Header(alias="X-Document-Name", min_length=1, max_length=1024)
    ],
    content_type: Annotated[str, Header(max_length=120)],
) -> Iterator[PendingUpload]:
    media_type = content_type.lower().replace("; charset=utf-8", "")
    try:
        with service.receive(unquote(name, errors="strict"), media_type) as upload:
            yield upload
    except InvalidDocument, UnicodeError:
        raise HTTPException(415) from None
    except UploadTooLarge:
        raise HTTPException(413) from None
    except OSError:
        raise HTTPException(503) from None


@router.post(
    "",
    responses={201: {"model": Document}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {media_type: BINARY for media_type in TYPES},
        }
    },
)
async def upload(
    request: Request,
    response: Response,
    owner: Owner,
    service: Service,
    staged: Annotated[PendingUpload, Depends(pending, scope="function")],
) -> Document:
    try:
        async with asyncio.timeout(60):
            async for chunk in request.stream():
                await run_in_threadpool(staged.write, chunk)
    except TimeoutError:
        raise HTTPException(408) from None
    document, created = await run_in_threadpool(service.finish, owner.user_id, staged)
    response.status_code = 201 if created else 200
    return document


@router.get("")
def documents(
    owner: Owner, service: Service, offset: Annotated[int, Query(ge=0, le=100000)] = 0
) -> list[Document]:
    return service.records.list(owner.user_id, offset)


@router.get("/{document_id}")
def status(document_id: UUID, owner: Owner, service: Service) -> Document:
    return service.records.get(owner.user_id, document_id)[0]


class DocumentDownload(StreamingResponse):
    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            with CancelScope(shield=True):
                assert self.background is not None
                await self.background()


@router.get(
    "/{document_id}/download",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/octet-stream": BINARY}}},
)
def download(document_id: UUID, owner: Owner, service: Service) -> StreamingResponse:
    document, key = service.records.get(owner.user_id, document_id)
    try:
        file = service.storage.open(key)
    except OSError:
        raise HTTPException(503) from None
    return DocumentDownload(
        iter(lambda: file.read(65536), b""),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(document.filename),
            "Content-Length": str(document.size_bytes),
        },
        background=BackgroundTask(file.close),
    )


@router.get("/{document_id}/proposals")
def proposals(
    document_id: UUID,
    owner: Owner,
    request: Request,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
) -> ProposalPage:
    service = cast(Proposals | None, request.app.state.proposals)
    if service is None:
        raise HTTPException(503)
    return service.read(owner.user_id, document_id, offset)
