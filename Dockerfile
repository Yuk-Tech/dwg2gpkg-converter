# ---- stage 1: build LibreDWG from source (not in Debian repos) ----
FROM debian:bookworm-slim AS libredwg-builder
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
    && make -j"$(nproc)" \
    && make install DESTDIR=/opt/libredwg-install

# ---- stage 2: runtime ----
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*
COPY --from=libredwg-builder /opt/libredwg-install/ /
RUN ldconfig
WORKDIR /app
COPY app/requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt
COPY app/ .
# Cloud Run injects $PORT; default 8080 for local runs.
ENV PORT=8080
EXPOSE 8080
# Stateless: all uploads/conversions use tempfile.TemporaryDirectory (/tmp).
# No credentials, no database, no persistent state.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
