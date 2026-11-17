#!/usr/bin/env python3
"""
analytics_engine.py - FASE 2: Engine Fisiologico Locale
========================================================
Elaborazione algebrica dei dati Garmin grezzi (garmin_raw_dump.json) per:

1. READINESS SCORE (0-100): combinazione pesata di HRV notturno, Sleep Score,
   HRV settimanale e Body Battery attuale.
2. CLASSIFICAZIONE LAP: separa il ritmo Z2/Z3 (corse lente) da Z1/Z2 (recuperi)
   e da Z4+ (ripetute/soglia) usando le zone cardio di Riccardo da config.json.
3. TRACKING PROGRESSI: FC Max (200), FC Soglia (176), Passo Soglia (4:44),
   VO2Max (54.5) - confronto baseline config vs valori osservati reali.
4. CONTESTO COMPATTO per DeepSeek: sintesi numerica JSON, senza testo libero
   (risparmio token + zero allucinazioni: solo dati reali).

Output: analytics_summary.json (per FASE 3) + contesto stampato a schermo.
"""

import json
import re
from math import isfinite
from statistics import median
from typing import Optional
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DUMP_PATH = PROJECT_ROOT / "garmin_raw_dump.json"
OUTPUT_PATH = PROJECT_ROOT / "analytics_summary.json"

READINESS_WEIGHTS = {"sleep": 0.30, "hrv": 0.25, "hrv_weekly": 0.05, "body_battery": 0.40}
READINESS_MIN_COVERAGE = 0.50
RHR_MIN_DAYS = 14

# ---------------------------------------------------------------------------
# MAPPATURA ESATTA ZONE GARMIN DI RICCARDO (da AGENTS.md / screenshot Garmin):
#   1) Corsa Lenta / Main Easy Run  -> Garmin ZONA 3 (142-158 bpm)
#   2) Soglia / Ripetute            -> Garmin ZONA 4 (FC Soglia / Passo Soglia live)
#   3) Massimale / Sprint           -> Garmin ZONA 5 (FC Max live)
#   4) Recupero / Defaticamento     -> Garmin ZONA 1 o ZONA 2 (zone live)
# Usata per interpretare i lap e per istruire l'AI sui target di zona.
# ---------------------------------------------------------------------------
ATHLETE_ZONE_TARGETS = {
    "corsa_lenta": "Z3",          # easy run: zona abituale dell'atleta (Z2/Z3 aerobia)
    "soglia_ripetute": "Z4",      # ripetute/soglia: FC Soglia live Garmin
    "massimale_sprint": "Z5",     # sprint: FC Max live Garmin
    "recupero_defaticamento": "Z1/Z2",
}


def clamp(value, lo=0.0, hi=100.0):
    return max(lo, min(hi, value))


def _garmin_number(value, minimum=0, maximum=float("inf")):
    if (type(value) in (int, float) and isfinite(value)
            and minimum <= value <= maximum):
        return value
    return None


def body_battery_values(reports, day=None) -> list:
    """Restituisce i campioni validi della giornata in ordine cronologico."""
    if not isinstance(reports, list):
        return []
    readings = []
    for report in reports:
        if not isinstance(report, dict) or (day and report.get("date") not in (None, day)):
            continue
        for sample in report.get("bodyBatteryValuesArray") or []:
            if not isinstance(sample, (list, tuple)) or len(sample) < 2:
                continue
            stamp = _garmin_number(sample[0])
            value = _garmin_number(sample[1], maximum=100)
            if stamp is not None and value is not None:
                readings.append((stamp, value))
    readings.sort(key=lambda reading: reading[0])
    return [value for _, value in readings]


def workout_duration_with_recovery_s(workout) -> float:
    """Durata totale REALE di un workout, identica al totale Garmin.

    Somma di TUTTE le fasi (durata_s) + i tempi di recupero tra le ripetute
    (recupero_s × ripetizioni). I recuperi vengono conteggiati SOLO per le fasi
    che il converter (workout_sync._repeat_group) trasforma in RepeatGroup:
    tipo "repeat" con ripetizioni > 1. Per le altre fasi (incluse le "interval"
    con ripetizioni, trattate come step singolo) il recupero NON viene sommato,
    così la durata mostrata coincide sempre con quella del payload Garmin.
    """
    total = 0.0
    for f in (workout or {}).get("fasi") or []:
        if not isinstance(f, dict):
            continue
        total += float(f.get("durata_s") or 0)
        rec = f.get("recupero_s")
        rip = f.get("ripetizioni")
        if rec and rip and f.get("tipo") == "repeat" and int(rip) > 1:
            n = max(1, int(rip))
            # Se durata_s manca, stima il lavoro della singola ripetizione
            # da distanza_m × ritmo e lo moltiplica per n (come il converter)
            if not f.get("durata_s") and f.get("distanza_m") and f.get("ritmo"):
                m = re.match(r"^(\d+):(\d{2})", str(f.get("ritmo") or ""))
                if m:
                    sec_km = int(m.group(1)) * 60 + int(m.group(2))
                    total += sec_km * float(f["distanza_m"]) / 1000.0 * n
            total += float(rec) * n
    return total


def pace_to_display(sec_km):
    """284 -> '4:44'."""
    if not sec_km or sec_km <= 0:
        return "n/a"
    m, s = divmod(int(round(sec_km)), 60)
    return f"{m}:{s:02d}"


def hr_to_pct(hr, lthr):
    """HR come % della soglia lattato."""
    return round(100.0 * hr / lthr, 1) if hr and lthr else None


def overpacing_tolerance_sec_km(dist_m, dur_s):
    """TOLLERANZA OVERPACING PROPORZIONALE INVERSA ALLA DISTANZA (v81).

    Ritorna il numero di sec/km PIÙ VELOCI del target ancora tollerabili
    (None = tolleranza illimitata, nessuna penalità):
    - allunghi < 100m o < 30s        -> illimitata (0 penalità);
    - ripetute BREVI 100-400m        -> fino a 30 s/km più veloci;
    - ripetute MEDIE 400-1000m       -> fino a 20 s/km più veloci;
    - ripetute LUNGHE / tempo run
      (fase >= 1000m)                -> severa, fino a 10 s/km più veloci.
    """
    if dist_m < 100 or dur_s < 30:
        return None
    if dist_m < 400:
        return 30
    if dist_m < 1000:
        return 20
    return 10


