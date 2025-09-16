FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends tini ca-certificates tzdata && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 TZ=Asia/Seoul
WORKDIR /app

# 실제 requirements.txt를 복사
COPY requirements.txt .
# 설치 + 설치 확인
RUN pip install --no-cache-dir -r requirements.txt && python - <<'PY'
import importlib, sys
for m in ("asyncpg","discord","dotenv"):
    try:
        importlib.import_module(m)
        print("OK", m)
    except Exception as e:
        print("FAIL", m, "->", e, file=sys.stderr); sys.exit(1)
PY

# 앱 코드
COPY bot.py .

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python","bot.py"]
