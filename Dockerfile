# Dockerfile for Render.com deployment
FROM python:3.11-slim

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y \
    curl \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright and Chromium browser
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy application code
COPY . .

# Create data directory
RUN mkdir -p data

# Expose the default port (informational; overridden by $PORT at runtime)
EXPOSE 8501

# PORT defaults to 8501 but PaaS platforms (Render, Railway, Fly.io, etc.)
# inject their own PORT at runtime, which this container binds to.
ENV PORT=8501
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0

# Health check (shell form so ${PORT} is expanded)
HEALTHCHECK CMD curl --fail "http://localhost:${PORT:-8501}/_stcore/health" || exit 1

# Run the application (shell form so ${PORT} is expanded from the environment)
CMD streamlit run app.py --server.port=${PORT:-8501} --server.address=0.0.0.0
