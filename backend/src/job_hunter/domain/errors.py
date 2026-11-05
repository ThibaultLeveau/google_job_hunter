from typing import Literal


class DomainError(Exception):
    def __init__(self, code: Literal["not_found", "conflict"]) -> None:
        self.code = code
        super().__init__(code)
