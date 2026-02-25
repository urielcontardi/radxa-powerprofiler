#!/usr/bin/env python3
"""
Power Profiler - SmartTrac G2 test runner.

Receita de teste:
  1. Verifica firmware (ST3001*) via last status, retry a cada N min
  2. Envia config JSON para cada sensor (POST config/v3/raw)
  3. Lê config de volta para capturar configRevision do servidor
  4. Aguarda todos aplicarem a config (last status → configRevision)
  5. Timer de X horas
  6. Repete ciclo 2-5 para quantas configs a receita definir

Registra timestamps de cada evento e salva relatório em report.json.
"""

import os
import sys
import json
import time
import yaml
import requests
from datetime import datetime, timezone
from typing import Any

EXPECTED_FW_PREFIX = "ST3001"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Logging com timestamp
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
def load_config() -> dict[str, Any]:
    path = os.path.join(SCRIPT_DIR, "config.yaml")
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


DEFAULT_SENSOR_IDS = [
    "RBT9269", "PNI6508", "DJE0269", "SCE1196", "QPE6415",
    "QJG6843", "JHS6915", "XIK4799", "KCC4196", "LHF1992",
]


def get_sensor_ids() -> list[str]:
    env_ids = os.environ.get("SENSOR_IDS", "").strip()
    if env_ids:
        return [s.strip() for s in env_ids.split(",") if s.strip()]
    cfg = load_config()
    ids = cfg.get("sensor_ids") or []
    if isinstance(ids, list) and ids:
        return [str(x) for x in ids]
    return DEFAULT_SENSOR_IDS.copy()


