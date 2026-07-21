# syntax=docker/dockerfile:1.7
FROM python:3.12.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/predixalearn
RUN useradd --create-home --uid 10001 predixalearn
COPY packages/institution-protocol ./packages/institution-protocol
COPY apps/institution-server ./apps/institution-server
RUN pip install --no-cache-dir --require-hashes -r apps/institution-server/requirements.lock \
    && pip install --no-cache-dir --no-deps ./packages/institution-protocol ./apps/institution-server
WORKDIR /srv/predixalearn/apps/institution-server
USER predixalearn
EXPOSE 8100
CMD ["sh", "-c", "alembic upgrade head && uvicorn institution_server.main:app --host 0.0.0.0 --port 8100 --proxy-headers --forwarded-allow-ips=${PREDIXALEARN_PROXY_IPS:-127.0.0.1}"]
