from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import SKU, SKUFieldProvenance
from app.domain.enums import FieldSource, FieldState, SKUFieldName

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

    with session.no_autoflush:
        provenance = next(
            (
                record
                for record in sku.field_provenance
                if record.field_name == resolved_field
            ),
            None,
        )
        if provenance is None:
            provenance = session.scalar(
                select(SKUFieldProvenance).where(
                    SKUFieldProvenance.sku_id == sku.id,
                    SKUFieldProvenance.field_name == resolved_field,
                )
            )

    if (
        provenance is not None
        and provenance.locked
        and resolved_source is not FieldSource.HUMAN
    ):
        raise LockedSKUFieldError(
            f"cannot apply {resolved_source.value} update to locked field "
            f"{resolved_field.value}"
        )

    if provenance is None:
        provenance = SKUFieldProvenance(
            sku=sku,
            field_name=resolved_field,
            source=resolved_source,
            state=state,
        )
        session.add(provenance)

    setattr(sku, resolved_field.value, value)

    if resolved_source is FieldSource.HUMAN:
        provenance.source = FieldSource.HUMAN
        provenance.state = FieldState.VERIFIED
        provenance.locked = True
        provenance.confidence = None
    else:
        provenance.source = resolved_source
        provenance.state = state
        provenance.locked = False
        provenance.confidence = confidence

    provenance.evidence = evidence
    return provenance
