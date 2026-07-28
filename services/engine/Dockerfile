FROM python:3.12-slim

WORKDIR /srv

# Deps first so a code edit doesn't reinstall the world on every rebuild.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

# /data is a named volume — the SQLite file must outlive the container.
VOLUME ["/data"]
EXPOSE 8090

HEALTHCHECK --interval=10s --timeout=5s --retries=6 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8090/health')" || exit 1

# Baked at build time. The container has no git and no repo, so it cannot look
# this up itself — which is exactly why it is trustworthy: the value is frozen
# to the image rather than to whatever the working tree says right now.
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
