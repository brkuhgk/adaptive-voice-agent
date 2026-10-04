# Image for Fly.io (or any container host). Campaign files are baked in from campaigns/;
# results, transcripts and the do-not-call list go to the /data volume (DATA_DIR).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scenarios ./scenarios
COPY campaigns ./campaigns
COPY scripts ./scripts

ENV CAMPAIGN_DIR=/app/campaigns DATA_DIR=/data
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
