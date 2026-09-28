FROM python:3.12-slim

RUN pip install --no-cache-dir psycopg2-binary==2.9.11 \
    && useradd --system --uid 10001 --create-home happychat

RUN mkdir /data && chown happychat:happychat /data

WORKDIR /app

COPY app/model_policy.py /app/model_policy.py

COPY app/happychat_api.py /app/happychat_api.py
COPY app/control_proxy.py /app/control_proxy.py
COPY app/model_adapter.py /app/model_adapter.py
COPY app/happy_token_gateway.py /app/happy_token_gateway.py

USER happychat

EXPOSE 8080 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"

CMD ["python", "-B", "/app/happychat_api.py"]
