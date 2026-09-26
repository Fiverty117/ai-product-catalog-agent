import uuid
from collections.abc import Callable

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai.product_copy import (
    PermanentProductCopyProviderError,
    ProductCopyProvider,
    ProductCopyRequest,
    RetryableProductCopyProviderError,
)
from app.ai.prompts.product_copy_v1 import (
    PRODUCT_COPY_PROMPT as PRODUCT_COPY_PROMPT_V1,
    PROMPT_VERSION as PROMPT_VERSION_V1,
)
from app.ai.prompts.product_copy_v2 import (
    PRODUCT_COPY_PROMPT as PRODUCT_COPY_PROMPT_V2,
    PROMPT_VERSION as PROMPT_VERSION_V2,
)
from app.db.models import ProductCopyRun
from app.domain.schemas import ProductCopyJobPayload, ProductCopyResult
from app.services.jobs import PermanentJobError
from app.services.product_copy import (
    PRODUCT_COPY_JOB_TYPE,
    PRODUCT_COPY_SCHEMA_VERSION,
    UnknownProductCopyProductError,
    create_running_product_copy_run,
    mark_product_copy_run_failed,
    mark_product_copy_run_succeeded,
)
from app.workers.job_worker import ClaimedJob

SessionFactory = Callable[[], Session]
PRODUCT_COPY_PROMPTS = {
    PROMPT_VERSION_V1: PRODUCT_COPY_PROMPT_V1,
    PROMPT_VERSION_V2: PRODUCT_COPY_PROMPT_V2,
}


class InvalidProductCopyHandlerJob(PermanentJobError):
    pass


class ProductCopyJobHandler:
    def __init__(
        self,
        session_factory: SessionFactory,
        provider: ProductCopyProvider,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider

    def __call__(self, claimed: ClaimedJob) -> None:
        payload = self._validate_payload(claimed)
        run_id = self._prepare_attempt(claimed, payload)
        request = ProductCopyRequest(
            model=payload.model,
            prompt=PRODUCT_COPY_PROMPTS[payload.prompt_version],
            prompt_version=payload.prompt_version,
            input_snapshot=payload.input_snapshot,
            parameters=payload.parameters,
        )
        try:
            provider_result = self._provider.generate(request)
            structured_result = ProductCopyResult.model_validate(
                provider_result.structured_result
            )
        except ValidationError:
            error = RetryableProductCopyProviderError(
                "Product copy provider returned output that failed validation"
            )
            self._fail_attempt(run_id, error)
            raise error from None
        except Exception as error:
            safe_error = _safe_provider_error(error)
            self._fail_attempt(run_id, safe_error)
            raise safe_error from None

        with self._session_factory() as session:
            run = _require_run(session, run_id)
            mark_product_copy_run_succeeded(
                session,
                run,
                structured_result=structured_result,
                usage=provider_result.usage,
            )
            session.commit()

    def _validate_payload(self, claimed: ClaimedJob) -> ProductCopyJobPayload:
        if claimed.job_type != PRODUCT_COPY_JOB_TYPE:
            raise InvalidProductCopyHandlerJob(
                f"handler requires job type {PRODUCT_COPY_JOB_TYPE}"
            )
        try:
            payload = ProductCopyJobPayload.model_validate(claimed.payload)
        except ValidationError:
            raise InvalidProductCopyHandlerJob(
                "invalid Product copy job payload"
            ) from None
        if payload.provider != self._provider.name:
            raise InvalidProductCopyHandlerJob(
                "job provider does not match configured Product copy provider"
            )
        if payload.prompt_version not in PRODUCT_COPY_PROMPTS:
            raise InvalidProductCopyHandlerJob(
                f"unsupported Product copy prompt version: {payload.prompt_version}"
            )
        if payload.schema_version != PRODUCT_COPY_SCHEMA_VERSION:
            raise InvalidProductCopyHandlerJob(
                f"unsupported Product copy schema version: {payload.schema_version}"
            )
        return payload

    def _prepare_attempt(
        self,
        claimed: ClaimedJob,
        payload: ProductCopyJobPayload,
    ) -> uuid.UUID:
        with self._session_factory() as session:
            try:
                run = create_running_product_copy_run(
                    session,
                    payload=payload,
                    job_id=claimed.id,
                )
            except (ValueError, UnknownProductCopyProductError) as error:
                raise InvalidProductCopyHandlerJob(str(error)) from None
            session.commit()
            return run.id

    def _fail_attempt(self, run_id: uuid.UUID, error: Exception) -> None:
        with self._session_factory() as session:
            run = _require_run(session, run_id)
            mark_product_copy_run_failed(session, run, error=error)
            session.commit()


def product_copy_handlers(
    session_factory: SessionFactory,
    provider: ProductCopyProvider,
) -> dict[str, ProductCopyJobHandler]:
    return {PRODUCT_COPY_JOB_TYPE: ProductCopyJobHandler(session_factory, provider)}


def _safe_provider_error(error: Exception) -> Exception:
    if isinstance(
        error,
        (
            PermanentProductCopyProviderError,
            RetryableProductCopyProviderError,
            PermanentJobError,
        ),
    ):
        return error
    return RetryableProductCopyProviderError(
        "Product copy provider attempt failed"
    )


def _require_run(session: Session, run_id: uuid.UUID) -> ProductCopyRun:
    run = session.get(ProductCopyRun, run_id)
    if run is None:
        raise RuntimeError("Product copy run disappeared during execution")
    return run
