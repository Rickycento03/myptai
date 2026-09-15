#!/usr/bin/env python3
"""
workout_sync.py - FASE 4: Sincronizzazione Calendario Garmin
============================================================
Converte il piano generato dall'AI (workout_plan.json, output di FASE 3)
nel formato Workout Garmin Connect (API workout-service) e lo carica
direttamente sul calendario dell'atleta:

  FASI AI (tipo, durata, distanza, zona, ritmo, ripetizioni, recupero)
      |
      v
  Garmin Workout JSON:
    - fase singola  -> ExecutableStepDTO (warmup/interval/recovery/cooldown/main)
    - ripetute      -> RepeatGroupDTO (interval + recovery, numberOfIterations)
    - target zona   -> heart.rate.zone (Z1-Z5 di Riccardo da config.json)
    - durata/distanza -> endCondition TIME/DISTANCE
      |
      v
  POST /workout-service/workout            (crea il workout nel Gear/Libreria)
  POST /workout-service/schedule/{id}      (lo agenda sul calendario)

CLI:
  python3 src/workout_sync.py --dry-run                    # stampa il JSON Garmin, nessuna chiamata API
  python3 src/workout_sync.py --date 2026-08-12            # convert + upload + schedule sulla data
  python3 src/workout_sync.py                              # idem, data default = domani
  python3 src/workout_sync.py --list                       # elenca i workout esistenti
  python3 src/workout_sync.py --delete <workoutId>         # elimina un workout
"""

import argparse
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
PLAN_PATH = PROJECT_ROOT / "workout_plan.json"

# =============================================================================
# Costanti API Garmin (workout-service) — allineate ai valori correnti
# (vedi python-garminconnect, test_workout_constants: issue #333/#370).
# =============================================================================
SPORT_RUNNING = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}

# stepTypeId / stepTypeKey / displayOrder
STEP_TYPES = {
    "warmup":   (1, "warmup", 1),
    "cooldown": (2, "cooldown", 2),
    "interval": (3, "interval", 3),
    "recovery": (4, "recovery", 4),
    "rest":     (5, "rest", 5),
    "repeat":   (6, "repeat", 6),
    "other":    (7, "other", 7),
    "main":     (8, "main", 8),
}

COND_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time",
             "displayOrder": 2, "displayable": True}
COND_DISTANCE = {"conditionTypeId": 3, "conditionTypeKey": "distance",
                 "displayOrder": 3, "displayable": True}
COND_ITERATIONS = {"conditionTypeId": 7, "conditionTypeKey": "iterations",
                   "displayOrder": 7, "displayable": False}

TARGET_NONE = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target",
               "displayOrder": 1}
TARGET_HR_ZONE = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone",
                  "displayOrder": 4}
# Target VELOCITÀ/PASSO: fasi di qualità (ripetute, intervalli, allunghi/strides,
# Z4/Z5) e recuperi attivi con ritmo espresso. I valori (targetValueOne/Two =
# range di velocità in m/s) vengono aggiunti a livello step da _executable.
TARGET_SPEED_ZONE = {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "speed.zone",
                     "displayOrder": 2}

# Ampiezza (s/km) della fascia di ritmo attorno al target espresso: il range
# speed.zone diventa [ritmo + PACE_BUFFER_S, ritmo - PACE_BUFFER_S] convertito
# in m/s (es. 4:20/km -> 4:10-4:30/km -> 3.704-4.000 m/s).
PACE_BUFFER_S = 10

# Zona AI -> numero zona Garmin (targetValueOne di heart.rate.zone).
# FALLBACK: usato solo se il tipo di fase non è coperto da PHASE_TO_GARMIN_ZONE.
ZONE_TO_NUMBER = {"Z1": 1, "Z2": 2, "Z3": 3, "Z4": 4, "Z5": 5}
ZONE_NUMBER_TO_LABEL = {1: "Z1", 2: "Z2", 3: "Z3", 4: "Z4", 5: "Z5"}

