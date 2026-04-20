FROM node:22-bookworm-slim AS node_runtime

FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY pyproject.toml README.md run_cli.py ./
COPY src ./src
COPY docs ./docs
COPY examples ./examples

RUN pip install --no-cache-dir .
RUN npm i -g @steipete/summarize

RUN useradd --create-home appuser
USER appuser

EXPOSE 8080

CMD ["python", "run_cli.py", "serve", "--config", "/app/examples/config.napcat.compose.json"]
