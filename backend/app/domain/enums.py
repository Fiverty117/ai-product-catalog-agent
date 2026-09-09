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
