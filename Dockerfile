# Power Profiler - SmartTrac G2 test runner
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Variáveis de ambiente na execução: IOT_API_BASE_URL, IOT_X_USER_ID, SENSOR_IDS (ou config)
ENTRYPOINT ["python", "-u", "run_tests.py"]
