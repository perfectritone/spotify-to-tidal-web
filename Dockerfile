FROM python:3.12-slim

WORKDIR /app

# Install git for git dependencies
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml .
COPY app/ app/

# Create venv and install dependencies
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
RUN uv venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN uv pip install --no-cache .

# Run the app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
