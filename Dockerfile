FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests
COPY alembic alembic
COPY alembic.ini ./
RUN pip install --no-cache-dir '.[dev]'
CMD ["uvicorn","platform_service.main:app","--host","0.0.0.0","--port","8000"]