# ---------------------------------------------------------------------------
# IoT API Client
# ---------------------------------------------------------------------------
class IoTClient:
    def __init__(self, base_url: str, user_id: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "accept": "application/json",
            "x-user-id": user_id,
        })

    def _get(self, path: str) -> Any:
        r = self.session.get(f"{self.base_url}{path}", timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> Any:
        r = self.session.post(f"{self.base_url}{path}", json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_last_status(self, sensor_id: str) -> dict | None:
        """GET /v1/smarttrac/{id}/status/last → statusV3 (ou None se falhar)."""
        try:
            data = self._get(f"/v1/smarttrac/{sensor_id}/status/last")
            return data.get("statusV3") or data.get("statusV2") or data.get("statusV1")
        except Exception:
            return None

    def get_config(self, sensor_id: str) -> dict:
        """GET /v1/smarttrac/{id}/config/v3/raw"""
        return self._get(f"/v1/smarttrac/{sensor_id}/config/v3/raw")

    def post_config(self, sensor_id: str, body: dict) -> dict:
        """POST /v1/smarttrac/{id}/config/v3/raw"""
        return self._post(f"/v1/smarttrac/{sensor_id}/config/v3/raw", body)


# ---------------------------------------------------------------------------
# TestContext — estado compartilhado entre etapas
# ---------------------------------------------------------------------------
class TestContext:
    def __init__(self, client: IoTClient, sensor_ids: list[str]):
        self.client = client
        self.sensor_ids = sensor_ids
        self.expected_config_revisions: dict[str, int] = {}
        self.events: list[dict] = []

    def record(self, event: str, sensor_id: str = "", details: str = ""):
        self.events.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "sensor_id": sensor_id,
            "details": details,
        })

    def save_report(self):
        path = os.path.join(SCRIPT_DIR, "report.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.events, f, indent=2, ensure_ascii=False)
        log(f"Relatório salvo em {path}")


# ---------------------------------------------------------------------------
# STEP: check_firmware_version
# Verifica se todos sensores estão na versão ST3001* (via last status).
# Se o endpoint falhar, sensor ainda não está na versão correta.
# Retry a cada N minutos.
# ---------------------------------------------------------------------------
def step_check_firmware_version(ctx: TestContext, step: dict) -> None:
    interval = int(step.get("retry_interval_minutes") or 1)
    max_attempts = step.get("max_attempts")
    attempt = 0

    while True:
        attempt += 1
        if max_attempts and attempt > max_attempts:
            log(f"FALHA: Máximo de tentativas ({max_attempts}) atingido.")
            ctx.record("check_firmware_failed", details=f"max_attempts={max_attempts}")
            sys.exit(1)

        log(f"Verificando firmware (tentativa {attempt})...")
        all_ok = True
        for sid in ctx.sensor_ids:
            status = ctx.client.get_last_status(sid)
            if status is None:
                log(f"  ✗ {sid}: sem resposta (ainda não na versão correta)")
                all_ok = False
                continue
            fw = status.get("firmwareVersion") or ""
            if fw.startswith(EXPECTED_FW_PREFIX):
                log(f"  ✓ {sid}: {fw}")
            else:
                log(f"  ✗ {sid}: {fw or 'sem firmwareVersion'}")
                all_ok = False

        if all_ok:
            ctx.record("check_firmware_ok", details=f"attempt={attempt}")
            log(f"Todos os {len(ctx.sensor_ids)} sensores na versão {EXPECTED_FW_PREFIX}*.")
            return

        log(f"Aguardando {interval} min antes da próxima verificação...")
        time.sleep(interval * 60)


# ---------------------------------------------------------------------------
# STEP: send_config
# Carrega um JSON template, adapta por sensor (macAddress, deviceId, idData),
# faz POST, depois GET para capturar configRevision do servidor.
# ---------------------------------------------------------------------------
def step_send_config(ctx: TestContext, step: dict) -> None:
    config_file = step.get("config_file")
    if not config_file:
        log("ERRO: step send_config requer 'config_file' no YAML.")
        sys.exit(1)

    config_path = os.path.join(SCRIPT_DIR, config_file)
    with open(config_path, "r", encoding="utf-8") as f:
        template = json.load(f)

    log(f"Enviando config '{config_file}' para {len(ctx.sensor_ids)} sensores...")

    for sid in ctx.sensor_ids:
        # 1) Ler config atual para obter macAddress e idData do sensor
        try:
            current = ctx.client.get_config(sid)
            cur_cfg = current.get("config") or current
        except Exception as e:
            log(f"  ✗ {sid}: Erro ao ler config atual: {e}")
            ctx.record("send_config_error", sensor_id=sid, details=str(e))
            sys.exit(1)

        # 2) Montar payload: template + campos por sensor
        body = json.loads(json.dumps(template))
        cfg = body.get("config") or body
        cfg["deviceId"] = sid
        cfg["macAddress"] = cur_cfg.get("macAddress", "")
        cfg["idData"] = cur_cfg.get("idData", "")

        # 3) POST config
        try:
            ctx.client.post_config(sid, body)
            log(f"  ✓ {sid}: config enviada")
            ctx.record("config_sent", sensor_id=sid, details=config_file)
        except requests.HTTPError as e:
            log(f"  ✗ {sid}: Erro ao enviar config: HTTP {e.response.status_code}")
            ctx.record("send_config_error", sensor_id=sid, details=f"HTTP {e.response.status_code}")
            sys.exit(1)

        # 4) GET config de volta para capturar configRevision atribuído pelo servidor
        try:
            readback = ctx.client.get_config(sid)
            rb_cfg = readback.get("config") or readback
            rev = rb_cfg.get("configRevision")
            ctx.expected_config_revisions[sid] = rev
            log(f"    {sid}: configRevision={rev}")
            ctx.record("config_revision_captured", sensor_id=sid, details=f"configRevision={rev}")
        except Exception as e:
            log(f"  ✗ {sid}: Erro ao ler config de volta: {e}")
            sys.exit(1)

    log("Config enviada e configRevision capturado para todos os sensores.")


# ---------------------------------------------------------------------------
# STEP: wait_config_applied
# Fica lendo last status até o configRevision de todos bater com o esperado.
# ---------------------------------------------------------------------------
def step_wait_config_applied(ctx: TestContext, step: dict) -> None:
    interval = int(step.get("retry_interval_minutes") or 1)
    max_attempts = step.get("max_attempts")
    attempt = 0

    if not ctx.expected_config_revisions:
        log("AVISO: Nenhum configRevision esperado. Pulando.")
        return

    while True:
        attempt += 1
        if max_attempts and attempt > max_attempts:
            log(f"FALHA: Máximo de tentativas ({max_attempts}) atingido.")
            ctx.record("wait_config_failed", details=f"max_attempts={max_attempts}")
            sys.exit(1)

        log(f"Verificando se sensores aplicaram a config (tentativa {attempt})...")
        all_ok = True
        for sid in ctx.sensor_ids:
            expected_rev = ctx.expected_config_revisions.get(sid)
            if expected_rev is None:
                continue
            status = ctx.client.get_last_status(sid)
            if status is None:
                log(f"  ✗ {sid}: sem resposta")
                all_ok = False
                continue
            actual_rev = status.get("configRevision")
            if actual_rev == expected_rev:
                log(f"  ✓ {sid}: configRevision={actual_rev}")
            else:
                log(f"  ✗ {sid}: configRevision={actual_rev} (esperado {expected_rev})")
                all_ok = False

        if all_ok:
            ctx.record("config_applied_all", details=f"attempt={attempt}")
            log("Todos os sensores aplicaram a nova config.")
            return

        log(f"Aguardando {interval} min...")
        time.sleep(interval * 60)


# ---------------------------------------------------------------------------
# STEP: wait_timer
# Espera X horas (e/ou minutos). Loga progresso a cada 10 min.
# ---------------------------------------------------------------------------
def step_wait_timer(ctx: TestContext, step: dict) -> None:
    hours = float(step.get("duration_hours") or 0)
    minutes = float(step.get("duration_minutes") or 0)
    total_s = hours * 3600 + minutes * 60
    if total_s <= 0:
        log("AVISO: Timer com duração 0. Pulando.")
        return

    label = ""
    if hours:
        label += f"{hours:.0f}h"
    if minutes:
        label += f" {minutes:.0f}min"
    label = label.strip()

    log(f"Timer iniciado: {label} ({int(total_s)}s)")
    ctx.record("timer_started", details=label)

    start = time.time()
    while True:
        remaining = total_s - (time.time() - start)
        if remaining <= 0:
            break
        sleep_chunk = min(600, remaining)
        time.sleep(sleep_chunk)
        elapsed_h = (time.time() - start) / 3600
        log(f"  Timer: {elapsed_h:.1f}h de {label}")

    ctx.record("timer_finished", details=label)
    log(f"Timer concluído ({label}).")


# ---------------------------------------------------------------------------
# Registry de handlers
# ---------------------------------------------------------------------------
STEP_HANDLERS = {
    "check_firmware_version": step_check_firmware_version,
    "send_config": step_send_config,
    "wait_config_applied": step_wait_config_applied,
    "wait_timer": step_wait_timer,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def load_execution_list() -> dict[str, Any]:
    path = os.path.join(SCRIPT_DIR, "execution_list.yaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    cfg = load_config()
    iot = cfg.get("iot") or {}
    base_url = os.environ.get("IOT_API_BASE_URL") or iot.get("base_url") or "https://iot.int.tractian.com"
    user_id = os.environ.get("IOT_X_USER_ID") or iot.get("x_user_id") or ""
    if not user_id:
        raise SystemExit("Defina IOT_X_USER_ID (env) ou iot.x_user_id em config.yaml")

    sensor_ids = get_sensor_ids()
    client = IoTClient(base_url, user_id)
    ctx = TestContext(client, sensor_ids)

    execution = load_execution_list()
    steps = execution.get("steps") or []

    log(f"=== {execution.get('name', 'Power Profiler')} ===")
    log(f"Sensores: {sensor_ids}")
    log(f"Etapas: {len(steps)}")
    ctx.record("test_started", details=f"sensors={','.join(sensor_ids)}")
    print()

    for i, step in enumerate(steps, 1):
        step_id = step.get("id")
        step_name = step.get("name", step_id)
        log(f"[{i}/{len(steps)}] {step_name}")
        ctx.record("step_started", details=f"{step_id}: {step_name}")

        handler = STEP_HANDLERS.get(step_id) if step_id else None
        if handler is None:
            log(f"  (handler não implementado: {step_id}, pulando)")
            continue

        handler(ctx, step)
        ctx.record("step_completed", details=f"{step_id}: {step_name}")
        print()

    ctx.record("test_completed")
    log("=== Execução concluída com sucesso ===")
    ctx.save_report()


if __name__ == "__main__":
    main()
