# x16hub: X16 Office sync hub (X16Link server <-> ownCloud) as a container.
#
#   docker build -t x16hub .                  # runtime image (default target)
#   docker build --target test .              # same image + run the unit tests (no network)
#
# Secrets and state are NOT in the image. Mount them (see INSTALL.md):
#   /secrets  owncloud.json, x16link.keys, ca.pem   (host dir must be 700, files 600)
#   /data     hub.db (sync state, link config)      (host dir 700)
#   /sd       optional: where `link key new --sdcard /sd` writes X16LINK.KEY
#
# The hub refuses secret files that are not owned by the user it runs as, so run the container
# with the UID/GID that owns those host directories (`--user "$(id -u):$(id -g)"`).

ARG PYTHON_VERSION=3.14

# ---------------------------------------------------------------- base: deps + code
FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY hub/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY hub/x16hub /app/x16hub

# ---------------------------------------------------------------- test: `docker build --target test .`
FROM base AS test
COPY hub/requirements-dev.txt /app/requirements-dev.txt
RUN pip install -r /app/requirements-dev.txt
COPY hub/tests /app/tests
RUN useradd --create-home --uid 10001 tester && chown -R tester /app
USER tester
RUN python -m pytest -q -p no:cacheprovider

# ---------------------------------------------------------------- runtime (default target)
FROM base AS runtime

LABEL org.opencontainers.image.title="x16hub" \
      org.opencontainers.image.description="X16 Office sync hub: X16Link server for Commander X16 apps, syncing with ownCloud" \
      org.opencontainers.image.source="https://github.com/hermeslohin/x16-office-hub"

# Default non-root user. Override with --user / compose `user:` to match the owner of the
# mounted /secrets and /data directories.
RUN groupadd --gid 1000 x16hub \
 && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /data --shell /usr/sbin/nologin x16hub \
 && mkdir -p /secrets /data /sd \
 && chown x16hub:x16hub /secrets /data /sd \
 && chmod 700 /secrets /data /sd

ENV X16HUB_OC_CREDS=/secrets/owncloud.json \
    X16HUB_LINK_KEYS=/secrets/x16link.keys \
    X16HUB_DATA=/data \
    HOME=/data \
    X16HUB_LOG=INFO

VOLUME ["/secrets", "/data"]
EXPOSE 6502/tcp
USER 1000:1000

# No HEALTHCHECK on purpose: a probe that connects without authenticating counts as a failed
# handshake, and 5 of those in 10 minutes block the probing address.

# Listen on all of the container's interfaces; publish the port with `-p 6502:6502` and restrict
# who can reach it with the host firewall (INSTALL.md, step 7). The hub's own allow-list
# (private ranges by default) still applies on top.
ENTRYPOINT ["python", "-m", "x16hub"]
CMD ["serve", "--bind", "0.0.0.0"]
