# Pipedream Migrator on Render (free tier)
# Pure-Python app: no binary deps, tiny image.

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py config.py analyzer.py guides.py importers.py ./
COPY templates/ templates/
COPY guides/ guides/
COPY samples/sample-project.zip samples/sample-project.zip

ENV PORT=10000
EXPOSE 10000

# Render 免费版：512MB RAM / 0.1 CPU — 单 worker + 2 线程
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120 app:app"]
