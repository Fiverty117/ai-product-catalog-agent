from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ExtractionRun, SKU, SKUFieldProvenance
from app.domain.enums import (
    ExtractionRunStatus,
    FieldSource,
    FieldState,
    SKUFieldName,
)

SKUFieldValue = str | Decimal | int | None


class LockedSKUFieldError(ValueError):
    pass


class InvalidSKUFieldError(ValueError):
    pass


def apply_sku_field_update(
    session: Session,
    *,
    sku: SKU,
    field_name: SKUFieldName | str,
    value: SKUFieldValue,
    source: FieldSource | str,
    state: FieldState = FieldState.EXTRACTED,
    confidence: Decimal | None = None,
    evidence: str | None = None,
) -> SKUFieldProvenance:
    """Apply one canonical SKU field update and its provenance metadata."""

    try:
        resolved_field = SKUFieldName(field_name)
    except ValueError as exc:
        raise InvalidSKUFieldError(f"unsupported SKU field: {field_name}") from exc

    resolved_source = FieldSource(source)
    if confidence is not None and not Decimal("0") <= confidence <= Decimal("1"):
        raise ValueError("confidence must be between 0 and 1 inclusive")

    provenance = _find_provenance(session, sku, resolved_field)

    if (
        provenance is not None
        and provenance.locked
        and resolved_source is not FieldSource.HUMAN
    ):
        raise LockedSKUFieldError(
            f"cannot apply {resolved_source.value} update to locked field "
            f"{resolved_field.value}"
        )

    if resolved_source is FieldSource.HUMAN:
        return _apply_update(
            session,
            sku=sku,
            provenance=provenance,
            field_name=resolved_field,
            value=value,
            source=FieldSource.HUMAN,
            state=FieldState.VERIFIED,
            locked=True,
            confidence=None,
            evidence=evidence,
            extraction_run=None,
        )
    return _apply_update(
        session,
        sku=sku,
        provenance=provenance,
        field_name=resolved_field,
        value=value,
        source=resolved_source,
        state=state,
        locked=False,
        confidence=confidence,
        evidence=evidence,
        extraction_run=None,
    )


def apply_reviewed_sku_field_update(
    session: Session,
    *,
    sku: SKU,
    field_name: SKUFieldName,
    value: SKUFieldValue,
    source: FieldSource,
    confidence: Decimal | None = None,
    evidence: str | None = None,
    extraction_run: ExtractionRun | None = None,
    replace_locked: bool = False,
) -> SKUFieldProvenance:
    """Apply one explicitly human-reviewed value as verified and locked."""

    if source not in {FieldSource.MODEL, FieldSource.HUMAN}:
        raise ValueError("reviewed updates must have model or human source")
    if confidence is not None and not Decimal("0") <= confidence <= Decimal("1"):
        raise ValueError("confidence must be between 0 and 1 inclusive")
    if source is FieldSource.MODEL:
        if extraction_run is None:
            raise ValueError("accepted model values require an extraction run")
        if extraction_run.status is not ExtractionRunStatus.SUCCEEDED:
            raise ValueError("accepted model values require a successful extraction run")
    elif extraction_run is not None:
        raise ValueError("human-corrected values cannot claim model run lineage")

    provenance = _find_provenance(session, sku, field_name)
    if provenance is not None and provenance.locked and not replace_locked:
        raise LockedSKUFieldError(
            f"cannot replace locked field {field_name.value} without explicit approval"
        )

    return _apply_update(
        session,
        sku=sku,
        provenance=provenance,
        field_name=field_name,
        value=value,
        source=source,
        state=FieldState.VERIFIED,
        locked=True,
        confidence=confidence if source is FieldSource.MODEL else None,
        evidence=evidence,
        extraction_run=extraction_run,
    )


def _find_provenance(
    session: Session,
    sku: SKU,
    field_name: SKUFieldName,
) -> SKUFieldProvenance | None:
    with session.no_autoflush:
        provenance = next(
            (
                record
                for record in sku.field_provenance
                if record.field_name == field_name
            ),
            None,
        )
        if provenance is None:
            provenance = session.scalar(
                select(SKUFieldProvenance).where(
                    SKUFieldProvenance.sku_id == sku.id,
                    SKUFieldProvenance.field_name == field_name,
                )
            )
    return provenance


def _apply_update(
    session: Session,
    *,
    sku: SKU,
    provenance: SKUFieldProvenance | None,
    field_name: SKUFieldName,
    value: SKUFieldValue,
    source: FieldSource,
    state: FieldState,
    locked: bool,
    confidence: Decimal | None,
    evidence: str | None,
    extraction_run: ExtractionRun | None,
) -> SKUFieldProvenance:
    if provenance is None:
        provenance = SKUFieldProvenance(
            sku=sku,
            field_name=field_name,
            source=source,
            state=state,
        )
        session.add(provenance)

    setattr(sku, field_name.value, value)
    provenance.source = source
    provenance.state = state
    provenance.locked = locked
    provenance.confidence = confidence
    provenance.evidence = evidence
    provenance.extraction_run = extraction_run
    return provenance
