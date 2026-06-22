# syntax=docker/dockerfile:1.7
#
# compass-worker:base — internal framework image. NOT deployed directly.
# Use compass-worker:performance, :cost, or :safety for actual workers.
#
# Build:
#   cd E:\8thelement\Compass
#   docker build -f compass-workers/Dockerfile -t compass-worker:base .

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Lens-framework deps. Same for Performance, Cost, and Safety.
RUN pip install --no-cache-dir \
        "clickhouse-connect>=0.7" \
        "psycopg[binary]>=3.1" \
        "python-dotenv>=1.0" \
        "pydantic_settings" \
        "prometheus_client>=0.20"

WORKDIR /app
COPY compass-workers/compass_worker /app/compass_worker
COPY compass-workers/run_worker.py /app/run_worker.py

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

ENTRYPOINT ["python", "run_worker.py"]
# No CMD here — children set their own; this image isn't deployed.