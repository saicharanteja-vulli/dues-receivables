FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home dues
COPY app.py release.py ocr.py maintenance.py backup_secure.py provision.py ./
COPY static ./static
COPY samples ./samples
RUN mkdir /data /backups && chown -R dues:dues /data /backups
USER dues
ENV DUES_DATA=/data PYTHONDONTWRITEBYTECODE=1
EXPOSE 8120
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8120/api/health',timeout=4)"
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8120","--no-proxy-headers"]
