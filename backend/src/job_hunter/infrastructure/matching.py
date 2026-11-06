from job_hunter.application.embeddings import EmbedJobs
from job_hunter.application.matching import EvaluateMatch
from job_hunter.infrastructure.ai import AISettings, create_ai


def create_matcher(
    embeddings: EmbedJobs, settings: AISettings, fixture_response: str | None = None
) -> EvaluateMatch:
    return EvaluateMatch(
        embeddings,
        lambda ledger: create_ai(settings, ledger, fixture_response=fixture_response),
        settings.model_dump_json(exclude={"api_key", "environment", "embedding_model"}),
    )
