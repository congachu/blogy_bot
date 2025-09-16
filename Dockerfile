FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tini ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 TZ=Asia/Seoul
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python","bot.py"]
