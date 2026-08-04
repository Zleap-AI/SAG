FROM python:3.12-slim AS builder

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

ARG SAG_API_EXTRAS=""
ARG PIP_INDEX_URL=""

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN if [ -n "$PIP_INDEX_URL" ]; then export PIP_INDEX_URL; fi; pip install --upgrade pip hatchling \
 && python -c 'import os, tomllib; p=tomllib.load(open("pyproject.toml", "rb")); deps=list(p["project"]["dependencies"]); deps += p["project"]["optional-dependencies"].get(os.environ.get("SAG_API_EXTRAS", ""), []); print("\n".join(deps))' > /tmp/requirements.txt \
 && pip install --prefix=/install --ignore-installed packaging -r /tmp/requirements.txt

COPY sag_api ./sag_api
COPY sag_agent ./sag_agent
RUN pip install --prefix=/install --no-deps --no-build-isolation .

FROM python:3.12-slim AS runtime

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/usr/local/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/v1/system/health || exit 1

CMD ["uvicorn", "sag_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
