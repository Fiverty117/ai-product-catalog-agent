import enum


class PhotoRole(str, enum.Enum):
    FRONT = "front"
    BACK = "back"
    SIDE = "side"
    NUTRITION = "nutrition"
    OTHER = "other"