# ---------------------------------------------------------------------------
# MAPPATURA ESATTA ZONE GARMIN DI RICCARDO (da AGENTS.md / screenshot Garmin):
#   1) Corsa Lenta / Main Easy Run  -> Garmin ZONA 3 (zoneNumber: 3, HR 142-158)
#   2) Soglia / Ripetute            -> Garmin ZONA 4 (zoneNumber: 4, FC 176, ~4:44/km)
#   3) Massimale / Sprint           -> Garmin ZONA 5 (zoneNumber: 5, FC Max 200)
#   4) Recupero / Defaticamento     -> Garmin ZONA 1 o ZONA 2 (HR 114-141)
# La mappatura è per TIPO DI FASE: l'etichetta zona dell'AI (es. "Z2" sulle
# easy run) NON va usata 1:1, altrimenti la Corsa Lenta finisce in Zona 2.
# ---------------------------------------------------------------------------
PHASE_TO_GARMIN_ZONE = {
    "warmup": 2,        # riscaldamento facile: Z1/Z2
    "easy_run": 3,      # Corsa Lenta / Main Easy Run -> Garmin ZONA 3
    "repeat": 4,        # Soglia / Ripetute -> Garmin ZONA 4
    "interval": 4,
    "tempo": 4,
    "progression": 4,
    "recovery": 1,     # Recupero / Defaticamento -> ZONA 1 (o 2)
    "cooldown": 1,
}
# Tipi di fase "duri": se l'AI marca zona Z5 (massimale/sprint) li forziamo a ZONA 5.
HARD_PHASE_TYPES = {"repeat", "interval", "tempo", "progression"}

# Mappa fase AI -> stepType Garmin (chiavi di STEP_TYPES)
PHASE_TO_STEP = {
    "warmup":      "warmup",
    "easy_run":    "main",      # corsa lenta continua
    "repeat":      "interval",  # il blocco ripetuto (dentro RepeatGroupDTO)
    "interval":    "interval",
    "tempo":       "interval",
    "progression": "interval",
    "recovery":    "recovery",
    "cooldown":    "cooldown",
}

NAME_MAX = 40          # limite workoutName (Garmin)
DESC_MAX = 100         # limite description del workout
STEP_DESC_MAX = 60     # limite description di ogni step


