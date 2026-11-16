FROM python:3.12-slim
WORKDIR /app

# What every install uses, and no more. The image was 2.3 GB once, nearly all of it calibre and
# fonts for it. calibre is gone: the reader opens books by itself (ownbooks.py).
#   ffmpeg          plays what a browser cannot (disc images, avi, wmv ...) by converting
#                   while it is watched (transcode.py)
#   poppler-utils   the PDF reader's pages and text (it used to come in with calibre)
#   fonts-wqy-microhei, fonts-dejavu-core   Chinese, Japanese, Korean and Latin for a PDF
#                   that brings no fonts of its own
#   djvulibre-bin   a scanned .djvu made into a PDF by itself (ddjvu), without calibre
#   libarchive-tools, 7zip   bsdtar opens the comics that are a real RAR archive, 7zz the
#                   .chm help files (unpack.py); a few megabytes together
#   intel-media-va-driver, mesa-va-drivers   the drivers through which ffmpeg reaches an
#                   Intel or an AMD graphics card, when the container is given one, to make
#                   the picture there (transcode.py). An NVIDIA card brings its own.
#                   Intel's exists for amd64 only - there are no Intel cards on ARM.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        poppler-utils \
        djvulibre-bin \
        libarchive-tools \
        7zip \
        fontconfig \
        fonts-wqy-microhei \
        fonts-dejavu-core \
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
