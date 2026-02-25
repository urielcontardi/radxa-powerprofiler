# Power Profiler – SmartTrac G2

Testes automatizados para SmartTrac G2. O script segue uma lista de execução e garante, na primeira etapa, que todos os sensores estão na mesma versão (last status).

## Requisitos

- Python 3.11+ ou Docker

## Configuração

1. Copie `config.yaml.example` para `config.yaml` e preencha:
   - `iot.base_url`: base da API IoT (default: `https://iot.int.tractian.com`)
   - `iot.x_user_id`: seu user ID para a API
   - `sensor_ids`: lista de IDs dos sensores SmartTrac G2

2. Ou use variáveis de ambiente:
   - `IOT_API_BASE_URL`
   - `IOT_X_USER_ID`
   - `SENSOR_IDS` (IDs separados por vírgula)

## Execução

### Com Docker

```bash
docker build -t power-profiler .
docker run --rm \
  -e IOT_X_USER_ID="seu-user-id" \
  -e SENSOR_IDS="YEH8051,OUTRO_ID" \
  power-profiler
```

### Local (Python)

```bash
pip install -r requirements.txt
export IOT_X_USER_ID="..."
export SENSOR_IDS="YEH8051,OUTRO_ID"
# ou use config.yaml
python run_tests.py
```

## Lista de execução

Os passos são definidos em `execution_list.yaml`. O primeiro passo implementado é:

- **check_sensor_versions**: obtém o last status de cada sensor (API `/v1/smarttrac/{id}` e config v3), extrai a versão e falha se houver sensores com versões diferentes.

Novos passos podem ser adicionados em `execution_list.yaml` e os handlers em `run_tests.py` (dicionário `STEP_HANDLERS`).
