FROM python:3.12-slim

# Java 21 for BRouter
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-21-jre-headless \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source
COPY app/ ./app/
COPY brouter/ ./brouter/
COPY config/ ./config/
COPY routes/ ./routes/
COPY VERSION ./VERSION

# Hash del commit "baked" al momento della build — passare sempre
# --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) in fase di build.
# Senza build-arg (es. sviluppo locale) resta "unknown", letto dall'app come
# fallback "dev" invece di rompere.
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=$GIT_COMMIT

# Download segments4 at first run (handled by entrypoint)
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 8501

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["streamlit", "run", "app/main.py", "--server.port=8501", "--server.address=0.0.0.0"]
