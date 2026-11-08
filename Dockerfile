FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    BILI_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates
RUN mkdir -p /data && chmod 700 /data

EXPOSE 8090

CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:8090", "app:app"]
