import os

from platform_service.application.reconciliation import CommandProcessor
from platform_service.domain.errors import ErrorClass, ReconciliationError
from platform_service.infrastructure.compiler import CapoCompiler
from platform_service.infrastructure.database import SessionLocal
from platform_service.workers.celery_app import celery_app


class FakeManagementAdapter:
    """Local adapter implementing the production port and command contract."""

    def apply(self, resources):
        mode = os.getenv("FAKE_FAILURE_MODE", "")
        if mode == "permanent":
            raise ValueError("simulated provider rejection")
        if mode == "transient":
            raise ConnectionError("simulated management API outage")

    def delete(self, namespace, name):
        return None


@celery_app.task(name="platform.reconcile", bind=True, max_retries=6)
def reconcile(self, message: dict):
    """Thin Celery entry point; reconciliation policy lives in CommandProcessor."""

    session = SessionLocal()
    try:
        return CommandProcessor(
            session, CapoCompiler(), FakeManagementAdapter(), os.getenv("HOSTNAME", "executor")
        ).process(message)
    except ReconciliationError as exc:
        session.rollback()
        if exc.classification in {ErrorClass.STALE, ErrorClass.DUPLICATE}:
            return {"result": exc.classification}
        if exc.classification == ErrorClass.CONFLICT:
            raise self.retry(exc=exc, countdown=min(2**self.request.retries, 60))
        raise
    except ConnectionError as exc:
        session.rollback()
        raise self.retry(exc=exc, countdown=min(2**self.request.retries, 300))
    finally:
        session.close()
