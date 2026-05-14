FROM python:3.12-slim
WORKDIR /app

ENV QTWEBENGINE_DISABLE_SANDBOX=1 \
    QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu"

# tgcrypto needs gcc for the C extension wheel build.
# calibre + Noto fonts: serve any ebook format as PDF in the browser.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc python3-dev \
    && pip install --no-cache-dir \
        fastapi==0.115.0 \
        uvicorn[standard]==0.32.0 \
        asyncpg==0.30.0 \
        python-multipart==0.0.12 \
        pyrogram==2.0.106 \
        tgcrypto==1.2.5 \
        qrcode[pil]==7.4.2 \
    && apt-get purge -y --auto-remove gcc python3-dev \
    && apt-get install -y --no-install-recommends \
        calibre \
        fonts-noto-core \
        fonts-noto-cjk \
        fonts-noto-extra \
        fonts-noto-color-emoji \
        fonts-wqy-microhei \
    && fc-cache -fv > /dev/null \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8088", "--proxy-headers"]
