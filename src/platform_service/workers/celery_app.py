from celery import Celery
from kombu import Exchange, Queue

from platform_service.config import get_settings

settings = get_settings()
command_exchange = Exchange("cluster.commands", type="direct", durable=True)
celery_app = Celery("platform", broker=settings.rabbitmq_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"confirm_publish": True},
    task_default_exchange=command_exchange.name,
    task_default_exchange_type="direct",
    task_queues=(
        Queue(
            settings.management_cluster,
            command_exchange,
            routing_key=settings.management_cluster,
            durable=True,
            queue_arguments={
                "x-queue-type": "quorum",
                "x-dead-letter-exchange": "cluster.commands.dlx",
            },
        ),
    ),
    task_routes={
        "platform.reconcile": {
            "queue": settings.management_cluster,
            "routing_key": settings.management_cluster,
        }
    },
)
