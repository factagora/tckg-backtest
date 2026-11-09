FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn jsonschema
COPY tckg_backtest ./tckg_backtest
COPY contract ./contract
COPY bench ./bench
CMD exec uvicorn tckg_backtest.server:app --host 0.0.0.0 --port ${PORT:-8080}
