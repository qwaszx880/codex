import os
import socket
import time
from datetime import datetime, timezone

from sqlalchemy import select

from platform_service.config import get_settings
from platform_service.infrastructure.database import (
    ExecutorInstance,
    ManagementCluster,
    SessionLocal,
)


def beat() -> None:
    settings = get_settings()
    executor_id = os.getenv("EXECUTOR_ID", socket.gethostname())
    with SessionLocal.begin() as session:
        management = session.scalar(
            select(ManagementCluster).where(ManagementCluster.name == settings.management_cluster)
        )
        if management is None:
            return
        instance = session.scalar(
            select(ExecutorInstance).where(ExecutorInstance.executor_id == executor_id)
        )
        values = {
            "management_cluster_id": management.id,
            "executor_version": "0.1.0-local",
            "capi_version": "v1beta1-fake",
            "capo_version": "v1beta1-fake",
            "status": "READY",
            "last_seen": datetime.now(timezone.utc),
        }
        if instance is None:
            session.add(ExecutorInstance(executor_id=executor_id, **values))
        else:
            for key, value in values.items():
                setattr(instance, key, value)


def main() -> None:
    while True:
        beat()
        time.sleep(10)


if __name__ == "__main__":
    main()
