FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tini ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 TZ=Asia/Seoul
WORKDIR /app

COPY requirements.txt .
RUN echo "===== requirements.txt (if any) =====" ; \
    (test -f requirements.txt && cat requirements.txt) || echo "NO requirements.txt"; \
    echo "====================================="

RUN pip install --no-cache-dir \
    discord.py==2.4.0 \
    asyncpg==0.29.0 \
    python-dotenv==1.0.0

RUN python - <<'PY'
import importlib, sys
for m in ("asyncpg","discord","dotenv"):
    importlib.import_module(m)
    print("OK", m)
PY

COPY bot.py .

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python","bot.py"]
