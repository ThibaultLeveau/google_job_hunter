import base64
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from job_hunter.domain.tasks import require_utc

PHASES = (
    "sessions",
    "verification",
    "recovery",
    "oidc",
    "rates",
    "verification_mail",
    "recovery_mail",
    "tasks",
    "documents",
    "accounts",
    "files",
    "artifacts",
)


class RetentionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    action_hours: int = Field(default=24, ge=0, le=87600)
    session_hours: int = Field(default=24, ge=0, le=87600)
    rate_hours: int = Field(default=1, ge=0, le=87600)
    mail_hours: int = Field(default=0, ge=0, le=87600)
    file_hours: int = Field(default=24, ge=1, le=87600)
    task_hours: int = Field(default=720, ge=1, le=87600)
    retry_hours: int = Field(default=1, ge=1, le=87600)
    artifact_days: int = Field(default=30, ge=1, le=3650)
    batch_size: int = Field(default=20, ge=1, le=100)


class RetentionCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: int = 1
    started: datetime
    policy: RetentionPolicy
    dry_run: bool
    phase: int = Field(default=0, ge=0, lt=len(PHASES))
    after: str | None = None

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def start(
        cls, now: datetime, policy: RetentionPolicy, dry_run: bool, token: str | None
    ) -> RetentionCheckpoint:
        require_utc(now)
        if token is None:
            return cls(started=now, policy=policy, dry_run=dry_run)
        try:
            if len(token) > 4096:
                raise ValueError()
            value = cls.model_validate_json(
                base64.b64decode(token, altchars=b"-_", validate=True)
            )
            require_utc(value.started)
            if (
                value.version != 1
                or value.started > now
                or value.policy != policy
                or value.dry_run != dry_run
            ):
                raise ValueError()
            return value
        except ValueError:
            raise ValueError("Invalid retention checkpoint.") from None