# =============================================================================
# CONVERTER: piano AI -> Workout Garmin
# =============================================================================
class WorkoutConverter:
    """Converte un piano AI (dict di workout_plan.json) nel payload Garmin.

    Le zone HR target usano la mappatura esatta di Riccardo per tipo di fase
    (PHASE_TO_GARMIN_ZONE), NON l'etichetta zona dell'AI.
    """

    def __init__(self, config: dict = None):
        self.config = config or self._load_config()
        self.zone_bpm = self._ordered_zone_bpm()

    @staticmethod
    def _load_config() -> dict:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def _ordered_zone_bpm(self) -> dict:
        """{1: [114,123], 2: [124,141], 3: [142,158], ...} in ordine crescente."""
        zones = self.config.get("athlete", {}).get("heart_rate_zones", {})
        ordered = {}
        for i, d in enumerate(
                sorted(zones.values(), key=lambda v: v.get("min_pct_hrr", v.get("min_pct_lthr", v.get("bpm", [0])[0]))), start=1):
            ordered[i] = d.get("bpm", [None, None])
        return ordered

    def _garmin_zone_number(self, fase: dict):
        """Numero zona Garmin per la fase, con la mappatura esatta di Riccardo.

        Priorità:
        1. zona AI "Z5" su fasi dure -> ZONA 5 (massimale/sprint, FC Max 200);
        2. tipo di fase -> PHASE_TO_GARMIN_ZONE (easy_run=3, soglia/ripetute=4, ...);
        3. fallback: etichetta zona AI 1:1.
        """
        tipo = fase.get("tipo")
        zona = fase.get("zona")
        if zona == "Z5" and tipo in HARD_PHASE_TYPES:
            return 5
        if tipo in PHASE_TO_GARMIN_ZONE:
            return PHASE_TO_GARMIN_ZONE[tipo]
        return ZONE_TO_NUMBER.get(zona)

    def convert(self, plan: dict) -> dict:
        """plan: può essere il wrapper {workout: {...}} o il piano nudo."""
        workout = plan.get("workout", plan) if isinstance(plan, dict) else {}
        if not isinstance(workout, dict):
            raise ValueError("Piano non valido: atteso un oggetto JSON")

        fasi = workout.get("fasi") or []
        if not fasi:
            raise ValueError("Piano senza fasi: impossibile costruire il workout Garmin")

        steps, order = [], 0
        for fase in fasi:
            if fase.get("tipo") == "repeat" and (fase.get("ripetizioni") or 1) > 1:
                step, order = self._repeat_group(fase, order)
            else:
                step, order = self._executable(fase, order)
            steps.append(step)

        total_s = self._estimated_duration(fasi)

        return {
            "workoutName": (workout.get("nome") or "Allenamento Coach AI")[:NAME_MAX],
            "description": self._workout_description(workout),
            "sportType": dict(SPORT_RUNNING),
            "estimatedDurationInSecs": int(total_s),
            "workoutSegments": [{
                "segmentOrder": 1,
                "sportType": dict(SPORT_RUNNING),
                "workoutSteps": steps,
            }],
            "author": {},
        }

    # ------------------------------------------------------------------ step
    def _executable(self, fase: dict, order: int) -> tuple:
        """Step eseguibile singolo. order è il contatore globale (1-based)."""
        order += 1
        step_type_key = PHASE_TO_STEP.get(fase.get("tipo"), "other")
        step_type_id, step_type_key_id, display_order = STEP_TYPES[step_type_key]
        zone_num = self._garmin_zone_number(fase)
        target = self._target(fase, zone_num)

        step = {
            "type": "ExecutableStepDTO",
            "stepOrder": order,
            "stepType": {"stepTypeId": step_type_id, "stepTypeKey": step_type_key_id,
                         "displayOrder": display_order},
            "targetType": target,
            "endCondition": self._end_condition(fase),
            "endConditionValue": self._end_condition_value(fase),
        }
        # Garmin vuole targetValueOne/Two a livello step (NON dentro targetType).
        # a) heart.rate.zone -> RANGE BPM della zona (es. Z2 -> 124-141), NON il
        #    numero zona (altrimenti l'app mostra "4--- bpm");
        # b) speed.zone -> RANGE di velocità in m/s ricavato dal ritmo espresso
        #    nella fase (targetValueOne = velocità MINIMA/ritmo più lento,
        #    targetValueTwo = velocità MASSIMA/ritmo più veloce);
        # c) no.target -> nessun valore (recuperi a corpo libero).
        tkey = target.get("workoutTargetTypeKey")
        if tkey == "speed.zone":
            sec_km = self._pace_to_sec(fase.get("ritmo"))
            if sec_km:
                slow = sec_km + PACE_BUFFER_S          # ritmo più lento (+10s)
                fast = max(sec_km - PACE_BUFFER_S, 60)  # ritmo più veloce (-10s)
                step["targetValueOne"] = round(1000.0 / slow, 3)
                step["targetValueTwo"] = round(1000.0 / fast, 3)
        elif tkey == "heart.rate.zone" and zone_num is not None:
            bpm = self.zone_bpm.get(zone_num)
            if bpm and bpm[0] and bpm[1]:
                step["targetValueOne"] = float(bpm[0])
                step["targetValueTwo"] = float(bpm[1])
                target["targetValueOne"] = float(bpm[0])
                target["targetValueTwo"] = float(bpm[1])
            else:
                step["targetValueOne"] = float(zone_num)
                step["targetValueTwo"] = None
        desc = self._step_description(fase, zone_num)
        if desc:
            step["description"] = desc
        return step, order

    def _repeat_group(self, fase: dict, order: int) -> tuple:
        """Blocco ripetute: RepeatGroupDTO con interval + recovery interni.

        Convenzione stepOrder (come python-garminconnect): numerazione globale
        unica — il gruppo prende il prossimo numero, i due step interni
        i successivi; il chiamante avanza il contatore di 3.
        """
        iterations = int(fase.get("ripetizioni") or 1)
        order += 1
        group_order = order

        # Convenzione AI (verificata sugli output reali):
        #   - durata_s  = TEMPO TOTALE del blocco (durata_totale_s = somma delle fasi)
        #   - distanza_m = distanza della SINGOLA ripetizione (es. "5x1000m" -> distanza_m: 1000)
        # Quindi si divide SOLO durata_s per iterations; distanza_m va usata così com'è
        # (dividendola la ripetuta diventerebbe 200m invece di 1000m).
        inner_fase = dict(fase)
        if inner_fase.get("durata_s"):
            inner_fase["durata_s"] = round(float(inner_fase["durata_s"]) / iterations, 1)

        interval, order = self._executable(inner_fase, order)
        inner = [interval]

        recupero_s = fase.get("recupero_s")
        if recupero_s:
            recovery, order = self._executable({
                "tipo": "recovery",
                "durata_s": recupero_s,
                "distanza_m": None,
                "zona": "Z1",
                "ritmo": None,
                "note": "Recupero attivo tra le ripetute (Z1/Z2).",
            }, order)
            inner.append(recovery)

        group = {
            "type": "RepeatGroupDTO",
            "stepOrder": group_order,
            "stepType": {"stepTypeId": STEP_TYPES["repeat"][0],
                         "stepTypeKey": STEP_TYPES["repeat"][1],
                         "displayOrder": STEP_TYPES["repeat"][2]},
            "numberOfIterations": iterations,
            "workoutSteps": inner,
            "endCondition": dict(COND_ITERATIONS),
            "endConditionValue": float(iterations),
            "smartRepeat": False,
        }
        return group, order

    # ------------------------------------------------------------------ target
    # Tipi di fase "di qualità" -> target VELOCITÀ (speed.zone) invece del cardio.
    SPEED_TARGET_TYPES = {"repeat", "interval", "tempo", "progression"}

    def _target(self, fase: dict, zone_num) -> dict:
        """Definizione del tipo di target Garmin per la fase:
        a) Warmup / Cooldown / Corsa Facile Z2-Z3 -> heart.rate.zone (range BPM);
        b) Ripetute / Intervalli / Allunghi (repeat, interval, strides, Z4, Z5)
           -> speed.zone (range di ritmo in m/s, se la fase ha un ritmo);
        c) Recuperi tra le ripetute -> speed.zone a ritmo facile/trotto se il
           ritmo è espresso, altrimenti no.target.
        I valori (targetValueOne/Two) vengono aggiunti a livello step da
        _executable, come richiesto dall'API Garmin."""
        tipo = fase.get("tipo")
        zona = fase.get("zona")
        pace_ok = bool(fase.get("ritmo") and self._pace_to_sec(fase.get("ritmo")))
        if tipo == "recovery":
            # c) recupero attivo: guida a ritmo facile/trotto se espresso
            return dict(TARGET_SPEED_ZONE) if pace_ok else dict(TARGET_NONE)
        if tipo in self.SPEED_TARGET_TYPES or zona in {"Z4", "Z5"}:
            # b) fasi di qualità: target velocità (fallback cardio se senza ritmo)
            return dict(TARGET_SPEED_ZONE) if pace_ok else dict(TARGET_HR_ZONE)
        if zone_num is None:
            return dict(TARGET_NONE)
        # a) warmup/cooldown/easy run: target cardio sulla zona Garmin
        return dict(TARGET_HR_ZONE)

    # ------------------------------------------------------------------ end condition
    @staticmethod
    def _end_condition(fase: dict) -> dict:
        """TIME se prevale la durata, DISTANCE se la fase è definita in metri.
        Override esplicito end_condition=="time": forza TIME anche con
        distanza_m (es. allunghi/strides 15s per 60m -> durata deterministica
        nel payload Garmin, indipendente dal ritmo)."""
        if fase.get("end_condition") == "time":
            return dict(COND_TIME)
        if fase.get("distanza_m"):
            return dict(COND_DISTANCE)
        return dict(COND_TIME)

    @staticmethod
    def _end_condition_value(fase: dict) -> float:
        if fase.get("end_condition") == "time":
            return float(fase.get("durata_s") or 0)
        if fase.get("distanza_m"):
            return float(fase["distanza_m"])
        return float(fase.get("durata_s") or 0)

    # ------------------------------------------------------------------ testi
    @staticmethod
    def _workout_description(workout: dict) -> str:
        parts = [workout.get("obiettivo") or "", workout.get("note") or ""]
        if workout.get("readiness_richiesta"):
            parts.append(f"Readiness: {workout['readiness_richiesta']}")
        text = " | ".join(p for p in parts if p).replace("\n", " ")
        return text[:DESC_MAX] or "Allenamento generato da MyPT AI."

    def _step_description(self, fase: dict, zone_num=None) -> str:
        bits = []
        if fase.get("distanza_m"):
            bits.append(f"{fase['distanza_m']}m")
        if fase.get("ritmo"):
            bits.append(f"ritmo {fase['ritmo']}")
        label = ZONE_NUMBER_TO_LABEL.get(zone_num) if zone_num else None
        if label:
            bpm = self.zone_bpm.get(zone_num)
            hr = f" HR {bpm[0]}-{bpm[1]}" if bpm and bpm[0] and bpm[1] else ""
            bits.append(f"zona {label}{hr}")
        elif fase.get("zona"):
            bits.append(f"zona {fase['zona']}")
        if fase.get("hr_target") and not label:
            bits.append(f"HR {fase['hr_target'][0]}-{fase['hr_target'][1]}")
        text = " ".join(bits)
        if fase.get("note") and len(text) < STEP_DESC_MAX // 2:
            text = f"{text} | {fase['note']}".strip(" |")
        return text[:STEP_DESC_MAX]

    # ------------------------------------------------------------------ durata
    PACE_RE = re.compile(r"^(\d{1,2}):([0-5]\d)/km$")

    @staticmethod
    def _pace_to_sec(ritmo: str):
        """'4:44/km' -> 284 (secondi al km)."""
        m = WorkoutConverter.PACE_RE.match(ritmo or "")
        return int(m.group(1)) * 60 + int(m.group(2)) if m else None

    @classmethod
    def _pace_to_speed(cls, ritmo: str):
        """'4:30/km' -> 3.704 m/s (velocità media della fascia, 3 decimali)."""
        sec = cls._pace_to_sec(ritmo)
        return round(1000.0 / sec, 3) if sec else None

    @classmethod
    def _estimated_duration(cls, fasi: list) -> float:
        """Stima durata totale.
        - fasi singole: durata_s.
        - ripetute: durata_s (TOTALE lavoro del blocco, convenzione AI)
          + recupero_s * iterazioni. Se durata_s assente, stima il lavoro della
          SINGOLA ripetizione da distanza_m*ritmo e lo moltiplica per n.
        """
        total = 0.0
        for f in fasi:
            if f.get("tipo") == "repeat" and int(f.get("ripetizioni") or 1) > 1:
                # Stessa condizione del converter (_repeat_group): solo con
                # ripetizioni > 1 si crea il RepeatGroup con i recuperi
                n = max(1, int(f.get("ripetizioni") or 1))
                rec = float(f.get("recupero_s") or 0)
                work = float(f.get("durata_s") or 0)
                if work:
                    # Convenzione AI: durata_s = tempo TOTALE del blocco
                    total += work + rec * n
                elif f.get("distanza_m") and f.get("ritmo"):
                    # distanza_m = SINGOLA ripetizione: il lavoro totale è n×
                    sec_km = cls._pace_to_sec(f["ritmo"])
                    if sec_km:
                        total += sec_km * float(f["distanza_m"]) / 1000.0 * n + rec * n
                else:
                    total += rec * n
            else:
                # Fase singola (o repeat con ripetizioni=1): solo la durata,
                # nessun recupero (il converter genera un ExecutableStep)
                total += float(f.get("durata_s") or 0)
        return total

    # ------------------------------------------------------------------ display
    @staticmethod
    def format_garmin(gw: dict) -> str:
        lines = [f"🏃 {gw['workoutName']} | durata stimata {gw['estimatedDurationInSecs'] // 60} min"]
        if gw.get("description"):
            lines.append(f"   📝 {gw['description']}")
        for seg in gw.get("workoutSegments", []):
            for step in seg.get("workoutSteps", []):
                inner = (f"{step['endCondition']['conditionTypeKey']}="
                         f"{WorkoutConverter._fmt_value(step)}")
                if step.get("targetType", {}).get("workoutTargetTypeKey") == "speed.zone":
                    inner += f", PACE {step.get('targetValueOne')}-{step.get('targetValueTwo')} m/s"
                elif step.get("targetType", {}).get("workoutTargetTypeKey") == "heart.rate.zone":
                    inner += f", HR-zone {step.get('targetValueOne')}"
                lines.append(f"   • [{step['stepOrder']}] {step['type']}: "
                             f"{step['stepType']['stepTypeKey']} ({inner})")
                if step.get("type") == "RepeatGroupDTO":
                    for inner_step in step.get("workoutSteps", []):
                        lines.append(f"        └─ [{inner_step['stepOrder']}] {inner_step['stepType']['stepTypeKey']} "
                                     f"({inner_step['endCondition']['conditionTypeKey']}="
                                     f"{WorkoutConverter._fmt_value(inner_step)})")
        return "\n".join(lines)

    @staticmethod
    def _fmt_value(step: dict) -> str:
        val = step.get("endConditionValue")
        if val is None:
            return "-"
        cond = step.get("endCondition", {}).get("conditionTypeKey")
        if cond == "distance":
            return f"{val}m"
        if cond == "iterations":
            return f"{val}x"
        return f"{val}s"


