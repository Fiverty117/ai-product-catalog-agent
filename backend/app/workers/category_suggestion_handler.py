import uuid
from collections.abc import Callable

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai.category_suggestion import (
    CategorySuggestionProvider,
    CategorySuggestionRequest,
    PermanentCategorySuggestionProviderError,
    RetryableCategorySuggestionProviderError,
)
from app.ai.prompts.product_category_v1 import PRODUCT_CATEGORY_PROMPT, PROMPT_VERSION
from app.db.models import CategorySuggestionRun
from app.domain.schemas import CategorySuggestionJobPayload, CategorySuggestionResult
from app.services.category_suggestions import (
    PRODUCT_CATEGORY_SCHEMA_VERSION,
    PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE,
    UnknownCategorySuggestionProductError,
    create_running_category_suggestion_run,
    mark_category_suggestion_run_failed,
    mark_category_suggestion_run_succeeded,
)
from app.services.jobs import PermanentJobError
from app.workers.job_worker import ClaimedJob

SessionFactory = Callable[[], Session]


class InvalidCategorySuggestionHandlerJob(PermanentJobError):
    pass


class ProductCategorySuggestionJobHandler:
    def __init__(
        self,
        session_factory: SessionFactory,
        provider: CategorySuggestionProvider,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider

    def __call__(self, claimed: ClaimedJob) -> None:
        payload = self._validate_payload(claimed)
        run_id = self._prepare_attempt(claimed, payload)
        request = CategorySuggestionRequest(
            model=payload.model,
            prompt=PRODUCT_CATEGORY_PROMPT,
            input_snapshot=payload.input_snapshot,
            parameters=payload.parameters,
        )
        try:
            provider_result = self._provider.suggest(request)
            taxonomy_ids = {
                item.category_id for item in payload.input_snapshot.taxonomy
            }
            structured_result = CategorySuggestionResult.model_validate(
                provider_result.structured_result,
                context={"taxonomy_ids": taxonomy_ids},
            )
        except ValidationError:
            error = RetryableCategorySuggestionProviderError(
                "category provider returned output that failed validation"
            )
            self._fail_attempt(run_id, error)
            raise error from None
        except Exception as error:
            safe_error = _safe_provider_error(error)
            self._fail_attempt(run_id, safe_error)
            raise safe_error from None

        with self._session_factory() as session:
            run = _require_run(session, run_id)
            mark_category_suggestion_run_succeeded(
                session,
                run,
                structured_result=structured_result,
                usage=provider_result.usage,
            )
            session.commit()

    def _validate_payload(
        self,
        claimed: ClaimedJob,
    ) -> CategorySuggestionJobPayload:
        if claimed.job_type != PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE:
            raise InvalidCategorySuggestionHandlerJob(
                f"handler requires job type {PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE}"
            )
        try:
            payload = CategorySuggestionJobPayload.model_validate(claimed.payload)
        except ValidationError:
            raise InvalidCategorySuggestionHandlerJob(
                "invalid product category suggestion job payload"
            ) from None
        if payload.provider != self._provider.name:
            raise InvalidCategorySuggestionHandlerJob(
                "job provider does not match configured category provider"
            )
        if payload.prompt_version != PROMPT_VERSION:
            raise InvalidCategorySuggestionHandlerJob(
                f"unsupported category prompt version: {payload.prompt_version}"
            )
        if payload.schema_version != PRODUCT_CATEGORY_SCHEMA_VERSION:
            raise InvalidCategorySuggestionHandlerJob(
                f"unsupported category schema version: {payload.schema_version}"
            )
        return payload

    def _prepare_attempt(
        self,
        claimed: ClaimedJob,
        payload: CategorySuggestionJobPayload,
    ) -> uuid.UUID:
        with self._session_factory() as session:
            try:
                run = create_running_category_suggestion_run(
                    session,
                    payload=payload,
                    job_id=claimed.id,
                )
            except (ValueError, UnknownCategorySuggestionProductError) as error:
                raise InvalidCategorySuggestionHandlerJob(str(error)) from None
            session.commit()
            return run.id

    def _fail_attempt(self, run_id: uuid.UUID, error: Exception) -> None:
        with self._session_factory() as session:
            run = _require_run(session, run_id)
            mark_category_suggestion_run_failed(session, run, error=error)
            session.commit()


def category_suggestion_handlers(
    session_factory: SessionFactory,
    provider: CategorySuggestionProvider,
) -> dict[str, ProductCategorySuggestionJobHandler]:
    return {
        PRODUCT_CATEGORY_SUGGESTION_JOB_TYPE: ProductCategorySuggestionJobHandler(
            session_factory,
            provider,
        )
    }


def _safe_provider_error(error: Exception) -> Exception:
    if isinstance(
        error,
        (
            PermanentCategorySuggestionProviderError,
            RetryableCategorySuggestionProviderError,
            PermanentJobError,
        ),
    ):
        return error
    return RetryableCategorySuggestionProviderError(
        "category suggestion provider attempt failed"
    )


def _require_run(session: Session, run_id: uuid.UUID) -> CategorySuggestionRun:
    run = session.get(CategorySuggestionRun, run_id)
    if run is None:
        raise RuntimeError("category suggestion run disappeared during execution")
    return run
