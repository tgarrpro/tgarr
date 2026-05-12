FROM python:3.12-slim
WORKDIR /app

# tgcrypto is a C extension — install gcc to build the wheel, then strip
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc python3-dev \
    && pip install --no-cache-dir \
        pyrogram==2.0.106 \
        tgcrypto==1.2.5 \
        asyncpg==0.30.0 \
        meilisearch==0.31.6 \
        python-dotenv==1.0.1 \
    && apt-get purge -y --auto-remove gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY . /app
RUN mkdir -p /app/session

CMD ["python", "-u", "main.py"]
