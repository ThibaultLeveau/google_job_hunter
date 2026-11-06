import hashlib
import json
import logging
import math
import warnings
from collections.abc import Callable
from typing import Any, Literal, Self

import httpx
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langsmith import tracing_context
from openai import APITimeoutError, LengthFinishReasonError, RateLimitError
from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

from job_hunter.application.ai import AI, Accounting, AIError, ErrorCode, Limits, Reply
from job_hunter.infrastructure.provider_budgets import BudgetAccounting
from job_hunter.settings import ConfigurationError


class AISettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOB_HUNTER_AI_", frozen=True, hide_input_in_errors=True
    )
    provider: Literal["disabled", "openai", "fixture"] = "disabled"
    environment: Literal["production", "development", "test"] = "production"
    api_key: SecretStr | None = None
    chat_model: str = Field(default="", pattern=r"^[a-zA-Z0-9_.:/-]{0,100}$")
    embedding_model: str = Field(default="", pattern=r"^[a-zA-Z0-9_.:/-]{0,100}$")
    temperature: float | None = Field(default=None, ge=0, le=2)
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="after")
    def configured(self) -> Self:
        if self.provider == "fixture" and self.environment == "production":
            raise ValueError("Fixtures require explicit development/test mode.")
        if self.provider == "openai" and (
            not self.chat_model
            or not self.embedding_model
            or not self.api_key
            or not self.api_key.get_secret_value().strip()
        ):
            raise ValueError("Configure both model IDs and the provider credential.")
        return self


def load_ai_settings() -> AISettings:
    try:
        return AISettings()
    except ValidationError, SettingsError:
        raise ConfigurationError(
            "Invalid AI settings. Check JOB_HUNTER_AI_ provider, environment, "
            "api_key, model IDs and limits."
        ) from None


class LangChainProvider:
    def __init__(
        self, settings: AISettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if settings.provider != "openai":
            raise ConfigurationError(
                "OpenAI adapter requires configured OpenAI settings."
            )
        self.settings, self.transport = settings, transport
        # The SDK's DEBUG request diagnostics can contain private prompt text.
        logging.getLogger("openai._base_client").disabled = True
        warnings.filterwarnings(
            "error", message="Pydantic serializer warnings:", category=UserWarning
        )

    async def request(
        self,
        prompt: str | None,
        source: str,
        schema: dict[str, Any],
        texts: tuple[str, ...],
    ) -> Reply:
        settings, usage = self.settings, dict[str, int]()

        def reply(data: object, error: ErrorCode | None = None) -> Reply:
            return Reply(
                data,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens", 0 if prompt is None else None),
                error,
            )

        async def capture(response: httpx.Response) -> None:
            await response.aread()
            if response.is_success:
                body = response.json()
                if prompt is None:
                    entries = body.get("data", [])
                    if len(entries) != len(texts) or any(
                        row.get("index") != index
                        or len(row.get("embedding", [])) != settings.limits.dimensions
                        or any(
                            type(x) not in (int, float) or not math.isfinite(x)
                            for x in row["embedding"]
                        )
                        for index, row in enumerate(entries)
                    ):
                        raise AIError("invalid_output")
                usage.update(
                    {
                        key: value
                        for key, value in body.get("usage", {}).items()
                        if key in {"prompt_tokens", "completion_tokens"}
                        and type(value) is int
                        and value >= 0
                    }
                )

        try:
            with tracing_context(enabled=False), httpx.Client(trust_env=False) as sync:
                async with httpx.AsyncClient(
                    transport=self.transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=settings.limits.seconds,
                    event_hooks={"response": [capture]},
                ) as client:
                    common: dict[str, Any] = dict(
                        api_key=settings.api_key,
                        base_url="https://api.openai.com/v1",
                        max_retries=0,
                        timeout=settings.limits.seconds,
                        http_client=sync,
                        http_async_client=client,
                    )
                    if prompt is not None:
                        model = ChatOpenAI(
                            **common,
                            model=settings.chat_model,
                            temperature=settings.temperature,
                            max_completion_tokens=settings.limits.output_tokens,
                            use_responses_api=False,
                            store=False,
                            cache=False,
                            verbose=False,
                            callbacks=[],
                        )
                        raw = await model.ainvoke(
                            [("system", prompt), ("human", source)],
                            config={"callbacks": []},
                            response_format={
                                "type": "json_schema",
                                "json_schema": {
                                    "name": schema["title"],
                                    "strict": True,
                                    "schema": schema,
                                },
                            },
                        )
                        refused = (
                            raw.tool_calls
                            or raw.invalid_tool_calls
                            or raw.additional_kwargs.get("refusal")
                        )
                        if refused:
                            return reply(None, "refused")
                        try:
                            data = (
                                json.loads(raw.content)
                                if isinstance(raw.content, str)
                                and raw.response_metadata.get("finish_reason") == "stop"
                                else None
                            )
                        except ValueError:
                            data = None
                    else:
                        model_embedding = OpenAIEmbeddings(
                            **common,
                            model=settings.embedding_model,
                            dimensions=settings.limits.dimensions,
                            check_embedding_ctx_length=False,
                            chunk_size=16,
                            model_kwargs={"encoding_format": "float"},
                        )
                        data = await model_embedding.aembed_documents(list(texts))
                    return reply(data)
        except LengthFinishReasonError:
            return reply(None, "invalid_output")
        except RateLimitError:
            raise AIError("rate_limited") from None
        except APITimeoutError:
            raise AIError("timeout") from None
        except AIError:
            raise
        except Exception:
            raise AIError("provider_failure") from None

    async def generate(self, prompt: str, source: str, schema: dict[str, Any]) -> Reply:
        return await self.request(prompt, source, schema, ())

    async def embed(self, texts: tuple[str, ...]) -> Reply:
        return await self.request(None, "", {}, texts)


class FixtureProvider:
    def __init__(self, settings: AISettings, response: str) -> None:
        if settings.provider != "fixture" or settings.environment == "production":
            raise ConfigurationError(
                "Fixture adapter requires explicit development/test settings."
            )
        self.response, self.dimensions = response, settings.limits.dimensions

    async def generate(self, prompt: str, source: str, schema: dict[str, Any]) -> Reply:
        return Reply(json.loads(self.response), 0, 0)

    async def embed(self, texts: tuple[str, ...]) -> Reply:
        return Reply(
            [
                [
                    (
                        hashlib.sha256(f"{index}:".encode() + text.encode()).digest()[0]
                        - 127.5
                    )
                    / 127.5
                    for index in range(self.dimensions)
                ]
                for text in texts
            ],
            0,
            0,
        )


def create_ai(
    settings: AISettings,
    accounting: Accounting,
    *,
    cancelled: Callable[[], bool] = lambda: False,
    fixture_response: str | None = None,
) -> AI:
    provider: LangChainProvider | FixtureProvider | None = None
    if settings.provider == "openai":
        provider = LangChainProvider(settings)
    elif settings.provider == "fixture":
        if fixture_response is None:
            raise ConfigurationError("Supply an explicit fixture response.")
        provider = FixtureProvider(settings, fixture_response)
    return AI(
        provider,
        provider,
        BudgetAccounting(accounting, required=settings.provider == "openai"),
        settings.limits,
        settings.chat_model or "fixture-chat-v1",
        settings.embedding_model or "fixture-embedding-v1",
        cancelled=cancelled,
    )
