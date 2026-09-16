"""Transactional-outbox relay that publishes Celery protocol messages."""

import os
import socket
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from platform_service.config import get_settings
from platform_service.infrastructure.database import OutboxEvent, SessionLocal
from platform_service.workers.celery_app import celery_app


class OutboxPublisher:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.owner = f"{socket.gethostname()}:{os.getpid()}"

    def _claim(self) -> list:
        now = datetime.now(timezone.utc)
        stale = now - timedelta(minutes=5)
        with SessionLocal.begin() as session:
            events = session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.published_at.is_(None),
                    or_(OutboxEvent.locked_at.is_(None), OutboxEvent.locked_at < stale),
                )
                .order_by(OutboxEvent.created_at)
                .limit(self.settings.outbox_batch_size)
                .with_for_update(skip_locked=True)
            ).all()
            ids = []
            for event in events:
                event.attempts += 1
                event.locked_by = self.owner
                event.locked_at = now
                ids.append(event.id)
            return ids

    def publish_batch(self) -> int:
        event_ids = self._claim()
        published = 0
        for event_id in event_ids:
            with SessionLocal() as session:
                event = session.get(OutboxEvent, event_id)
                try:
                    celery_app.send_task(
                        "platform.reconcile",
                        args=[event.payload],
                        queue=event.routing_key,
                        routing_key=event.routing_key,
                        retry=True,
                        retry_policy={
                            "max_retries": 3,
                            "interval_start": 1,
                            "interval_step": 2,
                            "interval_max": 10,
                        },
                    )
                    event.published_at = datetime.now(timezone.utc)
                    event.last_error = None
                    event.locked_at = None
                    event.locked_by = None
                    published += 1
                except Exception as exc:
                    event.last_error = str(exc)[:2000]
                    event.locked_at = None
                    event.locked_by = None
                session.commit()
        return published


def main() -> None:
    publisher = OutboxPublisher()
    while True:
        count = publisher.publish_batch()
        time.sleep(0 if count else 1)


if __name__ == "__main__":
    main()
