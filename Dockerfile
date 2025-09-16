FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tini ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 TZ=Asia/Seoul
WORKDIR /app

# (선택) 진단용: 빌드 시 포함된 requirements.txt 내용 확인
COPY requirements.txt .  # 파일이 없어도 빌드는 계속됨
RUN echo "===== requirements.txt (if any) =====" ; \
    (test -f requirements.txt && cat requirements.txt) || echo "NO requirements.txt"; \
    echo "====================================="

# ✅ 어떤 경우에도 필요한 것들 설치 (requirements가 비어있어도 통과)
RUN pip install --no-cache-dir \
    discord.py==2.4.0 \
    asyncpg==0.29.0 \
    python-dotenv==1.0.0

# 설치 확인
RUN python - <<'PY'
import importlib, sys
for m in ("asyncpg","discord","dotenv"):
    importlib.import_module(m)
    print("OK", m)
PY

COPY bot.py .

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python","bot.py"]
