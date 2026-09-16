from enum import StrEnum


class ErrorClass(StrEnum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    STALE = "STALE"
    CONFLICT = "CONFLICT"
    DUPLICATE = "DUPLICATE"


class ReconciliationError(Exception):
    classification = ErrorClass.PERMANENT


class TransientError(ReconciliationError):
    classification = ErrorClass.TRANSIENT


class StaleOperation(ReconciliationError):
    classification = ErrorClass.STALE


class DuplicateEvent(ReconciliationError):
    classification = ErrorClass.DUPLICATE


class OperationConflict(ReconciliationError):
    classification = ErrorClass.CONFLICT
