FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY configs/ configs/
COPY execution_list.yaml .
COPY run_tests.py .

# report.json será gravado aqui; use volume para persistir
ENV PYTHONUNBUFFERED=1

CMD ["python3", "run_tests.py"]
