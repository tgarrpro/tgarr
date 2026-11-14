FROM python:3.12-slim
WORKDIR /app

ENV QTWEBENGINE_DISABLE_SANDBOX=1 \
    QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu"

# Two layers on purpose. The first (calibre + fonts) is large, slow to build
# for arm64, and almost never changes; the second (Python packages) changes
# whenever a dependency does. In one layer, bumping a package rebuilt calibre.

# calibre + Noto fonts: serve any ebook format as PDF in the browser.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        calibre \
        fonts-noto-core \
        fonts-noto-cjk \
        fonts-noto-extra \
        fonts-noto-color-emoji \
        fonts-wqy-microhei \
    && fc-cache -fv > /dev/null \
    && rm -rf /var/lib/apt/lists/*

# tgcrypto needs gcc for the C extension wheel build.
#
# Reading aloud with a voice of this machine's own (tts.py): the runtime only - sherpa-onnx
# (kept below 1.13: later builds no longer bring the part that turns English into sounds),
# onnxruntime for the optional pronunciation model, jieba to split Chinese into words. The
# voices themselves are not in the image; they are fetched when somebody asks for them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc python3-dev \
    && pip install --no-cache-dir \
        fastapi==0.115.0 \
        uvicorn[standard]==0.32.0 \
        asyncpg==0.30.0 \
        python-multipart==0.0.12 \
        kurigram==2.2.27 \
        tgcrypto==1.2.5 \
        qrcode[pil]==7.4.2 \
        "numpy>=1.26,<3" \
        "sherpa-onnx>=1.12.40,<1.13" \
        "onnxruntime>=1.19,<2" \
        jieba==0.42.1 \
    && apt-get purge -y --auto-remove gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8088", "--proxy-headers"]
