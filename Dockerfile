FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 user
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user . .
USER user
ENV PORT=10000 DATA_DIR=/tmp/jj_data PYTHONUNBUFFERED=1
EXPOSE 10000
# 작업을 한 번에 하나씩 처리하는 구조라 worker는 반드시 1개
CMD ["sh", "-c", "gunicorn -w 1 --threads 8 -t 600 -b 0.0.0.0:${PORT} server:app"]
