# recall server — Cloudflare Containers image (also runs anywhere Docker does)
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY recall_pipeline ./recall_pipeline
COPY recall_server ./recall_server
RUN pip install --no-cache-dir ".[r2]"

# Container disk is ephemeral — R2 (RECALL_R2_* env) is the durable layer.
ENV RECALL_DATA_ROOT=/data
EXPOSE 8300
CMD ["uvicorn", "recall_server.app:app", "--host", "0.0.0.0", "--port", "8300"]
