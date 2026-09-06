FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV CONFIG_PATH=/app/config.yaml DB_PATH=/data/bloom.db
EXPOSE 8000
CMD ["python", "-m", "collector.main"]
