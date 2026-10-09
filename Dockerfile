FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY src/ ./
RUN useradd --system --no-create-home mock
USER mock

EXPOSE 19851
# Bind all interfaces inside the container; restrict exposure via `docker run -p 127.0.0.1:19851:19851`.
ENTRYPOINT ["python3", "mock_core.py", "--host", "0.0.0.0"]
