# Build from the repository root (it needs the ./pxe generator):
#   docker build -f services/controller/Dockerfile .
FROM python:3.13-alpine
WORKDIR /app
COPY services/controller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pxe /app/pxecli.py
COPY services/controller/app /app/app
ENV PXE_CLI=/app/pxecli.py \
    PXE_HTTP_ROOT=/srv/http \
    PXE_LOG_DIR=/var/log/pxe \
    PXE_DB=/var/lib/pxe-controller/pxe.db \
    PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
