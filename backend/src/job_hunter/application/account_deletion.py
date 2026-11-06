from typing import Protocol
from uuid import UUID


class AccountDeletion(Protocol):
    def delete(self, owner: UUID, session_digest: str) -> None: ...
