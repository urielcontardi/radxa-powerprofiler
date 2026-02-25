#!/usr/bin/env python3
"""
Power Profiler - Test runner para SmartTrac G2.
Segue a lista de execução (execution_list.yaml) e executa cada passo.
Parte 1: Garantir que todos os sensores estão na mesma versão (last status).
"""

import os
import sys
import yaml
import requests
from typing import Any


# --- Configuração ---
def get_env(key: str, default: str | None = None) -> str:
    val = os.environ.get(key, default)
    if val is None:
        raise SystemExit(f"Variável de ambiente obrigatória: {key}")
    return val


def load_config() -> dict[str, Any]:
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    if not os.path.isfile(config_path):
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_sensor_ids() -> list[str]:
    """IDs dos sensores: env SENSOR_IDS (vírgula) ou config.yaml."""
    env_ids = os.environ.get("SENSOR_IDS", "").strip()
    if env_ids:
        return [s.strip() for s in env_ids.split(",") if s.strip()]
    config = load_config()
    ids = config.get("sensor_ids") or config.get("sensors") or []
    if isinstance(ids, list) and ids:
        return [str(x) for x in ids]
    raise SystemExit("Defina SENSOR_IDS (env) ou sensor_ids em config.yaml")


# --- API IoT ---
class IoTClient:
    def __init__(self, base_url: str, user_id: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "accept": "application/json",
            "x-user-id": user_id,
        }

    def _get(self, path: str) -> dict[str, Any] | list:
        url = f"{self.base_url}{path}"
        r = requests.get(url, headers=self.headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_smarttrac(self, sensor_id: str) -> dict[str, Any]:
        """GET /v1/smarttrac/{id} - dispositivo SmartTrac."""
        return self._get(f"/v1/smarttrac/{sensor_id}")

    def get_smarttrac_config(self, sensor_id: str) -> dict[str, Any]:
        """GET /v1/smarttrac/{id}/config/v3/raw - config v3 (tem configRevision, deviceId)."""
        return self._get(f"/v1/smarttrac/{sensor_id}/config/v3/raw")

    def get_last_status(self, sensor_id: str) -> dict[str, Any]:
        """
        Last status do sensor: combina device + config para obter versão/estado.
        Garante um único objeto com informação de 'versão' para comparação.
        """
        device = self.get_smarttrac(sensor_id)
        try:
            config_payload = self.get_smarttrac_config(sensor_id)
            config = config_payload.get("config") or config_payload
        except Exception:
            config = {}

        # Versão: preferir campo explícito do device (firmwareVersion, version, etc.)
        version = (
            device.get("firmwareVersion")
            or device.get("firmware_version")
            or device.get("version")
            or config.get("configRevision")
        )
        if version is not None:
            version = str(version)

        return {
            "sensor_id": sensor_id,
            "version": version or "unknown",
            "device": device,
            "config_revision": config.get("configRevision"),
        }


# --- Steps da lista de execução ---
def step_check_sensor_versions(client: IoTClient, sensor_ids: list[str]) -> None:
    """
    Obtém last status de cada sensor e verifica se todos estão na mesma versão.
    """
    print("  Obtendo last status de cada sensor...")
    statuses: list[dict[str, Any]] = []
    for sid in sensor_ids:
        try:
            st = client.get_last_status(sid)
            statuses.append(st)
            print(f"    {sid}: version={st['version']}")
        except requests.HTTPError as e:
            print(f"    {sid}: ERRO HTTP {e.response.status_code}")
            raise
        except Exception as e:
            print(f"    {sid}: ERRO {e}")
            raise

    versions = {s["version"] for s in statuses}
    if len(versions) > 1:
        print("\n  FALHA: Sensores com versões diferentes:", versions)
        sys.exit(1)
    if not versions:
        print("\n  FALHA: Nenhum sensor retornou versão.")
        sys.exit(1)

    print(f"\n  OK: Todos os {len(sensor_ids)} sensores na mesma versão: {next(iter(versions))}")


STEP_HANDLERS = {
    "check_sensor_versions": step_check_sensor_versions,
}


def load_execution_list() -> dict[str, Any]:
    path = os.path.join(os.path.dirname(__file__), "execution_list.yaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    config = load_config()
    iot = config.get("iot") or {}
    base_url = os.environ.get("IOT_API_BASE_URL") or iot.get("base_url") or "https://iot.int.tractian.com"
    user_id = os.environ.get("IOT_X_USER_ID") or iot.get("x_user_id") or ""
    if not user_id:
        raise SystemExit("Defina IOT_X_USER_ID (env) ou iot.x_user_id em config.yaml")

    sensor_ids = get_sensor_ids()
    client = IoTClient(base_url, user_id)

    execution = load_execution_list()
    steps = execution.get("steps") or []
    print(f"Lista de execução: {execution.get('name', 'Power Profiler')}")
    print(f"Sensores: {sensor_ids}\n")

    for i, step in enumerate(steps, 1):
        step_id = step.get("id")
        step_name = step.get("name", step_id)
        print(f"[{i}/{len(steps)}] {step_name}")

        handler = STEP_HANDLERS.get(step_id) if step_id else None
        if handler is None:
            print(f"  (handler não implementado: {step_id}, ignorando)\n")
            continue

        handler(client, sensor_ids)
        print()

    print("Execução concluída com sucesso.")


if __name__ == "__main__":
    main()
