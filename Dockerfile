# Playwright's image ships Chromium and its system libraries.
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt && python -m playwright install chromium

COPY app ./app
COPY demo_site ./demo_site
COPY run.py .

RUN useradd --create-home --uid 10001 distill && mkdir -p /app/data && chown -R distill /app/data
USER distill

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
