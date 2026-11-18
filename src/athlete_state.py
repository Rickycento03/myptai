#!/usr/bin/env python3
"""
athlete_state.py - Gestione Memoria Persistente Atleta (MyPTAI 2.0)
==================================================================
Mantiene e aggiorna lo stato master dell'atleta (athlete_state.json / coach_memory.json)
strutturato secondo il blueprint scientifico MyPTAI:
  - profilo_atleta (PB, fisiologia cardiaca Karvonen, target gara)
  - stato_fisiologico_live (ACWR, carichi acuto/cronico, recovery metrics, HRV, sonno)
  - piano_settimanale (microciclo con priorità CHIAVE/RIGENERANTE/RIPOSO)
  - log_infortuni_e_fastidi (Injury Lab con impatto biomeccanico)
  - note_soggettive_e_stile_vita (preferenze coaching severo, abitudini, clima)
"""

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from user_storage import user_dir, owner_email, legacy_paths, migrate_owner_file, write_private_json

logger = logging.getLogger("athlete_state")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
USERS_DIR = DATA_DIR / "users"
MASTER_MEMORY_PATH = DATA_DIR / "athlete_state.json"
FALLBACK_COACH_MEMORY = PROJECT_ROOT / "coach_memory.json"

# Personal values are supplied by private state/configuration and Garmin.
DEFAULT_STATE_TEMPLATE = {'profilo_atleta': {'id': None,
                    'nome': '',
                    'dispositivo': None,
                    'distanza_target': None,
                    'tempo_obiettivo': None,
                    'passo_target_km': None,
                    'pb': {},
                    'fisiologia_cardiaca': {}},
 'stato_fisiologico_live': {},
 'piano_settimanale': {},
 'log_infortuni_e_fastidi': [],
 'note_soggettive_e_stile_vita': {}}


def calculate_karvonen_zones(max_hr: int, rhr_baseline: int) -> Dict[str, Any]:
    """Calcola le 5 zone cardio col metodo scientifico di Karvonen (Heart Rate Reserve)."""
    hrr = max(1, max_hr - rhr_baseline)
    return {
        "fc_max": max_hr,
        "fc_riposo_baseline": rhr_baseline,
        "riserva_cardiaca_hrr": hrr,
        "metodo_calcolo": "Karvonen",
        "zone_bpm": {
            "Z1": {
                "min": round(rhr_baseline + 0.50 * hrr),
                "max": round(rhr_baseline + 0.60 * hrr),
                "label": "Recupero Attivo / Spurgo",
                "pct": "50-60%"
            },
            "Z2": {
                "min": round(rhr_baseline + 0.60 * hrr),
                "max": round(rhr_baseline + 0.70 * hrr),
                "label": "Endurance Aerobica Pura",
                "pct": "60-70%"
            },
            "Z3": {
                "min": round(rhr_baseline + 0.70 * hrr),
                "max": round(rhr_baseline + 0.80 * hrr),
                "label": "Medio / Tempo Run",
                "pct": "70-80%"
            },
            "Z4": {
                "min": round(rhr_baseline + 0.80 * hrr),
                "max": round(rhr_baseline + 0.90 * hrr),
                "label": "Soglia Anaerobica / Ritmo Gara",
                "pct": "80-90%"
            },
            "Z5": {
                "min": round(rhr_baseline + 0.90 * hrr),
                "max": max_hr,
                "label": "VO2Max / Massimale",
                "pct": "90-100%"
            }
        }
    }


def classify_acwr(acwr: float, crash_block: bool = False) -> str:
    """Classifica l'Acute:Chronic Workload Ratio secondo i range scientifici."""
    if crash_block and acwr >= 1.5:
        return "CRASH_BLOCK_CONTROLLED"
    if acwr < 0.8:
        return "UNDER_TRAINING"
    if 0.8 <= acwr <= 1.3:
        return "SWEET_SPOT"
    if 1.3 < acwr <= 1.5:
        return "HIGH_LOAD"
    return "DANGER_ZONE"


def _user_memory_path(user_email: Optional[str] = None) -> Path:
    email = user_email or owner_email()
    if not email:
        raise ValueError("Account required for athlete memory")
    path = user_dir(email) / "athlete_state.json"
    old = legacy_paths(email)
    migrate_owner_file(email, path, [old["memory"] / "athlete_state.json",
                                   old["web"] / "athlete_state.json", MASTER_MEMORY_PATH,
                                   FALLBACK_COACH_MEMORY])
    return path


def load_athlete_state(user_email: Optional[str] = None) -> Dict[str, Any]:
    """Carica la memoria dell'atleta per utente o globale."""
    path = _user_memory_path(user_email)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
                return state
        except Exception as e:
            logger.warning(f"Errore lettura athlete_state da {path}: {e}")

    # Inizializza con template
    state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
    # New accounts start with an empty profile; existing private state is kept.
    if (user_email or owner_email()) != owner_email():
        state = {
            "profilo_atleta": {"id": user_email, "nome": "", "pb": {}, "fisiologia_cardiaca": {}},
            "stato_fisiologico_live": {}, "piano_settimanale": {},
            "log_infortuni_e_fastidi": [], "note_soggettive_e_stile_vita": {},
        }
    state["profilo_atleta"]["id"] = user_email or owner_email()
    save_athlete_state(state, user_email)
    return state


