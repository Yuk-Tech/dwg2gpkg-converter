# ---- stage 1: build LibreDWG from source (not in Debian/Ubuntu repos) ----
# Base image is ubuntu:24.04 ON PURPOSE, not debian:bookworm-slim:
# bookworm ships GDAL 3.6.2, which breaks this service — `ogrinfo -json`
# needs GDAL >= 3.7 (so /probe fails entirely) and its SQLite dialect
# cannot resolve the label-JOIN column (so NIB mapping from TEXT labels
# fails). Ubuntu 24.04 ships GDAL 3.8.4, the version the service was
# tested against (see SANDBOX-TEST.md §3). Do not switch back to bookworm.
FROM ubuntu:24.04 AS libredwg-builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    git autoconf automake libtool pkg-config gcc make texinfo ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# texinfo is required: without `makeinfo`, the top-level `make` fails in
# the doc/ subdirectory (LibreDWG.info, Error 127) even though the
# libraries and programs build fine.
ARG LIBREDWG_VERSION=0.13.3
# NOTE: do NOT run autogen.sh here — it does `git fetch --tags`, which
# hangs on slow networks. Replicate its steps minus the fetch.
RUN git clone --depth 1 --branch ${LIBREDWG_VERSION} \
      https://github.com/LibreDWG/libredwg.git /src/libredwg \
    && cd /src/libredwg \
    && echo ${LIBREDWG_VERSION} > .tarball-version \
    && echo ${LIBREDWG_VERSION} > .version \
    && autoreconf --install --symlink -I m4 \
    && git submodule update --init --recursive \
    && ./configure --disable-python --disable-trace \
    && make -j2 \
    && make install DESTDIR=/opt/libredwg-install

# ---- stage 2: runtime ----
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*
COPY --from=libredwg-builder /opt/libredwg-install/ /
RUN ldconfig
WORKDIR /app
COPY app/requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt
COPY app/ .
# Render injects $PORT; default 8080 for local runs.
ENV PORT=8080
EXPOSE 8080
# Stateless: all uploads/conversions use tempfile.TemporaryDirectory (/tmp).
# No credentials, no database, no persistent state.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
