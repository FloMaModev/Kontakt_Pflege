FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Berlin
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# ohne Root-Rechte laufen
RUN useradd --system --uid 10001 erechnung && mkdir -p /cache && chown erechnung /cache
USER erechnung
# Cache der Datenbankabfrage (übersteht Neustarts, wenn als Volume eingebunden)
VOLUME /cache

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=8).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips=*"]