# =============================================================================
# SYNC: autenticazione + upload + scheduling su Garmin Connect
# =============================================================================
class WorkoutSync:
    def __init__(self, config: dict = None, plan_path: Path = PLAN_PATH):
        self.config = config or self._load(CONFIG_PATH)
        self.plan_path = plan_path
        self._client = None

    @staticmethod
    def _load(path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def load_plan(self) -> dict:
        return self._load(self.plan_path)

    # ------------------------------------------------------------------ auth
    def client(self):
        """Garmin() autenticato (tokenstore se disponibile, altrimenti login diretto)."""
        if self._client is None:
            from garminconnect import Garmin

            garmin = self.config.get("garmin", {})
            email, password = garmin.get("email"), garmin.get("password")
            if not email or not password:
                raise RuntimeError("Credenziali Garmin mancanti in config.json")

            tokenstore = garmin.get("tokenstore_path") or self._default_tokenstore(email)
            tokenstore = os.path.expanduser(tokenstore)

            client = Garmin(email, password)
            if os.path.exists(tokenstore):
                print(f"  ➜ Login tramite tokenstore ({tokenstore})...")
                try:
                    client.login(tokenstore)
                except Exception as err:
                    print(f"  ⚠️ Tokenstore non valido ({err}), login diretto...")
                    client.login()
                    client.garth.dump(tokenstore)
            else:
                print(f"  ℹ️ Login diretto (nessun tokenstore in {tokenstore})...")
                client.login()
                client.garth.dump(tokenstore)
            self._client = client
        return self._client

    @staticmethod
    def _default_tokenstore(email: str) -> str:
        clean = email.lower().replace("@", "_").replace(".", "_")
        return f"~/.garminconnect_session_{clean}"

    # ------------------------------------------------------------------ API
    def _api(self, method: str, path: str, **kwargs):
        """Chiamata API compatibile con garminconnect 0.2.x e 0.3.x.

        - 0.2.x: connectapi(path, method=...) accetta il metodo esplicito.
        - 0.3.x: connectapi è solo GET; usa client.request(method, 'garmin.com', path)
          (il wrapper Garmin espone l'inner GarminClient con request/post/delete).
        """
        client = self.client()
        inner = getattr(client, "client", None)  # 0.3.x: GarminClient; 0.2.x: assente
        if inner is not None and hasattr(inner, "request"):
            resp = inner.request(method, "garmin.com", path, **kwargs)
            # 0.3.x: request() restituisce la Response grezza -> parsala come connectapi
            if hasattr(resp, "json"):
                try:
                    return resp.json()
                except Exception:  # noqa: BLE001 (204 no-content / body vuoto)
                    return None
            return resp
        return client.connectapi(path, method=method, **kwargs)

    def upload(self, garmin_workout: dict) -> dict:
        """POST /workout-service/workout — crea il workout nella libreria Garmin."""
        print("⬆️  Upload workout su Garmin Connect...")
        resp = self._api("POST", "/workout-service/workout", json=garmin_workout)
        workout_id = resp.get("workoutId") if isinstance(resp, dict) else None
        print(f"  ✅ Workout creato (id={workout_id})")
        return resp

    def schedule(self, workout_id, date_str: str) -> dict:
        """POST /workout-service/schedule/{id} — agenda il workout sul calendario."""
        print(f"📅 Scheduling workout {workout_id} su {date_str}...")
        resp = self._api("POST", f"/workout-service/schedule/{workout_id}", json={"date": date_str})
        print("  ✅ Workout schedulato sul calendario Garmin")
        return resp

    def list_workouts(self, start: int = 0, limit: int = 30) -> list:
        """GET /workout-service/workouts — elenco workout nella libreria."""
        resp = self._api("GET", "/workout-service/workouts",
                         params={"start": start, "limit": limit})
        return resp if isinstance(resp, list) else resp.get("workouts", [])

    def delete_workout(self, workout_id) -> None:
        """DELETE /workout-service/workout/{id} — rimuove un workout."""
        print(f"🗑️  Eliminazione workout {workout_id}...")
        self._api("DELETE", f"/workout-service/workout/{workout_id}")
        print("  ✅ Workout eliminato")

    def scheduled_workouts(self, year: int = None, month: int = None) -> list:
        """Workout schedulati nel calendario Garmin per il mese indicato (default: corrente).
        Ritorna la lista di calendarItems (itemType 'workout' = allenamenti,
        'event' = gare/eventi). garminconnect >= 0.3 usa /calendar-service;
        versione storica /workout-service/schedule (fallback)."""
        today = date.today()
        year = year or today.year
        month = month or today.month
        print(f"📅 Workout schedulati: {year}-{month:02d}...")
        # Garmin usa i mesi 0-indexed nell'URL
        path = f"/calendar-service/year/{year}/month/{month - 1}"
        try:
            resp = self._api("GET", path)
        except Exception:  # noqa: BLE001 — fallback endpoint storico
            resp = self._api("GET", f"/workout-service/schedule/year/{year}/month/{month - 1}")
        if isinstance(resp, dict):
            return resp.get("calendarItems", [])
        return resp if isinstance(resp, list) else []

    # ------------------------------------------------------------------ pipeline
    def sync_dict(self, plan: dict, date_str: str, dry_run: bool = False) -> dict:
        """Converte un piano dict (non necessariamente da workout_plan.json,
        es. un giorno della tabella multisettimanale), carica su Garmin e
        agenda sul calendario."""
        gw = WorkoutConverter(self.config).convert(plan)

        print("=" * 70)
        print("FASE 4 - WORKOUT SYNC | piano -> Garmin Workout")
        print("=" * 70)
        print(WorkoutConverter.format_garmin(gw))
        print("=" * 70)

        if dry_run:
            print("\n⚠️  DRY RUN: nessuna chiamata API effettuata.\n")
            print(json.dumps(gw, indent=2, ensure_ascii=False))
            return {"dry_run": True, "garmin_workout": gw}

        uploaded = self.upload(gw)
        workout_id = (uploaded or {}).get("workoutId")
        if workout_id is None:
            raise RuntimeError(f"Risposta di upload senza workoutId: {uploaded}")

        scheduled = self.schedule(workout_id, date_str)
        return {
            "date": date_str,
            "workout_id": workout_id,
            "uploaded": uploaded,
            "scheduled": scheduled,
            "garmin_workout": gw,
        }

    def sync(self, date_str: str, dry_run: bool = False) -> dict:
        """Converte il piano corrente (workout_plan.json), carica su Garmin e
        agenda sul calendario. Delega a sync_dict."""
        return self.sync_dict(self.load_plan(), date_str, dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sincronizza l'allenamento AI sul calendario Garmin")
    parser.add_argument("--date", help="Data di scheduling (YYYY-MM-DD); default domani")
    parser.add_argument("--dry-run", action="store_true", help="Mostra il JSON Garmin senza chiamare l'API")
    parser.add_argument("--list", action="store_true", help="Elenca i workout nella libreria Garmin")
    parser.add_argument("--delete", metavar="WORKOUT_ID", help="Elimina un workout per ID")
    args = parser.parse_args()

    sync = WorkoutSync()

    if args.list:
        items = sync.list_workouts()
        if not items:
            print("📭 Nessun workout presente nella libreria Garmin.")
            return 0
        print(f"📚 {len(items)} workout in libreria:")
        for w in items:
            dur_min = (w.get("estimatedDurationInSecs") or 0) // 60
            print(f"  • [{w.get('workoutId')}] {w.get('workoutName')} ({dur_min} min)")
        return 0

    if args.delete:
        sync.delete_workout(args.delete)
        return 0

    date_str = args.date or (date.today() + timedelta(days=1)).isoformat()
    result = sync.sync(date_str, dry_run=args.dry_run)
    if not args.dry_run:
        print(f"\n🎉 Allenamento schedulato per il {result['date']} (workoutId={result['workout_id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
