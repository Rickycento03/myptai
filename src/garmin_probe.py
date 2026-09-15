#!/usr/bin/env python3
"""
garmin_probe.py - FASE 1: Data Discovery Garmin Connect
=======================================================
Autentica con Garmin Connect (account Riccardo) ed estrae i dati grezzi da tutti
gli endpoint disponibili: Profilo, Sleep, HRV, Body Battery, Training Status,
Readiness, Max Metrics, RHR, Race Predictions, Endurance Score, Attività e Laps.

Output: garmin_raw_dump.json (alla radice del progetto) per l'analisi in FASE 2.
"""

import os
import json
import sys
from datetime import date, timedelta
from pathlib import Path

# --- Configurazione -----------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DUMP_PATH = PROJECT_ROOT / "garmin_raw_dump.json"


def load_credentials() -> dict:
    """Carica le credenziali da config.json (priorità) o .env (fallback)."""
    cfg = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        garmin = cfg.get("garmin", {})
        email, password = garmin.get("email"), garmin.get("password")
        source = f"config.json"
    else:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
        email, password = os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD")
        source = ".env"

    if not email or not password:
        print("❌ Credenziali Garmin mancanti (config.json o .env).")
        sys.exit(1)

    print(f"🔑 Credenziali caricate da {source} per: {email}")
    return {"email": email, "password": password, "cfg": cfg}


def tokenstore_path(email: str) -> Path:
    """Percorso del tokenstore persistente per l'account (evita re-login)."""
    clean = email.lower().replace("@", "_").replace(".", "_")
    ts = os.getenv("GARMIN_TOKENSTORE") or os.path.expanduser(
        f"~/.garminconnect_session_{clean}"
    )
    return Path(ts)


def authenticate(client: "Garmin", email: str, password: str) -> bool:
    """Login con tokenstore se disponibile, altrimenti login diretto + salvataggio."""
    ts = tokenstore_path(email)
    if ts.exists():
        print(f"  ➜ Login tramite tokenstore salvato ({ts})...")
        try:
            client.login(ts)
            print("✅ Autenticazione da tokenstore riuscita!")
            return True
        except Exception as err:
            print(f"  ⚠️ Tokenstore non valido ({err}), provo login diretto...")
    else:
        print(f"  ℹ️ Nessun tokenstore in {ts}, procedo con login diretto...")

    client.login()
    try:
        ts.parent.mkdir(parents=True, exist_ok=True)
        inner = getattr(client, "client", None)
        if inner is not None and hasattr(inner, "dump"):
            inner.dump(ts)
        elif getattr(client, "garth", None) is not None:
            client.garth.dump(ts)
        else:
            raise RuntimeError("nessun metodo di dump disponibile")
        print(f"  💾 Sessione salvata in {ts}")
    except Exception as err:  # noqa: BLE001
        print(f"  (Nota salvataggio sessione: {err})")
    print("✅ Autenticazione diretta riuscita!")
    return True


def build_client(email: str, password: str, tokenstore: str = None) -> "Garmin":
    """Autentica con Garmin per QUALSIASI account (multi-utente):
    1) tokenstore se disponibile e valido (login senza password);
    2) altrimenti login diretto con email+password (SSO Garmin).
    Aggiorna/salva il tokenstore sul percorso indicato (o default per-email).
    Solleva l'eccezione Garmin se le credenziali sono errate o il servizio non
    è raggiungibile (il chiamante decide come mapparla a 401/502)."""
    from garminconnect import Garmin

    client = Garmin(email, password)
    ts = Path(tokenstore) if tokenstore else tokenstore_path(email)
    if ts.exists():
        try:
            client.login(str(ts))
            return client
        except Exception as err:  # noqa: BLE001 (tokenstore scaduto -> login diretto)
            print(f"  ⚠️ Tokenstore non valido ({err}), login diretto...")
    client.login()  # può sollevare per credenziali errate / rete
    try:
        # Salvataggio tokenstore nel formato della libreria installata:
        # garminconnect >= 0.3 -> client.client.dump (garmin_tokens.json);
        # versioni garth legacy -> client.garth.dump (oauth1/oauth2).
        ts.parent.mkdir(parents=True, exist_ok=True)
        inner = getattr(client, "client", None)
        if inner is not None and hasattr(inner, "dump"):
            inner.dump(str(ts))
        elif getattr(client, "garth", None) is not None:
            client.garth.dump(str(ts))
        else:
            raise RuntimeError("nessun metodo di dump disponibile")
    except Exception as err:  # noqa: BLE001
        print(f"  (Nota salvataggio sessione: {err})")
    return client


