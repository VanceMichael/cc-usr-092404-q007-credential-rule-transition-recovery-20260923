FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY migrations ./migrations
ENV DATABASE_PATH=/app/data/skills.sqlite3
CMD ["sh", "-c", "python -m alembic upgrade head && uvicorn skill_engine:app --host 0.0.0.0 --port ${PORT:-8080}"]
