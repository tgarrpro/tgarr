FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir \
    pyrogram==2.0.106 \
    tgcrypto==1.2.5 \
    asyncpg==0.30.0 \
    meilisearch==0.31.6 \
    python-dotenv==1.0.1

COPY . /app
RUN mkdir -p /app/session

CMD ["python", "-u", "main.py"]
