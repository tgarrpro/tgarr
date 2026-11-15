FROM python:3.12-slim
WORKDIR /app

ENV QTWEBENGINE_DISABLE_SANDBOX=1 \
    QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu"

# What every install uses, and no more. The image was 2.3 GB, nearly all of it calibre (with
# the browser engine it prints through) and 480 MB of fonts for it - there for the few who
# open a .mobi. calibre is now fetched when a book first needs it (packs.py), as its own
# standalone build; here are only the dozen small libraries that build expects of a system.
#   ffmpeg          plays what a browser cannot (disc images, avi, wmv ...) by converting
#                   while it is watched (transcode.py)
#   poppler-utils   the PDF reader's pages and text (it used to come in with calibre)
#   fonts-wqy-microhei, fonts-dejavu-core   Chinese, Japanese, Korean and Latin for a PDF
#                   that brings no fonts of its own, and for books calibre converts
#   libegl1 ... libice6   what calibre's standalone build needs to start (11 MB together)
#   intel-media-va-driver, mesa-va-drivers   the drivers through which ffmpeg reaches an
#                   Intel or an AMD graphics card, when the container is given one, to make
#                   the picture there (transcode.py). An NVIDIA card brings its own.
#                   Intel's exists for amd64 only - there are no Intel cards on ARM.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        poppler-utils \
        xz-utils \
        fontconfig \
        fonts-wqy-microhei \
        fonts-dejavu-core \
        libegl1 libopengl0 libnss3 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
        libxcb-render-util0 libxcb-xkb1 libxkbcommon-x11-0 libsm6 libice6 \
        mesa-va-drivers \
        $( [ "$(dpkg --print-architecture)" = amd64 ] && echo intel-media-va-driver ) \
    && fc-cache -fv > /dev/null \
    && rm -rf /var/lib/apt/lists/*

# tgcrypto needs gcc for the C extension wheel build.
#
# What the voice and the picture model run on (numpy, onnxruntime, sherpa-onnx, jieba) is not
# installed here any more: packs.py installs it with pip, into the downloads folder, when the
# first model is fetched. pip itself therefore stays in the image.
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
    && apt-get purge -y --auto-remove gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8088", "--proxy-headers"]
