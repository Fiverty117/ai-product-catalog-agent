import enum


class PhotoRole(str, enum.Enum):
    FRONT = "front"
    BACK = "back"
    SIDE = "side"
    NUTRITION = "nutrition"
    OTHER = "other"


class SKUFieldName(str, enum.Enum):
    EXTERNAL_SKU = "external_sku"
    FLAVOR = "flavor"
    SIZE_VALUE = "size_value"
    SIZE_UNIT = "size_unit"
    SERVINGS = "servings"


class FieldSource(str, enum.Enum):
    HUMAN = "human"
    MODEL = "model"
    OCR = "ocr"
    RULE = "rule"
    IMPORT = "import"


class FieldState(str, enum.Enum):
    PENDING = "pending"
    EXTRACTED = "extracted"
    VERIFIED = "verified"
    NOT_LEGIBLE = "not_legible"
    NOT_PRESENT = "not_present"
    FAILED = "failed"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ExtractionRunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DerivedImageReviewDecision(str, enum.Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class DerivedImageReviewState(str, enum.Enum):
    UNREVIEWED = "unreviewed"
    APPROVED = "approved"
    REJECTED = "rejected"


class PhotoPresentationAssetType(str, enum.Enum):
    ORIGINAL = "original"
    DERIVED = "derived"


class PhotoPresentationWarning(str, enum.Enum):
    PREFERRED_DERIVED_ASSET_MISSING = "preferred_derived_asset_missing"
    PREFERRED_DERIVED_SELECTION_INVALID = "preferred_derived_selection_invalid"


class ObservationState(str, enum.Enum):
    EXTRACTED = "extracted"
    NOT_LEGIBLE = "not_legible"
    NOT_PRESENT = "not_present"


class ExtractionReviewField(str, enum.Enum):
    FLAVOR = "flavor"
    SIZE = "size"
    SERVINGS = "servings"


class ExtractionReviewDecision(str, enum.Enum):
    ACCEPTED = "accepted"
    CORRECTED = "corrected"
    REJECTED = "rejected"


class IdentityResolutionAction(str, enum.Enum):
    USE_EXISTING = "use_existing"
    CREATE_NEW = "create_new"


class CategorySuggestionReviewDecision(str, enum.Enum):
    ACCEPTED = "accepted"
    CORRECTED = "corrected"
    REJECTED = "rejected"


class CatalogReadinessIssueCode(str, enum.Enum):
    MISSING_PRIMARY_CATEGORY = "missing_primary_category"
    INACTIVE_PRIMARY_CATEGORY = "inactive_primary_category"
    NO_SKUS = "no_skus"
    MISSING_CATALOG_FRONT_PHOTO = "missing_catalog_front_photo"
    MISSING_CATALOG_PHOTO_ASSET = "missing_catalog_photo_asset"
    NO_PUBLISHABLE_SKUS = "no_publishable_skus"
    MISSING_ACTIVE_APPROVED_PRICE = "missing_active_approved_price"
    INACTIVE_SECONDARY_CATEGORY = "inactive_secondary_category"
    SKU_EXCLUDED_MISSING_ACTIVE_PRICE = "sku_excluded_missing_active_price"


class CatalogReadinessIssueSeverity(str, enum.Enum):
    BLOCKER = "blocker"
    WARNING = "warning"


class CatalogReadinessIssueScope(str, enum.Enum):
    PRODUCT = "product"
    SKU = "sku"
    CATEGORY = "category"


class CatalogHeroPhotoSource(str, enum.Enum):
    PRODUCT = "product"
    SKU = "sku"
