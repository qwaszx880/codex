from fastapi import FastAPI
from prometheus_client import make_asgi_app

from platform_service.api.routes import router

app = FastAPI(title="Cluster Control Plane", version="0.1.0")
app.include_router(router, prefix="/v1")
app.mount("/metrics", make_asgi_app())


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
