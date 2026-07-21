"""Domain exceptions mapped to safe API responses."""


class OCRServiceError(Exception):
    """Base class for expected service errors."""


class UnsupportedInputError(OCRServiceError):
    pass


class InputLimitError(OCRServiceError):
    pass


class InvalidInputError(OCRServiceError):
    pass


class WorkflowResultError(OCRServiceError):
    pass


class ResourceExhaustedError(OCRServiceError):
    pass


class QueueFullError(OCRServiceError):
    pass
