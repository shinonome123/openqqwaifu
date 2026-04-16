FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md run_cli.py ./
COPY src ./src
COPY docs ./docs
COPY examples ./examples

RUN pip install --no-cache-dir .

RUN useradd --create-home appuser
USER appuser

EXPOSE 8080

CMD ["python", "run_cli.py", "serve", "--config", "/app/examples/config.napcat.compose.json"]