def save_athlete_state(state: Dict[str, Any], user_email: Optional[str] = None) -> Path:
    """Write only the caller's namespace; legacy master is read-only migration."""
    path = _user_memory_path(user_email)
    write_private_json(path, state)
    return path


def sync_state_from_biometrics(summary: Dict[str, Any], user_email: Optional[str] = None) -> Dict[str, Any]:
    """Aggiorna la memoria persistente athlete_state.json unendo i dati live calcolati da analytics_summary."""
    state = load_athlete_state(user_email)

    atleta = summary.get("atleta", {})
    bio = summary.get("biometria", {})
    allen = summary.get("allenamento", {})
    readiness = summary.get("readiness", {})

    # 1. Aggiorna Fisiologia Karvonen se disponibili FC max e RHR
    max_hr = atleta.get("fc_max") or state.get("profilo_atleta", {}).get("fisiologia_cardiaca", {}).get("fc_max", 200)
    rhr = bio.get("rhr") or state.get("profilo_atleta", {}).get("fisiologia_cardiaca", {}).get("fc_riposo_baseline", 50)
    karvonen = calculate_karvonen_zones(int(max_hr), int(rhr))
    state.setdefault("profilo_atleta", {})["fisiologia_cardiaca"] = karvonen
    if atleta.get("nome"):
        state["profilo_atleta"]["nome"] = atleta["nome"]
    if user_email:
        state["profilo_atleta"]["id"] = user_email
    if summary.get("dispositivo") or atleta.get("dispositivo"):
        state["profilo_atleta"]["dispositivo"] = summary.get("dispositivo") or atleta.get("dispositivo")
    if summary.get("personal_records") or atleta.get("pb"):
        state["profilo_atleta"]["pb"] = summary.get("personal_records") or atleta.get("pb")

    previous_live = state.get("stato_fisiologico_live", {})
    previous_range = (previous_live.get("recovery_metrics") or {}).get("hrv_baseline_range")
    baseline = bio.get("hrv_baseline") or {}
    low = baseline.get("balancedLow") if isinstance(baseline, dict) else None
    high = baseline.get("balancedUpper") if isinstance(baseline, dict) else None
    valid_range = (isinstance(low, (int, float)) and not isinstance(low, bool)
                   and isinstance(high, (int, float)) and not isinstance(high, bool)
                   and 0 < low <= high)
    hrv_range = f"{low:g}-{high:g}" if valid_range else previous_range

    # 2. Aggiorna Stato Fisiologico Live
    acute_load = allen.get("epoc_acuto_7gg") or allen.get("carico_aerobico_high") or 500.0
    chronic_load = allen.get("epoc_cronico_28gg") or (acute_load * 0.85) or 400.0
    acwr_val = allen.get("acwr") or (round(acute_load / chronic_load, 2) if chronic_load else 1.0)
    acwr_status = classify_acwr(acwr_val)

    state["stato_fisiologico_live"] = {
        "data_rilevazione": str(date.today()),
        "training_status": allen.get("training_status_feedback") or "BALANCED",
        "training_status_code": 1 if "PRODUCTIVE" in str(allen.get("training_status_feedback")) else 2,
        "vo2max": allen.get("vo2max_attuale") or atleta.get("vo2max_baseline") or state.get("stato_fisiologico_live", {}).get("vo2max"),
        "carico_acuto_7g": float(acute_load),
        "carico_cronico_28g": float(chronic_load),
        "acwr": float(acwr_val),
        "acwr_status": acwr_status,
        "bilanciamento_carico": {
            "aerobico_basso": float(allen.get("carico_aerobico_low") or 0.0),
            "aerobico_alto": float(allen.get("carico_aerobico_high") or 0.0),
            "anaerobico": float(allen.get("carico_anaerobico") or 0.0),
            "focus": allen.get("load_feedback") or "BALANCED"
        },
        "recovery_metrics": {
            "body_battery_attuale": bio.get("body_battery_attuale") or 50,
            "body_battery_max_mattina": bio.get("body_battery_picco") or 80,
            "sonno_score": bio.get("sleep_score") or 70,
            "sonno_ore": bio.get("sonno_h") or 7.5,
            "sonno_fasi_minuti": {
                "deep": round((bio.get("deep_pct") or 15) * (bio.get("sonno_h") or 7.0) * 0.6),
                "rem": round((bio.get("rem_pct") or 20) * (bio.get("sonno_h") or 7.0) * 0.6),
                "light": round(60 * (bio.get("sonno_h") or 7.0) * 0.5),
                "awake": 20
            },
            "hrv_status": bio.get("hrv_status") or "BALANCED",
            "hrv_last_night_ms": bio.get("hrv_last_night") or bio.get("hrv_7n") or 55,
            "hrv_weekly_avg_ms": bio.get("hrv_weekly") or bio.get("hrv_7n") or 60,
            "hrv_baseline_range": hrv_range,
            "fc_riposo_odierna": bio.get("rhr") or rhr,
            "tempo_recupero_consigliato_ore": 0 if (readiness.get("readiness_score") or 0) > 60 else 24
        }
    }

    # 3. Sincronizza Infortuni
    if "infortuni" in summary:
        state["log_infortuni_e_fastidi"] = summary["infortuni"]

    save_athlete_state(state, user_email)
    return state
