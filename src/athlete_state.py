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

logger = logging.getLogger("athlete_state")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
USERS_DIR = DATA_DIR / "users"
MASTER_MEMORY_PATH = DATA_DIR / "athlete_state.json"
FALLBACK_COACH_MEMORY = PROJECT_ROOT / "coach_memory.json"

DEFAULT_STATE_TEMPLATE = {
    "profilo_atleta": {
        "id": "usr_ricky01",
        "nome": "Riccardo",
        "dispositivo": "Garmin Fenix 6X Pro Solar",
        "distanza_target": "21.097km",
        "tempo_obiettivo": "1:35:00",
        "passo_target_km": "4:30/km",
        "pb": {
            "1k": "3:54",
            "5k": "21:51",
            "10k": "44:54",
            "21k": "1:39:27"
        },
        "fisiologia_cardiaca": {
            "fc_max": 200,
            "fc_riposo_baseline": 50,
            "riserva_cardiaca_hrr": 150,
            "metodo_calcolo": "Karvonen",
            "zone_bpm": {
                "Z1": {"min": 125, "max": 140, "label": "Recupero Attivo / Spurgo", "pct": "50-60%"},
                "Z2": {"min": 140, "max": 155, "label": "Endurance Aerobica Pura", "pct": "60-70%"},
                "Z3": {"min": 155, "max": 170, "label": "Medio / Tempo Run", "pct": "70-80%"},
                "Z4": {"min": 170, "max": 185, "label": "Soglia Anaerobica / Ritmo Gara", "pct": "80-90%"},
                "Z5": {"min": 185, "max": 200, "label": "VO2Max / Massimale", "pct": "90-100%"}
            }
        }
    },
    "stato_fisiologico_live": {
        "data_rilevazione": str(date.today()),
        "training_status": "BALANCED",
        "training_status_code": 1,
        "vo2max": 54.2,
        "carico_acuto_7g": 520.0,
        "carico_cronico_28g": 252.0,
        "acwr": 1.15,
        "acwr_status": "SWEET_SPOT",
        "bilanciamento_carico": {
            "aerobico_basso": 462.8,
            "aerobico_alto": 322.5,
            "anaerobico": 95.9,
            "focus": "BALANCED"
        },
        "recovery_metrics": {
            "body_battery_attuale": 65,
            "body_battery_max_mattina": 85,
            "sonno_score": 78,
            "sonno_ore": 7.5,
            "sonno_fasi_minuti": {"deep": 65, "rem": 95, "light": 260, "awake": 30},
            "hrv_status": "BALANCED",
            "hrv_last_night_ms": 62,
            "hrv_weekly_avg_ms": 64,
            "hrv_baseline_range": "54-115",
            "fc_riposo_odierna": 50,
            "tempo_recupero_consigliato_ore": 0
        }
    },
    "piano_settimanale": {
        "macrociclo": "Preparazione Mezza Maratona",
        "settimana": 1,
        "focus_settimanale": "Costruzione Aerobica e Ritmo Gara",
        "km_target_settimana": 40.0,
        "km_completati_settimana": 0.0,
        "giorni": []
    },
    "log_infortuni_e_fastidi": [],
    "note_soggettive_e_stile_vita": {
        "abitudini_rilevanti": "Occasionali serate sociali nel weekend con alcol/fumo che deprimono HRV notturno e ritardano il sonno",
        "clima_sensibilita": "Sensibile alle temperature estive elevate (>28°C alza la FC di +8-10 bpm a parità di passo)",
        "feedback_atleta_richiesto": "Vuole coaching severo, senza complimenti gratuiti, focalizzato su ritmo e biomeccanica."
    }
}


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
    if not user_email:
        return MASTER_MEMORY_PATH
    sanitized = "".join(c if c.isalnum() or c in "._-" else "_" for c in user_email.lower())
    user_dir = USERS_DIR / sanitized
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / "athlete_state.json"


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

    # Fallback su file globale o template di default
    if MASTER_MEMORY_PATH.exists():
        try:
            with open(MASTER_MEMORY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Inizializza con template
    state = json.loads(json.dumps(DEFAULT_STATE_TEMPLATE))
    if user_email:
        state["profilo_atleta"]["id"] = user_email
    save_athlete_state(state, user_email)
    return state


def save_athlete_state(state: Dict[str, Any], user_email: Optional[str] = None) -> Path:
    """Salva la memoria dell'atleta sia nel percorso utente che sul master se owner."""
    path = _user_memory_path(user_email)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    # Mirror su master globale se è l'utente primario
    if not user_email or "centofanti" in user_email.lower():
        try:
            MASTER_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(MASTER_MEMORY_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Mirror su MASTER_MEMORY_PATH fallito: {e}")
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

    # 2. Aggiorna Stato Fisiologico Live
    acute_load = allen.get("epoc_acuto_7gg") or allen.get("carico_aerobico_high") or 500.0
    chronic_load = allen.get("epoc_cronico_28gg") or (acute_load * 0.85) or 400.0
    acwr_val = allen.get("acwr") or (round(acute_load / chronic_load, 2) if chronic_load else 1.0)
    acwr_status = classify_acwr(acwr_val)

    state["stato_fisiologico_live"] = {
        "data_rilevazione": str(date.today()),
        "training_status": allen.get("training_status_feedback") or "BALANCED",
        "training_status_code": 1 if "PRODUCTIVE" in str(allen.get("training_status_feedback")) else 2,
        "vo2max": allen.get("vo2max_attuale") or atleta.get("vo2max_baseline") or 54.2,
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
            "hrv_baseline_range": "54-115",
            "fc_riposo_odierna": bio.get("rhr") or rhr,
            "tempo_recupero_consigliato_ore": 0 if (readiness.get("readiness_score") or 0) > 60 else 24
        }
    }

    # 3. Sincronizza Infortuni
    if "infortuni" in summary:
        state["log_infortuni_e_fastidi"] = summary["infortuni"]

    save_athlete_state(state, user_email)
    return state