def per_user_dump_path(email: str) -> Path:
    """Dump grezzo per-utente: data/dumps/{slug}.json (multi-utente)."""
    clean = email.lower().replace("@", "_").replace(".", "_")
    return PROJECT_ROOT / "data" / "dumps" / f"garmin_raw_dump_{clean}.json"



def get_user_settings(client: "Garmin") -> dict:
    """Endpoint /userprofile-service/userprofile/user-settings: soglie ufficiali
    Garmin (lactateThresholdHeartRate / lactateThresholdSpeed / maxHeartRate /
    vo2MaxRunning) dell'utente.
    Compatibile con garminconnect 0.3.x (client interno `connectapi`)."""
    inner = getattr(client, "client", None)
    if inner is None or not hasattr(inner, "connectapi"):
        raise RuntimeError("client interno senza connectapi: libreria non compatibile")
    return inner.connectapi("/userprofile-service/userprofile/user-settings")


def get_training_load(client: "Garmin", date_iso: str) -> dict:
    """Endpoint /trainingload-service/trainingload/{data}: CARICO ACUTO (EPOC
    7gg) / CARICO CRONICO (28gg) / ACWR reali Garmin (v87).
    Fail-safe: se l'endpoint non è disponibile l'errore viene catturato da
    fetch_endpoint e le metriche restano null (zero crash)."""
    inner = getattr(client, "client", None)
    if inner is None or not hasattr(inner, "connectapi"):
        raise RuntimeError("client interno senza connectapi: libreria non compatibile")
    return inner.connectapi(f"/trainingload-service/trainingload/{date_iso}")


