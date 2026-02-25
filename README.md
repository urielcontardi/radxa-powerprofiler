# radxa-powerprofiler

Power Profiler – test runner SmartTrac G2. Roda em Docker; você só precisa do `.env` com `IOT_X_USER_ID`.

## Na Radxa (Docker)

### 1. Conectar e clonar

```bash
ssh serial@10.8.162.150
cd ~
git clone https://github.com/urielcontardi/radxa-powerprofiler.git
cd radxa-powerprofiler
```

### 2. Configurar o .env (uma vez)

```bash
cp .env.example .env
nano .env   # preencha IOT_X_USER_ID=seu-user-id (obrigatório)
```

Opcional no `.env`: `IOT_API_BASE_URL`, `SENSOR_IDS`.

### 3. Subir e rodar

```bash
docker compose up --build -d
```

Pronto. O container roda a receita (configs 1–4, 3h cada).

**Dashboard Web:**
Acesse `http://<IP_DA_RADXA>:5000` no seu navegador para ver o status dos testes, etapa atual e logs.

Logs no terminal:
```bash
docker compose logs -f
```

Relatório salvo em `./data/report.json` (volume persistente).

### Resumo

```bash
ssh serial@10.8.162.150
cd ~ && git clone https://github.com/urielcontardi/radxa-powerprofiler.git && cd radxa-powerprofiler
cp .env.example .env && nano .env   # só IOT_X_USER_ID
docker compose up --build -d
```

Nada de Python/venv/pip na máquina: tudo roda dentro do container.
