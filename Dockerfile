FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends tini ca-certificates tzdata && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 TZ=Asia/Seoul
WORKDIR /app

# 👉 requirements 복사 + 설치 (설치 확인까지 강제)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && python - <<'PY'
import importlib, sys
for m in ("asyncpg","discord","dotenv"):
    try:
        importlib.import_module(m)
        print(f"OK {m}")
    except Exception as e:
        print(f"FAIL {m} -> {e}", file=sys.stderr); sys.exit(1)
PY

COPY bot.py .

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python","bot.py"]