def run_probe(email: str = None, password: str = None, tokenstore: str = None,
              dump_path: Path = None) -> None:
    """Estrae i dati Garmin. Multi-utente: email/password/tokenstore/dump_path
    opzionali (default: account principale da config.json e dump alla radice)."""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    if not email or not password:
        creds = load_credentials()
        email = email or creds["email"]
        password = password or creds["password"]

    print("\n🌐 Connessione a Garmin Connect...")
    client = build_client(email, password, tokenstore)

    raw_dump = {
        "timestamp": today,
        "email": email,
        "endpoints": {},
    }

    def fetch_endpoint(name, func, *args, **kwargs):
        print(f"🔍 Estrazione: {name}...")
        try:
            res = func(*args, **kwargs)
            raw_dump["endpoints"][name] = {"status": "SUCCESS", "data": res}
            size = len(json.dumps(res, default=str))
            print(f"  ✓ {name}: Riuscito ({size} bytes)")
        except Exception as err:
            raw_dump["endpoints"][name] = {"status": "ERROR", "error": str(err)}
            print(f"  ✗ {name}: Errore ({err})")

    # 1. Profilo & Statistiche base
    fetch_endpoint("full_name", client.get_full_name)
    fetch_endpoint("user_summary_today", client.get_user_summary, today)
    fetch_endpoint("unit_system", client.get_unit_system)
    fetch_endpoint("devices", client.get_devices)
    fetch_endpoint("personal_records", client.get_personal_record)

    # 1b. Impostazioni utente (soglie ufficiali Garmin: FC Soglia / Passo Soglia / FC Max / Peso / Età)
    fetch_endpoint("user_settings", get_user_settings, client)

    # 1c. Carico allenamento: ACUTO EPOC 7gg / CRONICO 28gg / ACWR (v87)
    fetch_endpoint("trainingload_today", get_training_load, client, today)

    # 2. Statistiche giornaliere
    fetch_endpoint("stats_today", client.get_stats, today)
    fetch_endpoint("stats_yesterday", client.get_stats, yesterday)

    # 3. Biometria: Sleep, HRV, Body Battery
    fetch_endpoint("sleep_today", client.get_sleep_data, today)
    fetch_endpoint("sleep_yesterday", client.get_sleep_data, yesterday)
    fetch_endpoint("hrv_today", client.get_hrv_data, today)
    fetch_endpoint("hrv_yesterday", client.get_hrv_data, yesterday)
    fetch_endpoint("body_battery_today", client.get_body_battery, today)
    fetch_endpoint("body_battery_yesterday", client.get_body_battery, yesterday)

    # 3b. Sonno ultime 7 notti (per le medie settimanali "Medie 7 Notti")
    print("\n🌙 Estrazione sonno 7 notti (medie settimanali)...")
    for i in range(6, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        fetch_endpoint(f"sleep_day_{d}", client.get_sleep_data, d)

    # 4. Allenamento: Status, Readiness, Metriche
    fetch_endpoint("training_status_today", client.get_training_status, today)
    fetch_endpoint("training_readiness_today", client.get_training_readiness, today)
    fetch_endpoint("max_metrics_today", client.get_max_metrics, today)
    fetch_endpoint("rhr_today", client.get_rhr_day, today)
    fetch_endpoint("race_predictions", client.get_race_predictions)
    fetch_endpoint("endurance_score_today", client.get_endurance_score, today)

    # 5. Attività recenti + Laps (per il parsing ripetute/Z2)
    print("\n🏃 Estrazione attività di corsa recenti...")
    try:
        activities = client.get_activities(0, 10)
        raw_dump["endpoints"]["recent_activities"] = {
            "status": "SUCCESS",
            "data": activities,
        }
        print(f"  ✓ Trovate {len(activities)} attività recenti")

        # Per il parsing delle ripetute/Z2 interessano le attività di corsa.
        # I lap sono esposti da get_activity_splits (lapDTOs): nessun endpoint dedicato.
        running = [a for a in activities if a.get("activityType", {}).get("typeKey") == "running"]
        targets = running[:5] if running else activities[:3]
        print(f"  ➜ Estraggo dettagli/laps per {len(targets)} attività (di cui {len(running)} di corsa)")
        for act in targets:
            act_id = act.get("activityId")
            if not act_id:
                continue
            act_type = act.get("activityType", {}).get("typeKey", "unknown")
            print(f"  🔍 Dettagli attività {act_id} ({act_type})...")
            fetch_endpoint(f"activity_details_{act_id}", client.get_activity_details, act_id)
            fetch_endpoint(f"activity_splits_{act_id}", client.get_activity_splits, act_id)
            fetch_endpoint(f"activity_split_summaries_{act_id}", client.get_activity_split_summaries, act_id)
    except Exception as err:
        raw_dump["endpoints"]["recent_activities"] = {"status": "ERROR", "error": str(err)}
        print(f"  ✗ recent_activities: Errore ({err})")

    # Salva il dump completo
    out_path = dump_path or DUMP_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(raw_dump, f, indent=2, ensure_ascii=False, default=str)

    ok = sum(1 for e in raw_dump["endpoints"].values() if e["status"] == "SUCCESS")
    errs = sum(1 for e in raw_dump["endpoints"].values() if e["status"] == "ERROR")

    print(f"\n🎉 DATA DISCOVERY COMPLETATA!")
    print(f"   Endpoint OK: {ok}  |  Endpoint in errore: {errs}")
    print(f"📄 Dump grezzo salvato in: {DUMP_PATH}")


if __name__ == "__main__":
    run_probe()
