FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY dcbridge/ ./dcbridge/
COPY bridge.py .

EXPOSE 8000
ENTRYPOINT ["python", "bridge.py"]
