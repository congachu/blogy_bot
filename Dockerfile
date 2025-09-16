FROM python:3.11-slim

# 기본 패키지
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Seoul

WORKDIR /app

# 의존성
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱
COPY bot.py .

# 데이터 디렉토리(볼륨 마운트 위치)
VOLUME ["/data"]

# 헬스체크는 선택
# HEALTHCHECK CMD pgrep -f "python bot.py" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "bot.py"]
