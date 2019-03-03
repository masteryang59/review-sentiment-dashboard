FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY reviewdash ./reviewdash
COPY demo_store ./demo_store
COPY scripts/evaluate.py ./scripts/evaluate.py

RUN useradd --create-home app && mkdir data && chown app data
USER app

EXPOSE 8000
ENTRYPOINT ["python", "-m", "reviewdash"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
