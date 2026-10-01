FROM python:3.11-alpine

LABEL org.opencontainers.image.title="Mirrorgate"
LABEL org.opencontainers.image.description="Mirror container images from public registries into a private destination registry."
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/LightStein/mirrorgate"

WORKDIR /app

# skopeo for the actual copy work, curl for registry health checks,
# ca-certificates so TLS works through a corporate proxy.
RUN apk add --no-cache skopeo curl ca-certificates

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 -r requirements.txt

COPY app.py .
COPY templates/ ./templates/
# Vendored htmx. The UI used to fetch it from unpkg.com at page load, which
# made it depend on the browser's own internet access. See static/README.md.
COPY static/ ./static/

# OCP/PSS runs containers with a random UID and GID 0. Make /app group-0 readable
# so the random UID can exec the app. Don't set USER - OCP overrides it.
#
# /data is the history volume's mount point. Created group-0 writable here so
# the app can write even when no volume is mounted (history then lives in the
# container and dies with it, which is the documented degraded mode rather
# than a crash). A mounted PVC replaces this directory and carries the
# fsGroup the platform assigns.
RUN chgrp -R 0 /app && chmod -R g=u /app \
 && mkdir -p /data && chgrp 0 /data && chmod g=u /data

EXPOSE 8080

# All configuration comes from the environment. See the chart's values.yaml for
# the canonical set. NEXUS_REGISTRY is required at runtime — the app exits if unset.
ENV PORT=8080 \
    WORKERS=3 \
    HISTORY_SIZE=500 \
    HEALTH_CHECK_INTERVAL=30 \
    DATA_DIR=/data \
    LOG_TAIL_CHARS=4000

CMD ["python", "-u", "app.py"]
