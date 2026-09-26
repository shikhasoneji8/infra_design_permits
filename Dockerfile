FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY permit_harness ./permit_harness
COPY app ./app
COPY scripts ./scripts
COPY data ./data
RUN pip install --no-cache-dir .
ENV PORT=8000
CMD uvicorn app.server:app --host 0.0.0.0 --port ${PORT}