class AnalyticsEngine:
    def __init__(self, config: dict = None, dump: dict = None, email: str = None):
        self.config = config or self._load_json(CONFIG_PATH)
        self.dump = dump or self._load_json(DUMP_PATH)
        self.athlete = dict(self.config.get("athlete", {}))
        self._user_email = (email or self.dump.get("email") or "").strip().lower()
        # Nome reale dell'utente dal dump (full_name / email): MAI hardcoded
        full_name = (((self.dump.get("endpoints") or {}).get("full_name") or {})
                     .get("data") or "").strip()
        if full_name:
            self.athlete["name"] = full_name
        elif not self.athlete.get("name") and self._user_email:
            self.athlete["name"] = self._user_email
        cfg_email = ((self.config.get("garmin") or {}).get("email") or "").strip().lower()
        self._is_owner = bool(self._user_email) and self._user_email == cfg_email
        self.zones = self.athlete.get("heart_rate_zones", {})
        self.eps = self.dump.get("endpoints", {})
        self._derive_thresholds()
        self._profile_from_dump()

    def _profile_from_dump(self) -> None:
        """Profilo anagrafico reale dal dump per gli account secondari
        (user_settings.userData): eta (birthDate), peso (g -> kg), altezza,
        FC Max (top Z5 derivata) e VO2max baseline — MAI quelli di Riccardo."""
        if self._is_owner:
            return
        us = (self._ep("user_settings") or {}).get("userData") or {}
        if us.get("birthDate"):
            try:
                from datetime import date
                bd = date.fromisoformat(str(us["birthDate"]))
                self.athlete["age"] = (date.today() - bd).days // 365
            except Exception:  # noqa: BLE001
                pass
        if us.get("weight"):
            w = float(us["weight"])
            self.athlete["weight_kg"] = round(w / 1000.0, 1) if w > 1000 else round(w, 1)
        if us.get("height"):
            self.athlete["height_cm"] = float(us["height"])
        # FC Max UFFICIALE live da Garmin (maxHeartRate); fallback: derivata
        # dalla FC Soglia (top Z5), MAI valori cablati (v87).
        us_max = us.get("maxHeartRate")
        if us_max:
            self.athlete["max_hr"] = int(us_max)
        elif self.lthr:
            self.athlete["max_hr"] = round(self.lthr * 1.06)  # top Z5 derivata
        vo2 = self._vo2max_real()
        if vo2:
            self.athlete["baseline_vo2max"] = vo2

    def _thresholds_from_user_settings(self):
        """Restituisce la soglia Garmin in bpm e il passo in secondi per km."""
        us = (self._ep("user_settings") or {}).get("userData") or {}
        heart_rate = _garmin_number(us.get("lactateThresholdHeartRate"), minimum=1)
        bpm = int(heart_rate) if heart_rate is not None else None
        speed = _garmin_number(us.get("lactateThresholdSpeed"))
        if not speed:
            return bpm, None
        # Il profilo Garmin può esprimere 3,611 m/s come 0,3611.
        speed_m_s = speed * 10 if speed < 1 else speed
        pace = _garmin_number(1000 / speed_m_s, minimum=60, maximum=1000)
        return bpm, pace

    def _vo2max_real(self, *, for_display: bool = False) -> Optional[float]:
        """Legge solo VO₂max di corsa. La schermata usa il valore visualizzato da Garmin."""
        latest = self._ep("max_metrics_latest")
        ts = self._ep("training_status_today") or {}
        records = (
            latest.get("generic") if isinstance(latest, dict) else None,
            (ts.get("mostRecentVO2Max") or {}).get("generic"),
        )
        for record in records:
            if isinstance(record, dict):
                displayed = _garmin_number(record.get("vo2MaxValue"), minimum=1)
                precise = _garmin_number(record.get("vo2MaxPreciseValue"), minimum=1)
                value = (displayed or precise) if for_display else (precise or displayed)
                if value is not None:
                    return float(value)
        us = (self._ep("user_settings") or {}).get("userData") or {}
        return _garmin_number(us.get("vo2MaxRunning"), minimum=1)

    def _vdot_threshold_pace(self, vo2max: float):
        """Passo Soglia (sec/km) dal VO2max con modello fisiologico VDOT:
        VO2 (ml/kg/min) = 3.5 + 0.2 * v(m/min); soglia ~88% del VO2max.
        Cross-check tabella Daniels: VDOT 50 -> ~4:56/km, VDOT 54 -> ~4:30/km."""
        vo2_thr = 0.88 * vo2max
        v_ms = (vo2_thr - 3.5) / 0.2 / 60.0
        return 1000.0 / v_ms if v_ms > 0 else None

    def _derive_thresholds(self) -> None:
        """Soglie FC e Passo DINAMICHE per l'utente connesso, priorita:
        1) user_settings.userData (soglie UFFICIALI Garmin sull'orologio):
           lactateThresholdHeartRate / lactateThresholdSpeed — qualsiasi utente;
        2) Owner senza valori sull'orologio -> config.json (profilazione
           utente, MAI costanti cablate nel codice);
        3) Account secondari -> fallback fisiologico VDOT dal VO2max reale
           (poi stima da race_predictions); MAI i valori di un altro utente.
        v87: NESSUN valore fisso hardcoded (rimossi 176 bpm / 284 s/km)."""
        us_lthr, us_pace = self._thresholds_from_user_settings()
        if us_lthr:
            self.lthr = us_lthr
        elif self._is_owner:
            self.lthr = self.athlete.get("lactate_threshold_hr")
        else:
            self.lthr = None   # 90% della FC Max osservata: calcolata in build_context

        if us_pace:
            self.threshold_pace = us_pace
        elif self._is_owner:
            self.threshold_pace = self.athlete.get("threshold_pace_sec_per_km")
        else:
            vo2 = self._vo2max_real()
            self.threshold_pace = self._vdot_threshold_pace(vo2) if vo2 else None
            if not self.threshold_pace:
                races = self._ep("race_predictions") or {}
                hm_sec = races.get("timeHalfMarathon")
                if hm_sec:
                    self.threshold_pace = hm_sec / 21.0975
                elif races.get("time10K"):
                    self.threshold_pace = (races["time10K"] / 10.0) * 1.10

        self.athlete["threshold_pace_sec_per_km"] = (
            round(self.threshold_pace, 1) if self.threshold_pace else None)
        self.athlete["threshold_pace_display"] = (
            pace_to_display(self.threshold_pace) if self.threshold_pace else None)

    def _zones_for_athlete(self) -> dict:
        """Zone cardio dell'atleta calcolate col metodo scientifico di Karvonen (HRR):
        Target HR = RHR + (% * (HR_max - RHR))
        - Z1 (50-60% HRR): Recupero Attivo / Spurgo
        - Z2 (60-70% HRR): Endurance Aerobica Pura
        - Z3 (70-80% HRR): Medio / Tempo Run
        - Z4 (80-90% HRR): Soglia Anaerobica / Ritmo Gara
        - Z5 (90-100% HRR): VO2Max / Massimale
        """
        max_hr = int(self.athlete.get("max_hr") or 200)
        rhr = int(self.athlete.get("rhr_baseline") or 50)
        # Se presente RHR live o dal summary lo consideriamo per la riserva
        rhr = int(self._resting_heart_rate() or rhr)
        hrr = max(1, max_hr - rhr)

        return {
            "z1_recovery": {
                "label": "Recupero Attivo / Spurgo",
                "min_pct_hrr": 50, "max_pct_hrr": 60,
                "bpm": [round(rhr + 0.50 * hrr), round(rhr + 0.60 * hrr)]
            },
            "z2_endurance": {
                "label": "Endurance Aerobica Pura",
                "min_pct_hrr": 60, "max_pct_hrr": 70,
                "bpm": [round(rhr + 0.60 * hrr), round(rhr + 0.70 * hrr)]
            },
            "z3_tempo": {
                "label": "Medio / Tempo Run",
                "min_pct_hrr": 70, "max_pct_hrr": 80,
                "bpm": [round(rhr + 0.70 * hrr), round(rhr + 0.80 * hrr)]
            },
            "z4_threshold": {
                "label": "Soglia Anaerobica / Ritmo Gara",
                "min_pct_hrr": 80, "max_pct_hrr": 90,
                "bpm": [round(rhr + 0.80 * hrr), round(rhr + 0.90 * hrr)]
            },
            "z5_vo2max": {
                "label": "VO2Max / Massimale",
                "min_pct_hrr": 90, "max_pct_hrr": 100,
                "bpm": [round(rhr + 0.90 * hrr), max_hr]
            },
        }

    def _extract_device(self) -> str:
        """Estrae il dispositivo Garmin reale dell'atleta."""
        devices = self._ep("devices") or []
        if isinstance(devices, list) and devices:
            d = devices[0]
            name = d.get("productDisplayName") or d.get("deviceName") or d.get("deviceType")
            if name:
                return f"Garmin {name}"
        return "Garmin Connect Device"

    def _extract_pbs(self) -> dict:
        """Estrae i Personal Best reali (1k, 5k, 10k, 21k) da Garmin Connect."""
        prs = self._ep("personal_records") or []
        pbs = {}
        if isinstance(prs, list):
            for pr in prs:
                type_name = str(pr.get("typeId") or pr.get("typeKey") or pr.get("prType") or "").lower()
                val_s = pr.get("value") or pr.get("prValue")
                if not val_s:
                    continue
                try:
                    sec = float(val_s)
                    m, s = divmod(int(round(sec)), 60)
                    h, m = divmod(m, 60)
                    formatted = f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"
                    if "1k" in type_name or type_name == "1":
                        pbs["1k"] = formatted
                    elif "5k" in type_name or type_name == "2":
                        pbs["5k"] = formatted
                    elif "10k" in type_name or type_name == "3":
                        pbs["10k"] = formatted
                    elif "half" in type_name or "21k" in type_name or type_name == "4":
                        pbs["21k"] = formatted
                    elif "marathon" in type_name or "42k" in type_name or type_name == "5":
                        pbs["42k"] = formatted
                except Exception:
                    pass

        # Fallback sui race predictions se i PR espliciti non sono ancora valorizzati
        races = self._ep("race_predictions") or {}
        if not pbs.get("5k") and races.get("time5K"):
            pbs["5k"] = pace_to_display(races["time5K"])
        if not pbs.get("10k") and races.get("time10K"):
            m, s = divmod(int(races["time10K"]), 60)
            pbs["10k"] = f"{m}:{s:02d}"
        if not pbs.get("21k") and races.get("timeHalfMarathon"):
            m, s = divmod(int(races["timeHalfMarathon"]), 60)
            h, m = divmod(m, 60)
            pbs["21k"] = f"{h}:{m:02d}:{s:02d}"

        return pbs

    @staticmethod
    def evaluate_decision_engine(summary: dict, planned_workout: Optional[dict] = None) -> dict:
        """Matrice decisionale fisiologica (Decision Engine MyPTAI 2.0):
        Regola 1: HRV LOW + BB < 35 + RHR > baseline+5 -> REST / CANCELLA SEDUTA DURA
        Regola 2: HRV UNBALANCED o Sleep < 50 -> DOWNGRADE A FONDO LENTO Z1 (max 138 bpm)
        Regola 3: HRV BALANCED + BB >= 60 + Sleep >= 7h -> CONFERMA QUALITÀ 100%
        Regola 4: Crash block controllato prima di pausa/vacanza -> ACWR 1.8-2.0
        """
        bio = summary.get("biometria", {})
        readiness = summary.get("readiness", {})
        r_level = readiness.get("readiness_level")
        r_score = readiness.get("readiness_score")
        if r_score is None:
            return {
                "decision": "INSUFFICIENT_DATA",
                "status": "INSUFFICIENT_DATA",
                "card_title": "Dati di recupero insufficienti",
                "explanation": "Aggiorna Garmin per completare le misure di oggi.",
                "action": "",
                "target_zone": None,
                "max_hr": None,
            }
        hrv_status = bio.get("hrv_status", "BALANCED")
        bb = bio.get("body_battery_attuale", 50) or 50
        sleep_score = bio.get("sleep_score", 70) or 70
        sleep_h = bio.get("sonno_h", 7.0) or 7.0
        rhr = bio.get("rhr")
        rhr_base = (readiness.get("rhr_reference") or {}).get("baseline_bpm")
        if rhr_base is None:
            rhr_base = (summary.get("atleta") or {}).get("fc_riposo_baseline")

        # Controllo Regola 1: Riposo Assoluto
        if hrv_status == "LOW" and bb < 35 and rhr is not None and rhr_base is not None and rhr > rhr_base + 5:
            return {
                "decision": "REST_TOTAL",
                "status": "DANGER_FATIGUE",
                "card_title": "🛑 Riposo Totale Obbligatorio",
                "explanation": f"Sistema parasimpatico depresso (HRV LOW), Body Battery a {bb} e battiti a riposo elevati ({rhr} bpm vs base {rhr_base} bpm). Alto rischio sovrallenamento/infortunio.",
                "action": "Cancella la seduta prevista. Riposo assoluto o 30' di camminata/mobilità blanda.",
                "target_zone": "Z1",
                "max_hr": 130
            }

        # Controllo Regola 2: Downgrade a Z1
        if hrv_status == "UNBALANCED" or sleep_score < 50 or bb < 45 or r_level == "RED":
            return {
                "decision": "DOWNGRADE_Z1",
                "status": "RECOVERY_NEEDED",
                "card_title": "⚠️ Taglio Intensità: Downgrade a Z1 Rigenerante",
                "explanation": f"Recupero non ottimale (HRV {hrv_status}, Sonno score {sleep_score}/100, Body Battery {bb}). Evitare lavori sopra soglia o picchi di lattato.",
                "action": "Converti la seduta in 6-8 km di Fondo Lento Z1 puro (125-140 bpm, non superare 138 bpm).",
                "target_zone": "Z1",
                "max_hr": 140
            }

        # Controllo Regola 3: Conferma Seduta Chiave
        if not readiness.get("estimated") and hrv_status == "BALANCED" and bb >= 60 and sleep_h >= 6.8:
            return {
                "decision": "CONFIRM_KEY",
                "status": "OPTIMAL_FORM",
                "card_title": "🚀 Forma Ottima: Procedi al 100%",
                "explanation": f"Fisiologia pienamente allineata (HRV {hrv_status}, Body Battery {bb}, Sonno {sleep_h}h score {sleep_score}). Sistema nervoso pronto ad assorbire il carico.",
                "action": "Esegui il workout previsto rispettando i ritmi target e il controllo cardio.",
                "target_zone": "Z4/Z5",
                "max_hr": None
            }

        # Fallback conservativo
        return {
            "decision": "MAINTAIN_MODERATE",
            "status": "MODERATE_READINESS",
            "card_title": "📊 Condizione Media: Esegui con Disciplina",
            "explanation": f"Parametri nella norma (Readiness {r_score}/100, BB {bb}). Mantieni il volume programmato senza forzare ritmi superiori al target.",
            "action": "Esegui la seduta prevista mantenendo il cardio sotto controllo.",
            "target_zone": "Z2/Z3",
            "max_hr": 160
        }

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _load_json(path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _ep(self, name):
        return self.eps.get(name, {}).get("data")

    def _sleep_today(self) -> dict:
        sleep = (self._ep("sleep_today") or {}).get("dailySleepDTO") or {}
        if self.dump.get("timestamp") and sleep.get("calendarDate") not in (None, self.dump["timestamp"]):
            return {}
        return sleep

    def _hrv_today(self) -> dict:
        hrv = (self._ep("hrv_today") or {}).get("hrvSummary") or {}
        if self.dump.get("timestamp") and hrv.get("calendarDate") not in (None, self.dump["timestamp"]):
            return {}
        return hrv

    def _resting_heart_rate(self):
        metrics = ((self._ep("rhr_today") or {}).get("allMetrics") or {}).get("metricsMap") or {}
        rows = metrics.get("WELLNESS_RESTING_HEART_RATE") or []
        return _garmin_number(rows[0].get("value"), minimum=1) if rows else None

    # ------------------------------------------------------------------ medie 7 notti
    def _sleep_day_vals(self, field: str, days: int = 7) -> list:
        """Valori del campo per ciascuna delle ultime N notti estratte dal probe
        (endpoint sleep_day_<ISO>). Ritorna lista dei soli valori reali presenti."""
        vals = []
        dump_date = self.dump.get("timestamp")
        end_date = date.fromisoformat(dump_date) if dump_date else date.today()
        for i in range(days):
            night = (end_date - timedelta(days=i)).isoformat()
            s = self._ep(f"sleep_day_{night}")
            if not s and i < 2:
                s = self._ep("sleep_today" if i == 0 else "sleep_yesterday")
            s = s or {}
            daily = s.get("dailySleepDTO") or {}
            if daily.get("calendarDate") not in (None, night):
                continue
            if field == "sleepTimeSeconds":
                v = _garmin_number(daily.get("sleepTimeSeconds"), minimum=1)
            else:  # sleepScores.overall.value
                scores = daily.get("sleepScores") or s.get("sleepScores") or {}
                v = _garmin_number((scores.get("overall") or {}).get("value"), maximum=100)
            if v is not None:
                vals.append(v)
        return vals

    def sleep_score_7n_media(self) -> Optional[float]:
        """Media Sleep Score (overall) degli ultimi 7 giorni (reale, dal probe)."""
        vals = self._sleep_day_vals("sleepScores")
        return round(sum(vals) / len(vals), 1) if vals else None

    def _zone_from_hr(self, hr):
        """Restituisce la zona (es. 'Z2') dato l'HR medio, o None."""
        if not hr:
            return None
        zones = self._zones_for_athlete()
        for z, d in sorted(zones.items(), key=lambda kv: kv[1].get("min_pct_hrr", kv[1].get("min_pct_lthr", kv[1].get("bpm", [0])[0]))):
            lo, hi = d["bpm"]
            if lo <= hr <= hi:
                return z
        if not zones:
            return None
        return "Z5" if hr > zones["z5_vo2max"]["bpm"][0] else "Z1"

    # ===================================================== 1. READINESS SCORE
    def _personal_hrv_band(self) -> Optional[tuple]:
        baseline = self._hrv_today().get("baseline")
        if not isinstance(baseline, dict):
            return None
        low, lower, upper = (
            _garmin_number(baseline.get(key), minimum=1)
            for key in ("lowUpper", "balancedLow", "balancedUpper")
        )
        if None in (low, lower, upper) or not low <= lower <= upper:
            return None
        return low, lower, upper

    def hrv_component(self, *, weekly=False) -> Optional[float]:
        """Valuta ciascuna misura HRV rispetto alla fascia personale Garmin."""
        summary = self._hrv_today()
        value = _garmin_number(summary.get("weeklyAvg" if weekly else "lastNightAvg"), minimum=1)
        band = self._personal_hrv_band()
        if band is None or value is None:
            return None
        low, lower, upper = band

        if weekly and value > upper:
            return round(clamp(100 * (1 - (value - upper) / max(1, upper - lower))), 1)
        if value >= lower:
            return 100.0
        return round(clamp((value - low) / max(1, lower - low) * 100), 1)

    def _sleep_score(self) -> Optional[float]:
        scores = self._sleep_today().get("sleepScores") or {}
        return _garmin_number((scores.get("overall") or {}).get("value"), maximum=100)

    def sleep_component(self) -> Optional[float]:
        """Score sonno Garmin, con stima sulla durata quando manca il punteggio."""
        overall = self._sleep_score()
        if overall is not None:
            return float(overall)
        duration = _garmin_number(self._sleep_today().get("sleepTimeSeconds"), minimum=1)
        if duration is None:
            return None
        return round(clamp(duration / 28800.0 * 100.0), 1)

    def _body_battery_values(self) -> list:
        return body_battery_values(self._ep("body_battery_today"), self.dump.get("timestamp"))

    def body_battery_component(self) -> Optional[float]:
        """Ultima Body Battery valida sulla scala Garmin 0-100."""
        values = self._body_battery_values()
        return values[-1] if values else None

    def _rhr_reference(self) -> dict:
        """Mediana e oscillazione abituale sulle misure dei 28 giorni precedenti."""
        day = self.dump.get("timestamp")
        end = date.fromisoformat(day) if day else date.today()
        start = (end - timedelta(days=28)).isoformat()
        rows = self._ep("rhr_history")
        by_day = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            day = row.get("calendarDate")
            values = row.get("values") or {}
            if isinstance(day, str) and start <= day < end.isoformat() and isinstance(values, dict):
                rhr = _garmin_number(values.get("restingHR"), minimum=1)
                if rhr is not None:
                    by_day[day] = rhr

        count = len(by_day)
        reference = {"days": count, "min_days": RHR_MIN_DAYS, "baseline_bpm": None, "tolerance_bpm": None}
        if count >= RHR_MIN_DAYS:
            baseline = median(by_day.values())
            mad = median(abs(value - baseline) for value in by_day.values())
            reference.update(baseline_bpm=baseline, tolerance_bpm=max(2, 2 * mad))
        return reference

    def compute_readiness(self) -> dict:
        """Readiness su 100, usando i componenti disponibili e i loro pesi relativi."""
        components = {
            "hrv": self.hrv_component(),
            "hrv_weekly": self.hrv_component(weekly=True),
            "sleep": self.sleep_component(),
            "body_battery": self.body_battery_component(),
        }
        available = {key: weight for key, weight in READINESS_WEIGHTS.items() if components[key] is not None}
        coverage = sum(available.values())
        reference = self._rhr_reference()
        score = level = None
        weights = {}
        if coverage >= READINESS_MIN_COVERAGE:
            weights = {key: weight / coverage for key, weight in available.items()}
            score = clamp(sum(weights[key] * components[key] for key in weights))
            level = "GREEN" if score >= 67 else "AMBER" if score >= 34 else "RED"

        estimated = score is not None and (
            len(available) < len(READINESS_WEIGHTS) or self._sleep_score() is None
        )
        return {
            "readiness_score": round(score, 1) if score is not None else None,
            "readiness_level": level,
            "estimated": estimated,
            "components": components,
            "hrv_calibrating": self._personal_hrv_band() is None,
            "rhr_reference": reference,
            "rhr_adjustment_penalty": 0,
            "weights": weights,
        }

    # ===================================================== 2. CLASSIFICAZIONE LAP
    def classify_lap(self, lap: dict) -> dict:
        """Classifica un singolo lap in ZONE/INTENSITY basato su HR + ritmo.

        Regole (basate sulle zone Garmin reali di Riccardo, ritmo come conferma):
        - Z1 (HR<124) o ritmo >7:00/km con HR basso -> RECOVERY_Z1 (recupero camminato)
        - Z2 (124-141)                               -> EASY_Z2 (corsa facile/recovery)
        - Z3 (142-158)                               -> EASY_Z3 (corsa lenta: zona abituale di Riccardo)
        - Z4+ (>=159) o ritmo <=4:20/km              -> REPEAT_THRESHOLD (ripetuta/soglia)
        """
        avg_speed = lap.get("averageSpeed")  # m/s
        distance = lap.get("distance", 0)    # metri
        duration = lap.get("duration", 0)    # secondi
        avg_hr = lap.get("averageHR")
        max_hr = lap.get("maxHR")

        pace_sec_km = None
        if avg_speed:
            pace_sec_km = 1000.0 / avg_speed
        elif distance and duration:
            pace_sec_km = duration / (distance / 1000.0)

        zone = self._zone_from_hr(avg_hr)
        lthr = self.lthr

        # Soglie HR dinamiche per-utente: owner = valori ufficiali Garmin
        # (124/142/159 bpm da config), account secondari = %% della FC Soglia
        # stimata; se non derivabili (None) la classificazione usa solo il passo.
        if self._is_owner:
            z2_lo, z3_lo, z4_lo = 124, 142, 159
        elif lthr:
            z2_lo, z3_lo, z4_lo = round(lthr * 0.70), round(lthr * 0.81), round(lthr * 0.90)
        else:
            z2_lo = z3_lo = z4_lo = None

        # Decisione a cascata: prima il ritmo (più robusto per camminate/burst),
        # poi le zone HR come conferma.
        if pace_sec_km and pace_sec_km > 420:          # > 7:00/km: camminata/recupero
            kind = "RECOVERY_Z1"
        elif avg_hr and z2_lo is not None and avg_hr < z2_lo:   # HR sotto Z2: recupero
            kind = "RECOVERY_Z1"
        elif pace_sec_km and pace_sec_km <= 260:        # <= 4:20/km: lavoro veloce (ripetuta/burst)
            kind = "REPEAT_THRESHOLD"
        elif avg_hr and z4_lo is not None:
            if avg_hr >= z4_lo:                         # Z4+ (soglia/ripetute)
                kind = "REPEAT_THRESHOLD"
            elif avg_hr >= z3_lo:                       # Z3 (corsa lenta aerobica)
                kind = "EASY_Z3"
            else:                                       # Z2 (corsa facile)
                kind = "EASY_Z2"
        elif pace_sec_km:
            if pace_sec_km <= 300:                      # <= 5:00/km: lavoro sostenuto
                kind = "EASY_Z3"
            else:                                       # 5:00-7:00/km: corsa facile
                kind = "EASY_Z2"
        else:
            kind = "UNKNOWN"

        return {
            "lap_index": lap.get("lapIndex"),
            "distance_m": round(distance, 1) if distance else None,
            "duration_s": duration,
            "pace_sec_km": round(pace_sec_km, 1) if pace_sec_km else None,
            "pace_display": pace_to_display(pace_sec_km),
            "avg_hr": avg_hr,
            "max_hr": max_hr,
            "hr_pct_lthr": hr_to_pct(avg_hr, lthr),
            "zone": zone,
            "intensity": kind,
        }

    def analyze_activity(self, activity_id) -> dict:
        """Classifica tutti i lap di un'attività e determina il tipo di sessione."""
        splits = self._ep(f"activity_splits_{activity_id}") or {}
        laps = splits.get("lapDTOs", [])
        if not laps:
            return {"activity_id": activity_id, "error": "nessun lap disponibile"}

        classified = [self.classify_lap(lap) for lap in laps]
        kinds = [c["intensity"] for c in classified]
        counts = {k: kinds.count(k) for k in set(kinds)}

        total_dist = sum(c["distance_m"] or 0 for c in classified)
        total_time = sum(c["duration_s"] or 0 for c in classified)
        avg_hr = [c["avg_hr"] for c in classified if c["avg_hr"]]
        pace_list = [c["pace_sec_km"] for c in classified if c["pace_sec_km"]]

        # Tipo di sessione dominante (mappatura esatta zone Riccardo)
        n_repeat = counts.get("REPEAT_THRESHOLD", 0)
        n_z2 = counts.get("EASY_Z2", 0)
        n_z3 = counts.get("EASY_Z3", 0)
        n_rec = counts.get("RECOVERY_Z1", 0)
        avg_hr_val = round(sum(avg_hr) / len(avg_hr)) if avg_hr else None
        lthr = self.lthr  # v87: soglia SEMPRE live (mai valore cablato)

        if n_repeat >= 1:
            # CLASSIFICAZIONE FISIOLOGICA (v65): allunghi/strides finali (1 o più
            # lap brevi) in una corsa complessivamente lenta NON la trasformano in
            # progressione o ripetute. Se la FC media dell'intera corsa è in Z1/Z2
            # (avg_hr < LTHR * 0.88) e la distanza è < 9 km -> CORSA DI
            # SCIOGLIMENTO (Z2) (allunghi finali leggeri); altrimenti:
            # - 2+ lap intensi -> INTERVALLI/RIPETUTE
            # - 1 lap intenso  -> PROGRESSIONE/TEMPO
            if lthr and avg_hr_val and total_dist < 9000 and avg_hr_val < lthr * 0.88:
                session_type = "CORSA DI SCIOGLIMENTO (Z2)"
            elif n_repeat >= 2:
                session_type = "INTERVALLI/RIPETUTE"   # anche se con lap facili in mezzo
            else:
                session_type = "PROGRESSIONE/TEMPO"      # corsa con allungo a soglia
        elif n_rec >= 1 and n_z2 == 0 and n_z3 == 0:
            session_type = "RECUPERO ATTIVO"
        elif n_z2 + n_z3 >= 1:
            session_type = "CORSA LENTA Z2/Z3"
        else:
            session_type = "VARIO"

        return {
            "activity_id": activity_id,
            "session_type": session_type,
            "total_distance_m": round(total_dist),
            "total_time_s": int(total_time),
            "avg_pace_sec_km": round(sum(pace_list) / len(pace_list), 1) if pace_list else None,
            "avg_pace_display": pace_to_display(sum(pace_list) / len(pace_list)) if pace_list else "n/a",
            "avg_hr": avg_hr_val,
            "max_hr": max((c["max_hr"] for c in classified if c["max_hr"]), default=None),
            "lap_counts": counts,
            "laps": classified,
        }

    def analyze_recent_running(self, limit=5) -> list:
        """Analizza le attività di corsa recenti presenti nel dump."""
        activities = self._ep("recent_activities") or []
        running = [
            a for a in activities
            if (a.get("activityType") or {}).get("typeKey") == "running"
        ][:limit]

        results = []
        for act in running:
            aid = act.get("activityId")
            if not aid:
                continue
            analysis = self.analyze_activity(aid)
            if "error" in analysis:
                continue
            analysis["start_time_local"] = act.get("startTimeLocal")
            analysis["activity_name"] = act.get("activityName")
            # Corsa GUIDATA dall'orologio Garmin (workout seguendo un piano
            # dal calendario): il DTO espone workout_id/workoutId solo in quel caso.
            analysis["garmin_workout_used"] = bool(act.get("workout_id") or act.get("workoutId"))
            results.append(analysis)
        return results

    # ===================================================== 3. TRACKING PROGRESSI
    def track_progress(self, running_analysis: list) -> dict:
        """Confronta baseline dell'utente connesso (FC Max/FC Soglia/Passo/VO2Max
        dinamici, dal config per l'owner o stimati dal dump per i secondari)
        con i valori osservati nei dati reali."""
        vo2_now = self._vo2max_real()
        vo2_base = self.athlete.get("baseline_vo2max")

        # FC Max osservata: massimo maxHR tra le attività recenti
        fc_max_obs = max((a.get("max_hr") for a in running_analysis if a.get("max_hr")), default=None)

        # FC Soglia stimata: HR medio dei lap classificati Z4+ (lavoro a soglia)
        threshold_hrs = [
            c["avg_hr"] for a in running_analysis for c in a.get("laps", [])
            if c.get("intensity") == "REPEAT_THRESHOLD" and c.get("avg_hr")
        ]
        fc_soglia_est = round(sum(threshold_hrs) / len(threshold_hrs)) if threshold_hrs else None

        # Passo Soglia: confronto con il ritmo HM delle race predictions
        # (per atleti ben allenati il passo soglia ~ passo HM)
        races = self._ep("race_predictions") or {}
        hm_sec = races.get("timeHalfMarathon")
        hm_pace = (hm_sec / 21.0975) if hm_sec else None

        def _trend(current, baseline, tol=1.5):
            if current is None or baseline is None:
                return "n/a"
            delta = current - baseline
            if abs(delta) < tol:
                return "STABILE"
            return "IN MIGLIORAMENTO" if delta < 0 else "IN REGRESSO"

        vo2_delta = (round(vo2_now - vo2_base, 1) if vo2_now is not None and vo2_base else None)
        pace_delta = (round(hm_pace - self.threshold_pace, 1) if hm_pace else None)

        return {
            "fc_max": {"baseline": self.athlete.get("max_hr"), "osservata": fc_max_obs,
                       "delta": (fc_max_obs - self.athlete.get("max_hr")) if fc_max_obs else None},
            "fc_soglia": {"baseline": self.lthr, "stimata_da_lap_z4": fc_soglia_est,
                          "delta": (fc_soglia_est - self.lthr) if fc_soglia_est else None},
            "passo_soglia": {"baseline_sec_km": self.threshold_pace,
                             "baseline_display": self.athlete.get("threshold_pace_display"),
                             "stimato_da_hm_sec_km": round(hm_pace, 1) if hm_pace else None,
                             "stimato_da_hm_display": pace_to_display(hm_pace),
                             "delta_sec_km": pace_delta,
                             "trend": _trend(hm_pace, self.threshold_pace)},
            "vo2max": {"baseline": vo2_base, "attuale": vo2_now, "delta": vo2_delta,
                       "trend": _trend(vo2_now, vo2_base)},
        }

    # ===================================================== 4. CONTESTO COMPATTO
    def _garmin_readiness_score(self):
        readings = self._ep("training_readiness_today")
        if isinstance(readings, dict):
            readings = [readings]
        if not isinstance(readings, list):
            return None
        day = self.dump.get("timestamp")
        valid = [
            row for row in readings
            if isinstance(row, dict)
            and (not day or row.get("calendarDate") in (None, day))
            and _garmin_number(row.get("score"), minimum=1, maximum=100) is not None
        ]
        latest = max(valid, key=lambda row: row.get("timestamp") or row.get("timestampLocal") or "", default=None)
        return latest["score"] if latest else None

    def _training_load(self, status_device: dict):
        day = self.dump.get("timestamp")
        load = status_device.get("acuteTrainingLoadDTO") or {}
        acute = chronic = ratio = None
        if not day or status_device.get("calendarDate") in (None, day):
            acute = _garmin_number(load.get("dailyTrainingLoadAcute"))
            chronic = _garmin_number(load.get("dailyTrainingLoadChronic"))
            ratio = _garmin_number(load.get("dailyAcuteChronicWorkloadRatio"))

        # I dump precedenti possono contenere i carichi in una risposta separata.
        if acute is None and chronic is None and ratio is None:
            legacy = self._ep("trainingload_today") or {}
            if isinstance(legacy, dict):
                nested = legacy.get("trainingLoad")
                if isinstance(nested, dict):
                    legacy = nested
                acute = _garmin_number(legacy.get("acuteLoad"))
                chronic = _garmin_number(legacy.get("chronicLoad"))
                ratio = _garmin_number(legacy.get("acwr"))
        if ratio is None and acute is not None and chronic is not None and chronic > 0:
            ratio = round(acute / chronic, 2)
        return acute, chronic, ratio

    def build_context(self) -> dict:
        """Sintesi numerica compatta per DeepSeek (solo dati reali, niente testo libero)."""
        readiness = self.compute_readiness()
        running = self.analyze_recent_running()
        # FC Soglia dinamica per gli account secondari: 90%% della FC Max reale
        # osservata nei lap delle attivita (mai il valore cablato di Riccardo)
        if not self._is_owner and self.lthr is None:
            fc_max_obs = max((a.get("max_hr") for a in running if a.get("max_hr")), default=None)
            if fc_max_obs:
                self.lthr = round(fc_max_obs * 0.90)
        if not self._is_owner and self.lthr:
            self.athlete["max_hr"] = round(self.lthr * 1.06)  # top Z5 derivata
        progress = self.track_progress(running)

        ds = self._sleep_today()
        sleep_scores = ds.get("sleepScores") or {}
        sleep_seconds = _garmin_number(ds.get("sleepTimeSeconds"), minimum=1)
        sleep_nights = self._sleep_day_vals("sleepTimeSeconds")
        hrv = self._hrv_today()
        hrv_last_night = _garmin_number(hrv.get("lastNightAvg"), minimum=1)
        hrv_weekly = _garmin_number(hrv.get("weeklyAvg"), minimum=1)
        bb_vals = self._body_battery_values()
        races = self._ep("race_predictions") or {}
        ts = self._ep("training_status_today") or {}
        load = ((ts.get("mostRecentTrainingLoadBalance") or {})
                .get("metricsTrainingLoadBalanceDTOMap", {}) or {})
        load_first = next(iter(load.values()), {}) if load else {}

        status_devices = (ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}
        status_device = max(
            (row for row in status_devices.values() if isinstance(row, dict)),
            key=lambda row: (row.get("calendarDate") or "", bool(row.get("primaryTrainingDevice"))),
            default={},
        )
        epoc_acuto, epoc_cronico, acwr = self._training_load(status_device)

        # ---- Prompt dinamico per-utente (zero fallback cablati su Riccardo) ----
        nome = self.athlete.get("name") or "atleta"
        z = self._zones_for_athlete()
        z3, z4, z5 = z.get("z3_tempo", {}), z.get("z4_threshold", {}), z.get("z5_vo2max", {})
        z1, z2 = z.get("z1_recovery", {}), z.get("z2_endurance", {})
        if z4 and z4.get("bpm") and self.lthr:
            z4_hi = self.lthr
            z4_lo = z4["bpm"][0]
            z3_lo, z3_hi = z3["bpm"]
            z2_lo, z2_hi = z2["bpm"]
            z1_lo, _ = z1["bpm"]
            zones_txt = (
                f"corsa lenta -> Z3 ({z3_lo}-{z3_hi} bpm), "
                f"ripetute/soglia -> Z4 ({z4_lo}-{z4_hi} bpm), "
                f"massimale/sprint -> Z5 (> {z4_hi} bpm), "
                f"recupero/defaticamento -> Z1/Z2 ({z1_lo}-{z2_hi} bpm)")
        else:
            zones_txt = "zone FC non derivabili: usa i passi target indicati"
        pace_disp = self.athlete.get("threshold_pace_display")
        if self._is_owner and self.lthr:
            soglia_txt = (
                f"REGOLA SOGLIA UFFICIALE GARMIN: FC Soglia = {self.lthr} bpm e Passo Soglia = "
                f"{pace_disp} sono valori UFFICIALI calcolati da Garmin, SEMPRE FISSI: non "
                f"alterarli e non sostituirli con stime locali. Usali sempre come riferimento Z4.")
        elif self.lthr and pace_disp:
            soglia_txt = (
                f"REGOLA SOGLIA {nome}: FC Soglia = {self.lthr} bpm e Passo Soglia = {pace_disp} "
                f"(stima dai dati Garmin reali dell'atleta). Usali come riferimento Z4.")
        elif self.lthr:
            soglia_txt = f"REGOLA SOGLIA: FC Soglia = {self.lthr} bpm (riferimento Z4)."
        else:
            soglia_txt = "Soglia non disponibile: basati sul passo e sulla percezione dello sforzo."

        official_hr, official_pace = self._thresholds_from_user_settings()
        return {
            "garmin_data_date": self.dump.get("timestamp"),
            "garmin_fetched_at": self.dump.get("fetched_at"),
            "garmin_training_readiness": self._garmin_readiness_score(),
            "garmin_vo2max": self._vo2max_real(for_display=True),
            "garmin_official_threshold": {
                "heart_rate": official_hr,
                "pace_sec_per_km": official_pace,
            },
            "atleta": {
                "nome": self.athlete.get("name"),
                "eta": self.athlete.get("age"),
                "peso_kg": self.athlete.get("weight_kg"),
                "dispositivo": self._extract_device(),
                "pb": self._extract_pbs(),
                "fc_max": self.athlete.get("max_hr"),
                "fc_soglia": self.lthr,
                "passo_soglia": self.athlete.get("threshold_pace_display"),
                "vo2max_baseline": self.athlete.get("baseline_vo2max"),
                "zone_hr": {z: d["bpm"] for z, d in self._zones_for_athlete().items()},
                "zone_garmin_target": ATHLETE_ZONE_TARGETS,
            },
            "dispositivo": self._extract_device(),
            "personal_records": self._extract_pbs(),
            "readiness": readiness,
            "biometria": {
                "sonno_h": round(sleep_seconds / 3600.0, 1) if sleep_seconds is not None else None,
                "sonno_secondi": sleep_seconds,
                "sleep_score": _garmin_number(
                    (sleep_scores.get("overall") or {}).get("value"), maximum=100),
                "sleep_feedback": ds.get("sleepScoreFeedback"),
                "deep_pct": (sleep_scores.get("deepPercentage") or {}).get("value"),
                "rem_pct": (sleep_scores.get("remPercentage") or {}).get("value"),
                "stress_notte": _garmin_number(ds.get("avgSleepStress"), maximum=100),
                "hrv_last_night": hrv_last_night,
                "hrv_weekly": hrv_weekly,
                "hrv_baseline": hrv.get("baseline"),
                "hrv_7n": hrv_weekly,
                "hrv_status": hrv.get("status"),
                "body_battery_attuale": bb_vals[-1] if bb_vals else None,
                "body_battery_picco": max(bb_vals) if bb_vals else None,
                "rhr": self._resting_heart_rate(),
                "stress_giorno": _garmin_number(
                    (self._ep("user_summary_today") or {}).get("averageStressLevel"), maximum=100),
                "sonno_7n": round(sum(sleep_nights) / len(sleep_nights) / 3600.0, 2) if sleep_nights else None,
                # Media sui minuti interi visualizzati per ciascuna notte in Garmin Connect.
                "sonno_7n_minuti": int(sum(s // 60 for s in sleep_nights) // len(sleep_nights)) if sleep_nights else None,
                "sonno_7n_notti": len(sleep_nights),
                "sleep_score_7n": self.sleep_score_7n_media(),
            },
            "allenamento": {
                "vo2max_attuale": progress["vo2max"]["attuale"],
                "training_status_feedback": status_device.get("trainingStatusFeedbackPhrase"),
                "carico_aerobico_low": load_first.get("monthlyLoadAerobicLow"),
                "carico_aerobico_high": load_first.get("monthlyLoadAerobicHigh"),
                "carico_anaerobico": load_first.get("monthlyLoadAnaerobic"),
                "target_anaerobico": [load_first.get("monthlyLoadAnaerobicTargetMin"),
                                      load_first.get("monthlyLoadAnaerobicTargetMax")],
                "load_feedback": load_first.get("trainingBalanceFeedbackPhrase"),
                "race_predictions": {
                    "5k_s": races.get("time5K"), "10k_s": races.get("time10K"),
                    "hm_s": races.get("timeHalfMarathon"), "maratona_s": races.get("timeMarathon"),
                },
                # Carico Garmin acuto (7 giorni), cronico (28 giorni) e relativo rapporto.
                "epoc_acuto_7gg": epoc_acuto,
                "epoc_cronico_28gg": epoc_cronico,
                "acwr": acwr,
            },
            "progressi": progress,
            "attivita_recenti": [{
                "data": a.get("start_time_local", "")[:10],
                "tipo": a.get("session_type"),
                "distanza_km": round((a.get("total_distance_m") or 0) / 1000.0, 2),
                "tempo_s": a.get("total_time_s"),
                "ritmo_medio": a.get("avg_pace_display"),
                "hr_medio": a.get("avg_hr"),
                "hr_max": a.get("max_hr"),
                "lap": a.get("lap_counts"),
                # v79: elenco COMPLETO dei lap reali dell'orologio Garmin
                # (da running_analysis[0]["laps"] — classifica per-zone con
                # lap_index, distance_m, duration_s, pace, HR medio/max).
                "laps": a.get("laps"),
                "garmin_workout_used": bool(a.get("garmin_workout_used")),
            } for a in running],
            "istruzioni": [
                "Genera UN SOLO allenamento strutturato JSON coerente con readiness e progressi.",
                f"Usa SOLO questi dati reali. Zone cardio di {nome} (mappatura esatta): {zones_txt}.",
                f"{soglia_txt}",
                "Output JSON: {scheda: {nome, obiettivo, readiness_richiesta, fasi: [{tipo, durata_s, distanza_m, zona, ritmo, hr_target}]}}",
            ],
        }

    def build_context_text(self) -> str:
        """Versione testo ultra-compatta (per debug/telegram)."""
        c = self.build_context()
        r = c["readiness"]
        b = c["biometria"]
        a = c["allenamento"]
        score = r["readiness_score"]
        readiness_text = "Readiness non disponibile" if score is None else f"Readiness {score}/100 ({r['readiness_level']})"
        if r["estimated"]:
            readiness_text += " · provvisoria"
        lines = [
            f"{readiness_text} "
            f"[HRV {r['components']['hrv']} | Sleep {r['components']['sleep']} | "
            f"HRV 7g {r['components']['hrv_weekly']} | BB {r['components']['body_battery']}]",
            f"Sonno {b['sonno_h']}h (score {b['sleep_score']}, deep {b['deep_pct']}%, rem {b['rem_pct']}%) | "
            f"HRV notte {b['hrv_last_night']} (weekly {b['hrv_weekly']}, status {b['hrv_status']}) | "
            f"BB {b['body_battery_attuale']}/{b['body_battery_picco']} | RHR {b['rhr']}",
            f"VO2Max {a['vo2max_attuale']} | {a['load_feedback']}",
            "Sessione:" + " | ".join(
                f"{x['data']} {x['tipo']} {x['distanza_km']}km @{x['ritmo_medio']} HR{x['hr_medio']}/{x['hr_max']}"
                for x in c["attivita_recenti"][:3]),
        ]
        return "\n".join(lines)

    # ===================================================== RUN PRINCIPALE
    def run(self) -> dict:
        context = self.build_context()
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(context, f, indent=2, ensure_ascii=False)
        return context


# =============================================================================
# INJURY LAB 2.0 - Anatomia, Referto del Coach e Impatto Biomeccanico
# =============================================================================
# Ogni regione anatomica ha: impatto_base (0-100) sulla corsa, diagnosi di
# precisione, cause probabili, consigli fisioterapici e mobilità.
# Il referto è generato LOCALMENTE (zero token, zero allucinazioni): è un
# database curato di medicina sportiva, non una chiamata a DeepSeek.

INJURY_ANATOMY = {
    "head": {
        "impatto": 10, "nome": "Testa/Nuca",
        "diagnosi": "Cefalea da sforzo o tensione cervico-cranica (trapezio superiore e suboccipitali)",
        "cause": ["Tensione muscolare cervicale", "Disidratazione o accumulo di lattato", "Postura da corsa con capo proteso"],
        "fisioterapia": ["Riposo attivo 24-48h se cefalea persistente", "Ghiaccio 10' su nuca", "Idratazione e magnesio"],
        "mobilita": ["Stretching suboccipitale 30s x2", "Rotazioni cervicali lente", "Auto-massaggio con pallina su trapezi"],
    },
    "neck": {
        "impatto": 15, "nome": "Collo",
        "diagnosi": "Cervicalgia meccanica da ipertono degli scaleni e del trapezio superiore",
        "cause": ["Postura da smartphone/PC", "Tensione durante le ripetute (spalle alte)", "Stress accumulato"],
        "fisioterapia": ["Calore locale 10' prima della corsa", "Tecarterapia o massoterapia se dolore >48h", "Correzione posturale in corsa (spalle basse)"],
        "mobilita": ["Chin tuck 10 ripetizioni", "Inclinazioni laterali 20s x2", "Rilassamento trapezio con foam roller"] ,
    },
    "shoulder": {
        "impatto": 20, "nome": "Spalla",
        "diagnosi": "Sovraccarico del sovraspinato o sindrome da conflitto sub-acromiale (spalla da corsa)",
        "cause": ["Braccia rigide o pompaggio eccessivo", "Debolezza del cingolo scapolare", "Tecnica di bracciata sbilanciata"],
        "fisioterapia": ["Riposo dal sollevamento pesi 48h", "Ghiaccio 15' sull'acromion", "Esercizi pendolari senza carico"],
        "mobilita": ["Rotazioni esterne con elastico 3x12", "Stretching deltoide posteriore", "Scapole a V contro muro 3x10"],
    },
    "upper_arm": {
        "impatto": 5, "nome": "Braccio/Tricipite",
        "diagnosi": "DOMS del tricipite o contrattura del braccio da bracciata rigida",
        "cause": ["Accumulo di acido lattico", "Pompaggio braccia troppo energico", "Allenamento di forza recente"],
        "fisioterapia": ["Stretching dolce post-corsa", "Massaggio decontratturante", "Riposo attivo (camminata)"],
        "mobilita": ["Stretching tricipite 30s x2 per lato", "Rotazioni di braccia", "Foam roller su tricipite"],
    },
    "elbow": {
        "impatto": 10, "nome": "Gomito",
        "diagnosi": "Epicondilite/ epitrocleite da gesto ripetuto (rara da corsa, spesso da forza)",
        "cause": ["Sovraccarico da pesi o lavoro manuale", "Bracciata con gomito bloccato", "Tecnica di pompaggio errata"],
        "fisioterapia": ["Ghiaccio 10' x3/die sull'epicondilo", "Taping elastico", "Riposo dalla forza 48h"],
        "mobilita": ["Stretching estensori avambraccio", "Flesso-estensione gomito senza carico 3x15", "Impugnature isometriche leggere"],
    },
    "forearm": {
        "impatto": 5, "nome": "Avambraccio",
        "diagnosi": "Ipertono dei flessori/estensori dell'avambraccio (da bracciata o forza)",
        "cause": ["Sovraccarico da presa", "Bracciata contratta", "Lavoro al PC prolungato"],
        "fisioterapia": ["Stretching dolce", "Massaggio longitudinale", "Pausa dal grip training"],
        "mobilita": ["Stretching flessori avambraccio 30s", "Rotazioni polso senza carico 2x15", "Apri/chiudi mano lento 20 ripetizioni"],
    },
    "wrist": {
        "impatto": 10, "nome": "Polso",
        "diagnosi": "Tendinopatia dei flessori del carpo o sindrome del tunnel carpale iniziale",
        "cause": ["Gesti ripetuti", "Bracciata con polso piegato", "Forza recente (push-up, trazioni)"],
        "fisioterapia": ["Ghiaccio 10' sul polso", "Tutore notturno se formicolii", "Riposo dai carichi 48h"],
        "mobilita": ["Circonduzioni polso 2x10", "Stretching flessori", "Estensione resistita con elastico leggero"],
    },
    "hand": {
        "impatto": 5, "nome": "Mano",
        "diagnosi": "Contusione palmare o contrattura dei muscoli intrinseci della mano",
        "cause": ["Caduta o trauma diretto", "Grip eccessivo", "Bracciata a pugno chiuso"],
        "fisioterapia": ["Ghiaccio 10' x3/die", "Riposo dalla presa", "Compressione leggera"],
        "mobilita": ["Apri/chiudi mano 3x15", "Stretching delle dita", "Pallina morbida da spremere 3x10"],
    },
    "fingers": {
        "impatto": 0, "nome": "Dita della mano",
        "diagnosi": "Distorsione o contusione delle articolazioni interfalangee (stiramento dei tendini flessori)",
        "cause": ["Trauma diretto o incastro", "Grip intenso", "Gestualità fine ripetuta"],
        "fisioterapia": ["Ghiaccio 5' x3/die", "Taping di immobilizzazione del dito", "Riposo funzionale"],
        "mobilita": ["Flesso-estensione attiva delle dita", "Pinza pollice-indice con elastico", "Esercizi di destrezza (pallina piccola)"],
    },
    "chest": {
        "impatto": 20, "nome": "Petto",
        "diagnosi": "Dolorabilità intercostale o contrattura del grande pettorale",
        "cause": ["Respirazione toracica forzata", "Sforzo di forza recente", "Tosse prolungata"],
        "fisioterapia": ["Calore locale 10'", "Stretching dolce respirando", "Evitare sforzi di spinta 48h"],
        "mobilita": ["Respirazione diaframmatica 3' ", "Apertura petto su muro 30s x2", "Stretching pettorale 30s per lato"],
    },
    "abs": {
        "impatto": 20, "nome": "Addominali",
        "diagnosi": "Contrattura del retto addominale o strappo di I grado dell'obliquo",
        "cause": ["Core instabile durante la corsa", "Sforzo di forza (addominali, plank)", "Torsioni brusche"],
        "fisioterapia": ["Ghiaccio 10' se dolore acuto", "Riposo dal core work 48h", "Respirazione controllata"],
        "mobilita": ["Dead bug lenti 3x8", "Stretching obliqui 30s per lato", "Plank breve senza dolore 3x20s"],
    },
    "upper_back": {
        "impatto": 25, "nome": "Schiena alta / Trapezi",
        "diagnosi": "Ipertono del trapezio medio e dei romboidi (postura da corsa o da scrivania)",
        "cause": ["Postura seduta prolungata", "Braccia tese in corsa", "Debolezza del core"],
        "fisioterapia": ["Calore 10' sulla zona", "Massoterapia mirata", "Pausa dal rematore 48h"],
        "mobilita": ["Shrug senza carico 3x12", "Stretching trapezi 30s x2", "Rotazioni toraciche da seduto 2x10"],
    },
    "lower_back": {
        "impatto": 75, "nome": "Zona lombare",
        "diagnosi": "Lombalgia meccanica da sovraccarico o contrattura del quadrato dei lombi",
        "cause": ["Core debole", "Impatto ripetuto in corsa", "Accorciamento ileopsoas"],
        "fisioterapia": ["Calore 15' + ghiaccio alternati", "Evitare corsa ad alta intensità", "Tecnica di corsa con bacino neutro"],
        "mobilita": ["Stretching ileopsoas 30s x2 per lato", "Cat-camel 10 ripetizioni lente", "Ponte gluteo 3x12"],
    },
    "hip": {
        "impatto": 80, "nome": "Anca",
        "diagnosi": "Borsite trocanterica o sindrome dello scatto (coxa saltans) dell'ileo-tibiale",
        "cause": ["Disfunzione dei glutei", "Accorciamento del tensore della fascia lata", "Volume in salita eccessivo"],
        "fisioterapia": ["Ghiaccio 10' sul trocantere", "Foam roller sulla banda ileo-tibiale", "Ridurre discese e salite"],
        "mobilita": ["Clamshell 3x15 per lato", "Stretching piriforme 30s x2", "Ponte su una gamba 3x8"],
    },
    "glute": {
        "impatto": 60, "nome": "Gluteo",
        "diagnosi": "Sovraccarico del gluteo medio o contrattura profonda del piriforme",
        "cause": ["Debolezza glutea (running con glutei 'spenti')", "Seduta prolungata", "Cambi di ritmo bruschi"],
        "fisioterapia": ["Foam roller mirato 2'", "Massaggio profondo", "Attivazione glutea pre-corsa"],
        "mobilita": ["Glute bridge 3x15", "Monopodalico su step 3x10", "Stretching piriforme 30s x2"],
    },
    "quad": {
        "impatto": 85, "nome": "Quadricipite",
        "diagnosi": "Contrattura del retto femorale o sindrome femoro-rotulea iniziale",
        "cause": ["Sovraccarico in salita", "Accorciamento del quadricipite", "Aumento volume troppo rapido"],
        "fisioterapia": ["Ghiaccio 10' post-corsa", "Stretching dolce", "Ridurre salite e cambi di ritmo"],
        "mobilita": ["Stretching quadricipite 30s x2", "Squat isometrico a parete 3x20s", "Foam roller anteriore coscia"],
    },
    "hamstring": {
        "impatto": 90, "nome": "Ischiocrurali",
        "diagnosi": "Contrattura o stiramento di I grado del bicipite femorale",
        "cause": ["Allunghi o sprint senza riscaldamento", "Debolezza eccentrica", "Pavimento pelvico/ core instabile"],
        "fisioterapia": ["Riposo relativo 48-72h", "Ghiaccio 15' x3/die", "Niente sprint né allunghi"],
        "mobilita": ["Nordic curl eccentrico (solo se indolore)", "Stretching ischiocrurali 30s x2", "Cat-camel e ponte gluteo"],
    },
    "knee": {
        "impatto": 95, "nome": "Ginocchio",
        "diagnosi": "Sindrome femoro-rotulea (ginocchio del corridore) o tendinopatia rotulea",
        "cause": ["Disfunzione del vasto mediale", "Sovraccarico di volume", "Scarpe usurate o pronazione"],
        "fisioterapia": ["Ghiaccio 15' sulla rotula", "Niente corse in discesa", "Valutazione plantare/calzatura"],
        "mobilita": ["Stretching quadricipite e TFL", "Lavoro eccentrico su step (solo indolore)", "Attivazione gluteo medio 3x12"],
    },
    "shin": {
        "impatto": 95, "nome": "Tibia",
        "diagnosi": "Periostite tibiale (MTSS - Medial Tibial Stress Syndrome)",
        "cause": ["Aumento di volume rapido", "Superfici dure", "Pronazione eccessiva o scarpe rigide"],
        "fisioterapia": ["Ghiaccio 15' x3/die sul bordo tibiale", "Ridurre l'impatto (terreno morbido)", "Valutazione biomeccanica del piede"],
        "mobilita": ["Caviglia: circonduzioni e resistenza con elastico", "Sollevamenti talloni 3x15", "Stretching polpaccio 30s x2"],
    },
    "calf": {
        "impatto": 90, "nome": "Polpaccio",
        "diagnosi": "Contrattura del gastrocnemio o stiramento di I grado del soleo",
        "cause": ["Riscaldamento insufficiente", "Tecnica di appoggio avampodalica", "Sovraccarico in salita"],
        "fisioterapia": ["Ghiaccio 12' post-corsa", "Niente sprint né balzi", "Massaggio profondo del polpaccio"],
        "mobilita": ["Stretching polpaccio muro 30s x2", "Eccentrico sul gradino (solo indolore)", "Foam roller polpaccio 2'"],
    },
    "achilles": {
        "impatto": 100, "nome": "Tendine d'Achille",
        "diagnosi": "Tendinopatia achillea (inserzionale o del corpo del tendine) o paratenonite",
        "cause": ["Sovraccarico di chilometraggio", "Scarpe con drop ridotto senza adattamento", "Debolezza del polpaccio (eccentrica)"],
        "fisioterapia": ["RIPOSO dalla corsa: cross-training (nuoto/bici) fino a indolenza", "Ghiaccio 15' x3/die sul tendine", "Protocollo eccentrico Alfredson solo dopo valutazione"],
        "mobilita": ["Eccentrico lento su gradino (solo se prescritto)", "Stretching polpaccio dolce 30s", "Lavoro di mobilità caviglia con elastico"],
    },
    "ankle": {
        "impatto": 90, "nome": "Caviglia",
        "diagnosi": "Distorsione laterale residua o tendinopatia dei peronei",
        "cause": ["Terreno irregolare", "Debolezza stabilizzatori", "Recidiva di distorsione precedente"],
        "fisioterapia": ["Ghiaccio 12' se gonfia", "Taping o cavigliera elastica", "Evitare trail e terreni sconnessi"],
        "mobilita": ["Equilibrio monopodalico 3x30s", "Circonduzioni caviglia 2x15", "Resistenza eversione con elastico 3x12"],
    },
    "foot": {
        "impatto": 85, "nome": "Piede",
        "diagnosi": "Fascite plantare iniziale o sovraccarico dell'avampiede (metatarsalgia)",
        "cause": ["Arco plantare poco supportato", "Chilometraggio in aumento", "Calzature rigide o troppo flessibili"],
        "fisioterapia": ["Ghiaccio 10' sulla pianta (bottiglia congelata)", "Ridurre impatto e salite", "Valutazione plantare"],
        "mobilita": ["Stretching fascia plantare 30s x2", "Towel curls 3x15", "Sollevamenti arco plantare 3x10"],
    },
    "toes": {
        "impatto": 100, "nome": "Dita del piede",
        "diagnosi": "Sesamoidite o stiramento dei tendini flessori delle dita (dito a martello iniziale)",
        "cause": ["Scarpe strette o drop alto", "Spinta dell'alluce sovraccarica", "Trauma diretto (urti)"],
        "fisioterapia": ["Ghiaccio 10' x3/die", "Scarpe a punta larga (toe box ampia)", "Riposo dallo sprint e dalle salite"],
        "mobilita": ["Apri/chiudi le dita 3x15", "Towel curls con asciugamano", "Stretching dei flessori plantari"],
    },
}

# Mappa part_id (della sagoma SVG) -> regione anatomica
INJURY_PART_REGION = {
    "head_front": "head", "head_back": "head",
    "neck_front": "neck", "neck_back": "neck", "trapezius": "upper_back",
    "shoulder_l": "shoulder", "shoulder_r": "shoulder",
    "arm_upper_l": "upper_arm", "arm_upper_r": "upper_arm",
    "tricep_l": "upper_arm", "tricep_r": "upper_arm",
    "elbow_l": "elbow", "elbow_r": "elbow",
    "forearm_l": "forearm", "forearm_r": "forearm",
    "wrist_l": "wrist", "wrist_r": "wrist",
    "hand_l": "hand", "hand_r": "hand",
    "fingers_l": "fingers", "fingers_r": "fingers",
    "chest": "chest", "abdominals": "abs",
    "upper_back": "upper_back", "lower_back": "lower_back",
    "hip_l": "hip", "hip_r": "hip",
    "glute_l": "glute", "glute_r": "glute",
    "quad_l": "quad", "quad_r": "quad",
    "hamstring_l": "hamstring", "hamstring_r": "hamstring",
    "knee_l": "knee", "knee_r": "knee",
    "shin_l": "shin", "shin_r": "shin",
    "calf_l": "calf", "calf_r": "calf",
    "achilles_l": "achilles", "achilles_r": "achilles",
    "ankle_l": "ankle", "ankle_r": "ankle",
    "foot_l": "foot", "foot_r": "foot",
    "toes_l": "toes", "toes_r": "toes",
}


def _injury_region(part_id: str) -> str:
    """Regione anatomica di una parte della sagoma (default: la parte stessa)."""
    return INJURY_PART_REGION.get(part_id, part_id)


def injury_impact_pct(injury: dict) -> int:
    """Impatto biomeccanico % sulla corsa: base della regione × (severità/5).
    Es.: dito mano sev.5 -> 0% · Achille/dito piede sev.5 -> 100%."""
    region = _injury_region(str(injury.get("part") or ""))
    base = INJURY_ANATOMY.get(region, {}).get("impatto", 20)
    sev = max(1, min(5, int(injury.get("severity") or 1)))
    return round(base * sev / 5)


def injury_impact_level(pct: int) -> str:
    """Livello di raccomandazione in base all'impatto %."""
    if pct >= 80:
        return "stop"      # 🚫 blocco corsa / cross-training
    if pct >= 50:
        return "reduce"    # ⚠️ riduci volume e intensità
    if pct >= 25:
        return "modify"    # 🟡 modifica volume
    if pct > 0:
        return "mild"      # 🟢 quasi inalterata
    return "none"          # 🟢 corsa inalterata

INJURY_IMPACT_LABELS = {
    "stop": "🚫 Blocco corsa · riposo / cross-training",
    "reduce": "⚠️ Riduci volume e intensità (solo corsa facile)",
    "modify": "🟡 Modifica volume: accorcia la seduta",
    "mild": "🟢 Corsa quasi inalterata, monitora il fastidio",
    "none": "🟢 Corsa inalterata: nessun impatto biomeccanico",
}


def generate_injury_report(part_id: str, severity: int, label: str = None) -> dict:
    """Referto del Coach per un fastidio: diagnosi di precisione, cause
    probabili, consigli fisioterapici e mobilità + impatto sulla corsa.
    Deterministico (database locale), zero token, zero allucinazioni."""
    region = _injury_region(part_id)
    anat = INJURY_ANATOMY.get(region) or INJURY_ANATOMY["foot"]
    pct = injury_impact_pct({"part": part_id, "severity": severity})
    lvl = injury_impact_level(pct)
    return {
        "regione": anat["nome"],
        "diagnosi": anat["diagnosi"],
        "cause": list(anat["cause"]),
        "fisioterapia": list(anat["fisioterapia"]),
        "mobilita": list(anat["mobilita"]),
        "impatto_pct": pct,
        "impatto_level": lvl,
        "raccomandazione": INJURY_IMPACT_LABELS[lvl],
        "disclaimer": "Referto informativo generato dal Coach: non sostituisce il parere di un medico o fisioterapista.",
    }


def active_injuries_impact(injuries: list) -> dict:
    """Impatto biomeccanico COMPLESSIVO degli infortuni attivi (il più
    restrittivo vince): massimo impatto %, parte responsabile e raccomandazione."""
    attivi = [i for i in (injuries or []) if not i.get("resolved")]
    if not attivi:
        return {"active": False, "max_pct": 0, "level": "none",
                "label": None, "raccomandazione": INJURY_IMPACT_LABELS["none"]}
    worst = max(attivi, key=lambda i: injury_impact_pct(i))
    pct = injury_impact_pct(worst)
    lvl = injury_impact_level(pct)
    return {
        "active": True,
        "max_pct": pct,
        "level": lvl,
        "label": worst.get("label") or worst.get("part"),
        "part": worst.get("part"),
        "raccomandazione": INJURY_IMPACT_LABELS[lvl],
        "count": len(attivi),
    }


if __name__ == "__main__":
    engine = AnalyticsEngine()
    summary = engine.run()

    print("=" * 70)
    print("FASE 2 - ANALYTICS ENGINE | " + date.today().isoformat())
    print("=" * 70)
    print(engine.build_context_text())
    print("-" * 70)

    print("\n📈 TRACKING PROGRESSI (baseline -> osservato):")
    for k, v in summary["progressi"].items():
        print(f"  {k}: {v}")

    print("\n🏃 CLASSIFICAZIONE SESSIONI RECENTI:")
    for a in summary["attivita_recenti"]:
        print(f"  {a['data']} | {a['tipo']} | {a['distanza_km']}km @{a['ritmo_medio']} | HR {a['hr_medio']}/{a['hr_max']}")
        if a["lap"]:
            print(f"      lap: {a['lap']}")

    print(f"\n📄 Contesto compatto DeepSeek salvato in: {OUTPUT_PATH}")
