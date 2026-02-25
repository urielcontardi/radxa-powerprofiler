FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Configura timezone
RUN apt-get update && apt-get install -y tzdata && \
    ln -fs /usr/share/zoneinfo/America/Sao_Paulo /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata && \
    rm -rf /var/lib/apt/lists/*
ENV TZ=America/Sao_Paulo

COPY configs/ configs/
COPY execution_list.yaml .
COPY run_tests.py .

# report.json será gravado aqui; use volume para persistir
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["python3", "run_tests.py"]
