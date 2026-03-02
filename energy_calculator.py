#!/usr/bin/env python3
"""
energy_calculator.py — Calculadora de Energia SmartTrac G2

Lê o relatório de calibração (report.json) + medições de corrente média (mA)
para calibrar o modelo de energia e prever o consumo de qualquer config.

═══════════════════════════════════════════════════════════════════════════════
MODELO DE ENERGIA
═══════════════════════════════════════════════════════════════════════════════

  P_device [mW] = P_baseline
                + Σ_periodic [ (ΔE_sensor(N, rate) + β₁ × wave_bytes) / T_sense ]

Onde:
  P_baseline         = P_sleep + custo rádio vazio (medido por config_1)
  β₁ [mJ/byte]      = energia por byte de waveform (calibrado por config_12 − config_11)
  ΔE_sensor(N, rate) = energia de aquisição × escala por tempo:
                         ΔE_accel_ref × (N/rate) / t_accel_ref   [proporcional à duração]
  T_sense            = periodo de aquisição (periodS no JSON)

Parâmetros calibrados:
  P_baseline, β₁, ΔE_accel_16k, ΔE_accel_2k, ΔE_mag, ΔE_piezo

NOTA: A escala por tempo de aquisição absorve tanto sensing quanto overhead de
stats TX. Para aquisições muito curtas (< 0.5s), o overhead de stats representa
fração maior e a precisão diminui. Refine adicionando config_18 (accel_only,
16kHz/8192) para separar os dois componentes.

═══════════════════════════════════════════════════════════════════════════════
USO
═══════════════════════════════════════════════════════════════════════════════

  # Gerar template CSV de medições (preencher após o teste)
  python energy_calculator.py --template

  # Calibrar modelo com medições e exibir parâmetros
  python energy_calculator.py --measurements measurements.csv --voltage 3.6

  # Calibrar + prever configs de produto
  python energy_calculator.py --measurements measurements.csv --voltage 3.6 \\
      --predict configs/config_5.json configs/config_8.json

  # Prever TODOS os product configs (5-10)
  python energy_calculator.py --measurements measurements.csv \\
      --predict $(ls configs/config_{5,6,7,8,9,10}.json)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constantes do modelo
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent

# Período de TX de calibração [s]
T_TX_CALIB = 600.0

# Bytes de waveform adicionados em config_12 vs config_11
#   Accel: 3 eixos × 32768 amostras × 2 bytes
#   Mag  : 3 eixos × 2048  amostras × 2 bytes
#   Piezo: 1 eixo  × 32768 amostras × 2 bytes
WAVE_BYTES_FULL = (3 * 32768 + 3 * 2048 + 1 * 32768) * 2  # 274 432 bytes

# Tempos de aquisição de referência [s] (usados para escalar ΔE)
T_ACQ_ACCEL_16K_REF = 32768 / 16000   # 2.048 s   (config_13: 16kHz/32768)
T_ACQ_ACCEL_2K_REF  =  4096 /  2000   # 2.048 s   (config_16: 2kHz/4096 — mesmo tempo!)
T_ACQ_MAG_REF       =  2048 /  1400   # 1.463 s   (config_14: 1400Hz/2048)
T_ACQ_PIEZO_REF     = 32768 / 200000  # 0.164 s   (config_15: 200kHz/32768)

# Mapeamento role → arquivo de calibração (basename sem extensão)
CALIB_ROLES = {
    "baseline":   "config_1",   # rádio apenas, sem sensing
    "all_stats":  "config_11",  # todos sensores, stats, 16kHz/32768
    "all_wave":   "config_12",  # todos sensores, waveform+stats
    "accel_16k":  "config_13",  # accel only, 16kHz/32768
    "mag":        "config_14",  # mag only
    "piezo":      "config_15",  # piezo only
    "accel_2k":   "config_16",  # accel only, 2kHz/4096
    "linearity":  "config_17",  # todos, period=300s (validação)
}

ROLE_DESCRIPTIONS = {
    "baseline":  "Baseline: rádio apenas, sem sensing",
    "all_stats": "Todos sensores | 16kHz/32768 | stats",
    "all_wave":  "Todos sensores | 16kHz/32768 | waveform+stats",
    "accel_16k": "Accel only     | 16kHz/32768 | stats",
    "mag":       "Mag only       | 1400Hz/2048  | stats",
    "piezo":     "Piezo only     | 200kHz/32768 | stats",
    "accel_2k":  "Accel only     | 2kHz/4096   | stats  [mesmo t_acq que accel_16k]",
    "linearity": "Todos sensores | 16kHz/32768 | stats | T_sense=300s  [validação]",
}


# ---------------------------------------------------------------------------
# Estruturas de dados
# ---------------------------------------------------------------------------

@dataclass
class ModelParams:
    """Parâmetros calibrados do modelo de energia."""
    voltage_V: float

    # Potência base (sleep + rádio idle)
    P_baseline_mW: float

    # Custo por byte de waveform [mJ/byte]
    beta1_mJ_per_byte: float

    # ΔE por ciclo de T_tx [mJ] — energia extra acima do baseline por aquisição
    # Inclui: sensing + overhead TX de stats (absorvido no modelo)
    dE_accel_16k_mJ: float   # config_13 − config_1, referência 16kHz/32768
    dE_accel_2k_mJ: float    # config_16 − config_1, referência 2kHz/4096
    dE_mag_mJ: float         # config_14 − config_1, referência 1400Hz/2048
    dE_piezo_mJ: float       # config_15 − config_1, referência 200kHz/32768

    # Diferença de corrente entre 16kHz e 2kHz (mesmo t_acq = 2.048s)
    delta_I_accel_mA: float

    # Aditividade: (P11 − P1) vs soma das partes
    additivity_error_pct: Optional[float] = None

    # Validação de linearidade com config_17
    linearity_error_pct: Optional[float] = None

    # Correntes estimadas dos sensores [mA]
    # Nota: apenas a *diferença* de corrente entre 16k e 2k é determinada
    # sem ambiguidade. O valor absoluto requer conhecer o stats overhead.
    # Aqui, apresentamos a corrente EFETIVA (sensing + stats amortizados).
    I_eff_accel_16k_mA: float = 0.0   # ΔE_accel_16k / (V × t_acq_ref)
    I_eff_accel_2k_mA: float  = 0.0
    I_eff_mag_mA: float        = 0.0
    I_eff_piezo_mA: float      = 0.0


@dataclass
class PeriodicBreakdown:
    period_s: float
    accel_rate_hz: int
    N_accel: int
    N_mag: int
    N_piezo: int
    t_acq_accel_s: float
    t_acq_mag_s: float
    t_acq_piezo_s: float
    waveform_bytes: int
    E_accel_mJ: float
    E_mag_mJ: float
    E_piezo_mJ: float
    E_wave_per_tx_mJ: float
    P_sense_mW: float   # contribuição deste periódico à P_avg
    P_wave_mW: float    # contribuição do waveform à P_avg


@dataclass
class PredictionResult:
    config_file: str
    P_total_mW: float
    I_avg_mA: float
    E_mWh_per_day: float
    battery_life_days: Optional[float]   # None se battery_capacity_mAh não fornecido
    P_baseline_mW: float
    P_sense_total_mW: float
    P_wave_total_mW: float
    periodic: list[PeriodicBreakdown] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Ajuste do modelo
# ---------------------------------------------------------------------------

def fit_model(measurements: dict[str, float], voltage_V: float) -> ModelParams:
    """
    Calibra o modelo a partir das correntes médias medidas (mA).

    measurements: dict com chaves iguais aos valores de CALIB_ROLES
                  ex.: {"config_1": 45.2, "config_11": 52.1, ...}
    voltage_V   : tensão da bateria [V]
    """
    def E(role: str) -> float:
        """Energia por ciclo T_tx [mJ] = I_avg[mA] × V[V] × T_tx[s]."""
        key = CALIB_ROLES[role]
        if key not in measurements:
            raise ValueError(
                f"Medição ausente para '{key}' (role='{role}'). "
                f"Preencha measurements.csv com os dados do teste."
            )
        return measurements[key] * voltage_V * T_TX_CALIB

    # Parâmetros diretos
    E1   = E("baseline")
    E11  = E("all_stats")
    E12  = E("all_wave")
    E13  = E("accel_16k")
    E14  = E("mag")
    E15  = E("piezo")
    E16  = E("accel_2k")
    E17  = E("linearity")

    beta1          = (E12 - E11) / WAVE_BYTES_FULL
    dE_accel_16k   = E13 - E1
    dE_accel_2k    = E16 - E1
    dE_mag         = E14 - E1
    dE_piezo       = E15 - E1
    delta_I_accel  = (dE_accel_16k - dE_accel_2k) / (voltage_V * T_ACQ_ACCEL_16K_REF)

    # Correntes efetivas (ΔE / V / t_acq_ref) — absorvem stats overhead
    I_eff_accel_16k = dE_accel_16k / (voltage_V * T_ACQ_ACCEL_16K_REF)
    I_eff_accel_2k  = dE_accel_2k  / (voltage_V * T_ACQ_ACCEL_2K_REF)
    I_eff_mag       = dE_mag        / (voltage_V * T_ACQ_MAG_REF)
    I_eff_piezo     = dE_piezo      / (voltage_V * T_ACQ_PIEZO_REF)

    # Validação de aditividade: P11 − P1 vs (P13−P1) + (P14−P1) + (P15−P1)
    dE_all_measured = E11 - E1
    dE_all_sum      = dE_accel_16k + dE_mag + dE_piezo
    additivity_err  = (dE_all_measured - dE_all_sum) / dE_all_measured * 100.0 if dE_all_measured else None

    # Validação de linearidade: config_17 (T_sense=300s) deve dar
    # P17 ≈ 2×P11 − P1  →  E17_pred = 2×E11 − E1
    E17_pred       = 2.0 * E11 - E1
    linearity_err  = (E17_pred - E17) / E17 * 100.0 if E17 else None

    return ModelParams(
        voltage_V=voltage_V,
        P_baseline_mW=measurements[CALIB_ROLES["baseline"]] * voltage_V,
        beta1_mJ_per_byte=beta1,
        dE_accel_16k_mJ=dE_accel_16k,
        dE_accel_2k_mJ=dE_accel_2k,
        dE_mag_mJ=dE_mag,
        dE_piezo_mJ=dE_piezo,
        delta_I_accel_mA=delta_I_accel,
        additivity_error_pct=additivity_err,
        linearity_error_pct=linearity_err,
        I_eff_accel_16k_mA=I_eff_accel_16k,
        I_eff_accel_2k_mA=I_eff_accel_2k,
        I_eff_mag_mA=I_eff_mag,
        I_eff_piezo_mA=I_eff_piezo,
    )


# ---------------------------------------------------------------------------
# Cálculo de payload de waveform
# ---------------------------------------------------------------------------

def compute_waveform_bytes(sc: dict) -> int:
    """Calcula bytes de waveform para um sampleConfig."""
    total = 0
    N_accel = sc.get("mainAccelXSamples", 0)
    N_mag   = sc.get("magnetometerXSamples", 0)
    N_piezo = sc.get("piezoSamples", 0)
    if sc.get("sendMainAccelXWaveform") and N_accel > 0:
        total += 3 * N_accel * 2   # 3 eixos × N × 2 bytes
    if sc.get("sendMagnetometerXWaveform") and N_mag > 0:
        total += 3 * N_mag * 2
    if sc.get("sendPiezoWaveform") and N_piezo > 0:
        total += N_piezo * 2
    return total


# ---------------------------------------------------------------------------
# Predição
# ---------------------------------------------------------------------------

def predict_config(
    config: dict,
    model: ModelParams,
    battery_capacity_mAh: Optional[float] = None,
) -> PredictionResult:
    """
    Prediz o consumo médio de um config qualquer.

    Fórmula por config periódico:
        P_sense [mW] += ΔE_sensor(N, rate) / T_sense
        P_wave  [mW] += β₁ × waveform_bytes / T_sense

    Escala de ΔE por tempo de aquisição:
        ΔE_accel(16k, N) = dE_accel_16k_ref × (N/rate) / t_accel_16k_ref
        ΔE_accel(2k,  N) = dE_accel_2k_ref  × (N/rate) / t_accel_2k_ref
        (similar para mag e piezo)
    """
    V       = model.voltage_V
    T_tx    = float(config["config"]["commsConfig"]["transmissionPeriodS"])

    P_sense_total = 0.0
    P_wave_total  = 0.0
    periodic_list: list[PeriodicBreakdown] = []

    for pc in config["config"].get("periodicConfig", []):
        T_sense     = float(pc["periodS"])
        sc          = pc["sampleConfig"]

        accel_rate  = sc.get("mainAccelSampleRateHz", 16000)
        N_accel     = sc.get("mainAccelXSamples", 0)
        N_mag       = sc.get("magnetometerXSamples", 0)
        N_piezo     = sc.get("piezoSamples", 0)

        t_accel = N_accel / accel_rate if N_accel > 0 else 0.0
        t_mag   = N_mag   / 1400       if N_mag   > 0 else 0.0
        t_piezo = N_piezo / 200000     if N_piezo > 0 else 0.0

        # ΔE por aquisição, escalado pelo tempo de aquisição
        if N_accel > 0:
            if accel_rate >= 8000:   # 16kHz branch
                E_accel = model.dE_accel_16k_mJ * (t_accel / T_ACQ_ACCEL_16K_REF)
            else:                    # 2kHz branch
                E_accel = model.dE_accel_2k_mJ  * (t_accel / T_ACQ_ACCEL_2K_REF)
        else:
            E_accel = 0.0

        E_mag   = model.dE_mag_mJ   * (t_mag   / T_ACQ_MAG_REF)   if N_mag   > 0 else 0.0
        E_piezo = model.dE_piezo_mJ * (t_piezo / T_ACQ_PIEZO_REF) if N_piezo > 0 else 0.0

        # Contribuição à potência média [mW] — escala com 1/T_sense
        P_sense = (E_accel + E_mag + E_piezo) / T_sense

        # Custo waveform por TX: cada aquisição gera waveform → β₁×bytes / T_sense
        wave_bytes  = compute_waveform_bytes(sc)
        E_wave_per_tx = model.beta1_mJ_per_byte * wave_bytes  # mJ por TX (quando T_sense=T_tx)
        P_wave        = E_wave_per_tx / T_sense if wave_bytes > 0 else 0.0

        P_sense_total += P_sense
        P_wave_total  += P_wave

        periodic_list.append(PeriodicBreakdown(
            period_s=T_sense,
            accel_rate_hz=accel_rate if N_accel > 0 else 0,
            N_accel=N_accel,
            N_mag=N_mag,
            N_piezo=N_piezo,
            t_acq_accel_s=round(t_accel, 4),
            t_acq_mag_s=round(t_mag, 4),
            t_acq_piezo_s=round(t_piezo, 4),
            waveform_bytes=wave_bytes,
            E_accel_mJ=round(E_accel, 4),
            E_mag_mJ=round(E_mag, 4),
            E_piezo_mJ=round(E_piezo, 4),
            E_wave_per_tx_mJ=round(E_wave_per_tx, 4),
            P_sense_mW=round(P_sense, 4),
            P_wave_mW=round(P_wave, 4),
        ))

    P_total = model.P_baseline_mW + P_sense_total + P_wave_total
    I_avg   = P_total / V
    E_day   = P_total * 24.0   # mWh/day

    battery_life = (battery_capacity_mAh / I_avg / 24.0) if battery_capacity_mAh and I_avg > 0 else None

    return PredictionResult(
        config_file=config.get("_source_path", ""),
        P_total_mW=round(P_total, 4),
        I_avg_mA=round(I_avg, 4),
        E_mWh_per_day=round(E_day, 3),
        battery_life_days=round(battery_life, 1) if battery_life else None,
        P_baseline_mW=round(model.P_baseline_mW, 4),
        P_sense_total_mW=round(P_sense_total, 4),
        P_wave_total_mW=round(P_wave_total, 4),
        periodic=periodic_list,
    )


# ---------------------------------------------------------------------------
# Leitura de medições
# ---------------------------------------------------------------------------

def load_measurements(csv_path: str) -> dict[str, float]:
    """
    Lê CSV com colunas: config_file, I_avg_mA
    Retorna dict: basename sem extensão → I_avg_mA
    """
    measurements: dict[str, float] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cf  = row.get("config_file", "").strip()
            val = row.get("I_avg_mA", "").strip()
            if not cf or not val:
                continue
            key = Path(cf).stem   # "configs/config_1.json" → "config_1"
            try:
                measurements[key] = float(val)
            except ValueError:
                pass
    return measurements


def generate_template(output_path: str, report_path: Optional[str] = None) -> None:
    """Gera CSV template de medições para preencher após o teste."""

    # Se há report.json, lê as janelas de medição para enriquecer o template
    windows = []
    if report_path and Path(report_path).exists():
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
        windows = report.get("measurement_windows", [])

    rows = []
    for role, cfg_name in CALIB_ROLES.items():
        cfg_file = f"configs/{cfg_name}.json"
        purpose  = ROLE_DESCRIPTIONS[role]
        timer_start = ""
        timer_end   = ""
        for w in windows:
            if Path(w.get("config_file", "")).stem == cfg_name:
                timer_start = w.get("timer_start", "")
                timer_end   = w.get("timer_end", "")
                break
        rows.append({
            "config_file": cfg_file,
            "I_avg_mA": "",
            "purpose": purpose,
            "timer_start": timer_start,
            "timer_end": timer_end,
        })

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["config_file", "I_avg_mA", "purpose", "timer_start", "timer_end"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Template gerado: {output_path}")
    print("Preencha a coluna I_avg_mA com os valores medidos pelo profiler de energia.")


# ---------------------------------------------------------------------------
# Saída formatada
# ---------------------------------------------------------------------------

def _pct_flag(pct: Optional[float]) -> str:
    if pct is None:
        return "N/A"
    flag = "✓" if abs(pct) < 5 else ("⚠" if abs(pct) < 15 else "✗")
    return f"{pct:+.1f}% {flag}"


def print_model_params(params: ModelParams) -> None:
    sep = "═" * 72
    print(f"\n{sep}")
    print("  PARÂMETROS DO MODELO DE ENERGIA")
    print(sep)
    print(f"  Tensão da bateria       : {params.voltage_V:.2f} V")
    print(f"  P_baseline (sleep+rádio): {params.P_baseline_mW:.4f} mW  "
          f"({params.P_baseline_mW / params.voltage_V:.4f} mA)")
    print()
    print("  ── Custo de transmissão (waveform) ──────────────────────────")
    print(f"  β₁                      : {params.beta1_mJ_per_byte * 1e6:.4f} nJ/byte  "
          f"({params.beta1_mJ_per_byte:.6e} mJ/byte)")
    print()
    print("  ── ΔE de aquisição por referência ───────────────────────────")
    print(f"  Accel 16kHz/32768 (2.048s): {params.dE_accel_16k_mJ:.4f} mJ/ciclo  "
          f"(I_eff={params.I_eff_accel_16k_mA:.4f} mA)")
    print(f"  Accel  2kHz/ 4096 (2.048s): {params.dE_accel_2k_mJ:.4f} mJ/ciclo  "
          f"(I_eff={params.I_eff_accel_2k_mA:.4f} mA)")
    print(f"  ΔI_accel (16k−2k)          : {params.delta_I_accel_mA:.4f} mA")
    print(f"  Mag    1400Hz/2048 (1.463s): {params.dE_mag_mJ:.4f} mJ/ciclo  "
          f"(I_eff={params.I_eff_mag_mA:.4f} mA)")
    print(f"  Piezo  200kHz/32768(0.164s): {params.dE_piezo_mJ:.4f} mJ/ciclo  "
          f"(I_eff={params.I_eff_piezo_mA:.4f} mA)")
    print()
    print("  ── Validações ───────────────────────────────────────────────")
    print(f"  Aditividade  (P11 = P13+P14+P15): erro = {_pct_flag(params.additivity_error_pct)}")
    print(f"  Linearidade  (P17 = 2×P11 − P1) : erro = {_pct_flag(params.linearity_error_pct)}")
    print(sep)


def print_prediction(result: PredictionResult, battery_capacity_mAh: Optional[float] = None) -> None:
    sep = "─" * 72
    print(f"\n  Config : {result.config_file}")
    print(sep)
    print(f"  P_total      : {result.P_total_mW:.4f} mW")
    print(f"  I_avg        : {result.I_avg_mA:.4f} mA")
    print(f"  E/dia        : {result.E_mWh_per_day:.3f} mWh/dia")
    if result.battery_life_days is not None:
        print(f"  Autonomia    : {result.battery_life_days:.1f} dias  "
              f"({result.battery_life_days/365:.2f} anos)")
    print()
    print(f"  Breakdown de potência:")
    print(f"    P_baseline  : {result.P_baseline_mW:.4f} mW  "
          f"({result.P_baseline_mW / result.P_total_mW * 100:.1f}%)")
    print(f"    P_sensing   : {result.P_sense_total_mW:.4f} mW  "
          f"({result.P_sense_total_mW / result.P_total_mW * 100:.1f}%)")
    print(f"    P_waveform  : {result.P_wave_total_mW:.4f} mW  "
          f"({result.P_wave_total_mW / result.P_total_mW * 100:.1f}%)")
    print()
    print(f"  {'T_sense':>8} | {'Accel':>14} | {'Mag':>8} | {'Piezo':>8} | "
          f"{'Wave':>10} | {'P_sense':>10} | {'P_wave':>10}")
    print(f"  {'-'*8} | {'-'*14} | {'-'*8} | {'-'*8} | "
          f"{'-'*10} | {'-'*10} | {'-'*10}")
    for pb in result.periodic:
        accel_str = (f"{pb.accel_rate_hz//1000}kHz/{pb.N_accel}"
                     if pb.N_accel else "—")
        mag_str   = str(pb.N_mag)   if pb.N_mag   else "—"
        piezo_str = str(pb.N_piezo) if pb.N_piezo else "—"
        print(f"  {pb.period_s:>7.0f}s | {accel_str:>14} | {mag_str:>8} | "
              f"{piezo_str:>8} | {pb.waveform_bytes:>8}B   | "
              f"{pb.P_sense_mW:>9.4f}mW | {pb.P_wave_mW:>9.4f}mW")
    print(sep)


def predictions_to_csv(results: list[PredictionResult], output_path: str) -> None:
    """Exporta todas as predições para CSV."""
    rows = []
    for r in results:
        rows.append({
            "config_file": r.config_file,
            "P_total_mW": r.P_total_mW,
            "I_avg_mA": r.I_avg_mA,
            "E_mWh_per_day": r.E_mWh_per_day,
            "battery_life_days": r.battery_life_days if r.battery_life_days else "",
            "P_baseline_mW": r.P_baseline_mW,
            "P_sense_total_mW": r.P_sense_total_mW,
            "P_wave_total_mW": r.P_wave_total_mW,
        })
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nPredições exportadas: {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Calculadora de Energia SmartTrac G2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("USO")[1] if "USO" in __doc__ else "",
    )
    parser.add_argument(
        "--report",
        default=str(SCRIPT_DIR / "report.json"),
        help="Caminho para report.json (default: report.json)",
    )
    parser.add_argument(
        "--measurements",
        default=None,
        help="CSV com colunas config_file,I_avg_mA",
    )
    parser.add_argument(
        "--voltage",
        type=float,
        default=3.6,
        help="Tensão da bateria em V (default: 3.6)",
    )
    parser.add_argument(
        "--battery-mah",
        type=float,
        default=None,
        help="Capacidade da bateria em mAh (para calcular autonomia)",
    )
    parser.add_argument(
        "--predict",
        nargs="+",
        default=[],
        help="Configs para prever (ex: configs/config_5.json configs/config_8.json)",
    )
    parser.add_argument(
        "--predict-all",
        action="store_true",
        help="Prever todos os product configs (config_5 a config_10)",
    )
    parser.add_argument(
        "--template",
        action="store_true",
        help="Gerar measurements_template.csv e sair",
    )
    parser.add_argument(
        "--output-csv",
        default=None,
        help="Exportar predições para CSV",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="Exportar predições e parâmetros para JSON",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # ── Modo: gerar template ──────────────────────────────────────────────
    if args.template:
        out = str(SCRIPT_DIR / "measurements_template.csv")
        generate_template(out, args.report if Path(args.report).exists() else None)
        return

    # ── Verificar medições ────────────────────────────────────────────────
    if not args.measurements:
        print("ERRO: Forneça --measurements <arquivo.csv> com as correntes medidas.")
        print("      Para gerar o template: python energy_calculator.py --template")
        sys.exit(1)

    if not Path(args.measurements).exists():
        print(f"ERRO: Arquivo não encontrado: {args.measurements}")
        sys.exit(1)

    measurements = load_measurements(args.measurements)

    missing = [CALIB_ROLES[r] for r in CALIB_ROLES if CALIB_ROLES[r] not in measurements]
    if missing:
        print(f"AVISO: Medições ausentes: {missing}")
        print("       Calibração parcial — parâmetros que dependem dessas medições serão ignorados.")

    # ── Calibrar modelo ───────────────────────────────────────────────────
    required_for_fit = ["baseline", "all_stats", "all_wave", "accel_16k", "mag", "piezo", "accel_2k"]
    if any(CALIB_ROLES[r] not in measurements for r in required_for_fit):
        print("ERRO: Medições insuficientes para calibrar o modelo.")
        print("      Preencha todos os configs de calibração no CSV.")
        sys.exit(1)

    params = fit_model(measurements, args.voltage)
    print_model_params(params)

    # ── Predições ─────────────────────────────────────────────────────────
    predict_files = list(args.predict)
    if args.predict_all:
        predict_files += sorted(str(p) for p in (SCRIPT_DIR / "configs").glob("config_[5-9].json"))
        predict_files += sorted(str(p) for p in (SCRIPT_DIR / "configs").glob("config_1[0].json"))

    results: list[PredictionResult] = []

    if predict_files:
        sep = "═" * 72
        print(f"\n{sep}")
        print("  PREDIÇÕES DE CONSUMO")
        print(sep)

        for cfg_path in predict_files:
            if not Path(cfg_path).exists():
                print(f"\n  AVISO: arquivo não encontrado: {cfg_path}")
                continue
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["_source_path"] = cfg_path

            r = predict_config(cfg, params, args.battery_mah)
            print_prediction(r, args.battery_mah)
            results.append(r)

    # ── Exportar ──────────────────────────────────────────────────────────
    if results and args.output_csv:
        predictions_to_csv(results, args.output_csv)

    if args.output_json:
        out_data = {
            "generated_at": datetime.now().isoformat(),
            "model_params": asdict(params),
            "predictions": [asdict(r) for r in results],
        }
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(out_data, f, indent=2, ensure_ascii=False)
        print(f"Resultados exportados: {args.output_json}")

    if not predict_files:
        print("\nDica: use --predict configs/config_5.json para prever consumo de um config,")
        print("      ou --predict-all para todos os product configs (5-10).")


if __name__ == "__main__":
    main()
