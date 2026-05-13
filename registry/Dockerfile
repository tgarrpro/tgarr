FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.115.0 \
    uvicorn[standard]==0.32.0 \
    asyncpg==0.30.0 \
    python-multipart==0.0.12

COPY . /app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090", "--proxy-headers"]
