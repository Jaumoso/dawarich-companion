FROM python:3.14.0rc3-slim-trixie
WORKDIR /app
RUN apt-get update && apt-get install -y gcc libpq-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY route_editor.py .
RUN useradd -m -u 1000 editor && chown -R editor:editor /app
USER editor
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT:-5000}/health || exit 1
EXPOSE 5000
CMD ["python", "route_editor.py"]