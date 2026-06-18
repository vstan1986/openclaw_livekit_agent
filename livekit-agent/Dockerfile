# syntax=docker/dockerfile:1
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY my_agent/ my_agent/

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "my_agent.main", "start"]
