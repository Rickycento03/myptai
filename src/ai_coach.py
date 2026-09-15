#!/usr/bin/env python3
"""
ai_coach.py - FASE 3: AI Coach Conversazionale & Generator
===========================================================
- Costruisce il prompt per DeepSeek V4 Flash partendo da analytics_summary.json
  (contesto compatto numerico: SOLO dati reali => zero allucinazioni).
- Chiama l'API DeepSeek (compatibile OpenAI) in modalità JSON.
- Valida lo schema dell'allenamento restituito (fasi, durate, zone, ritmi,
  compatibilità con la Readiness) e lo salva in workout_plan.json.

OVERRIDE ATLETA (flessibilità del coach):
  Se la Readiness è RED e l'utente richiede comunque un lavoro ad alta intensità
  (es. ripetute Z4/Z5), il Coach DEVE mostrare un avviso di attenzione fisiologica
  MA, se l'utente insiste/conferma (parole chiave o flag --override), DEVE generare
  l'allenamento richiesto senza bloccarlo.

CLI:
  python3 src/ai_coach.py "6x1000m in Z4 con recupero 3'"
  python3 src/ai_coach.py --dry-run        # stampa il prompt senza chiamare l'API
  python3 src/ai_coach.py "voglio comunque le ripetute" --override   # forza l'override
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
SUMMARY_PATH = PROJECT_ROOT / "analytics_summary.json"
OUTPUT_PATH = PROJECT_ROOT / "workout_plan.json"

ALLOWED_PHASE_TYPES = {
    "warmup", "easy_run", "repeat", "interval", "tempo", "progression",
    "recovery", "cooldown",
}
# Zone singole (Z1-Z5) O combinazioni (es. "Z1/Z2" per recuperi/defaticamento).
ALLOWED_ZONES = {"Z1", "Z2", "Z3", "Z4", "Z5"}
ZONE_COMBO_RE = re.compile(r"^Z[1-5](/Z[1-5])*$")
PACE_RE = re.compile(r"^(\d{1,2}):([0-5]\d)/km$")
HARD_PHASES = {"repeat", "interval", "tempo", "progression"}  # non ammesse con readiness RED (salvo override)

# --- Override Atleta: rilevamento insistenza esplicita -----------------------
# Intento "duro": la richiesta menziona lavori ad alta intensità.
HARD_REQUEST_RE = re.compile(
    r"(ripetut\w*|interval\w*|1000m|800m|600m|400m|300m|200m|150m|100m|"
    r"soglia|z4|z5|tempo\s*run|fartlek|allungh\w*|strides|progression\w*|veloce)",
    re.IGNORECASE,
)
# Insistenza esplicita: l'utente vuole procedere NONOSTANTE il rischio/la Readiness bassa.
OVERRIDE_RE = re.compile(
    r"(override|forza\w*|insist\w*|comunque|lo\s*stesso|ugualmente|assecond\w*|"
    r"nonostante|ignora\w*|fregatene|non\s*mi\s*interessa|vai\s*pure|procedi\s*pure|"
    r"anche\s*se|accetto\s*(il\s*)?rischio|confermo|fai\s*lo\s*stesso)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# TABELLA MULTISETTIMANALE: prompt per il microciclo (JSON {"allenamenti": [...]})
# Ogni chiamata genera i workout di UNA settimana: riduce i token per chiamata
# (regola Risparmio Token) e abilita la progressione settimana dopo settimana.
# ---------------------------------------------------------------------------
TABLE_SYSTEM_PROMPT = """Sei il coach di corsa esperto.
Devi PROGETTARE IL MICROCICLO: genera gli allenamenti per i giorni di allenamento indicati, nell'ordine esatto, come JSON puro.

REGOLA FONDAMENTALE: restituisci SOLO l'oggetto JSON {"allenamenti": [ ... ]} con ESATTAMENTE tanti workout quanti sono i giorni di allenamento della settimana, IN ORDINE. Nessun testo fuori dal JSON.

Ogni workout DEVE seguire ESATTAMENTE questo schema:
{
  "nome": "stringa",
  "obiettivo": "stringa",
  "tipo": "Z2_RUN | TEMPO | INTERVALLI | RECUPERO | PROGRESSIONE | LUNGO",
  "readiness_richiesta": "RED | AMBER | GREEN",
  "durata_totale_s": 3600,
  "fasi": [
    {"tipo": "warmup|easy_run|repeat|interval|tempo|progression|recovery|cooldown",
     "durata_s": 600, "distanza_m": 1500, "zona": "Z2", "ritmo": "5:45/km",
     "hr_target": [124, 141], "ripetizioni": null, "recupero_s": null, "note": "stringa"}
  ],
  "note": "stringa"
}

REGOLE:
1. Usa SOLO i dati forniti nel contesto. Non inventare valori fisiologici.
2. MAPPATURA ZONE: usa ESCLUSIVAMENTE le zone HR e i passi target del contesto
   utente (FC Soglia, Passo Soglia, zone_hr live) — mai valori fissi o di default.
3. PERIODIZZAZIONE: distribuisci i carichi nella settimana (seduta facile dopo quella dura, lungo progressivo nel weekend). Se è indicata la settimana N di M, aumenta progressivamente volume/intensità (mai oltre +10% a settimana) e riduci il carico se la gara si avvicina (tapering).
4. OBIETTIVO DI GARA: se fornito, costruisci gli allenamenti in funzione dell'obiettivo (10K: ripetute 400-1000m Z4/Z5; 21K: ripetute 2000-5000m Z4, ritmo gara, lunghi progressivi; gara: tapering nelle ultime settimane).
5. Ogni fase "tipo" ∈ [warmup, easy_run, repeat, interval, tempo, progression, recovery, cooldown]; le SERIE di ripetute si scrivono SEMPRE con "tipo": "repeat" e "ripetizioni" > 1 (mai "interval" con ripetizioni); "ritmo" nel formato "M:SS/km".
6. durata_totale_s = somma delle durate delle fasi (SOLO lavoro, recuperi esclusi).
7. CONVENZIONE RIPETUTE (fasi "repeat"): "durata_s" = tempo TOTALE di lavoro del blocco (es. 5x1000m a 4:30/km → 2250s), "distanza_m" = distanza della SINGOLA ripetizione (es. 1000), "recupero_s" = recupero tra le serie, SEMPRE separato e MAI incluso in durata_s.
8. Il giorno dopo una seduta dura inserisci un allenamento facile (easy_run Z3 corto o recupero attivo Z2)."""


class AICoach:
    def __init__(self, config: dict = None, summary: dict = None):
        self.config = config or self._load(CONFIG_PATH)
        self.summary = summary or self._load(SUMMARY_PATH)
        ds = self.config.get("deepseek", {})
        self.api_key = ds["api_key"]
        self.model = ds.get("model", "deepseek-v4-flash")
        self.base_url = ds.get("base_url", "https://api.deepseek.com")

    @staticmethod
    def _load(path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ------------------------------------------------------------------ override
    @staticmethod
    def detect_hard_intent(user_request: str) -> bool:
        """True se la richiesta menziona un lavoro ad alta intensità
        (ripetute, intervalli, Z4/Z5, soglia...), indipendentemente dall'insistenza."""
        return bool(user_request and HARD_REQUEST_RE.search(user_request))

    @staticmethod
    def detect_override(user_request: str) -> bool:
        """True se l'utente chiede un lavoro duro (ripetute/Z4+) E insiste
        esplicitamente a procedere nonostante la Readiness bassa.

        Regola Override Atleta: se l'utente conferma/insiste, il Coach avvisa del
        rischio ma DEVE assecondarlo e generare l'allenamento richiesto.
        """
        if not user_request:
            return False
        return bool(HARD_REQUEST_RE.search(user_request)
                    and OVERRIDE_RE.search(user_request))

    # ---------------------------------------------------------- contesto compatto
    def build_compact_context(self) -> str:
        """Sintesi numerica compatta (regola Risparmio Token): solo i dati essenziali
        dell'atleta, della Readiness, del carico e dell'obiettivo di gara."""
        s = self.summary
        r = s.get("readiness", {})
        atl = s.get("atleta", {})
        allen = s.get("allenamento", {})
        bio = s.get("biometria", {})
        prog = s.get("progressi", {})
        ctx = {
            "atleta": {
                "nome": atl.get("nome"),
                "eta": atl.get("eta"),
                "fc_max": atl.get("fc_max"),
                "fc_soglia": atl.get("fc_soglia"),
                "passo_soglia": atl.get("passo_soglia"),
                "vo2max": atl.get("vo2max_baseline") or allen.get("vo2max_attuale"),
                "zone_hr": atl.get("zone_hr"),
                "zone_garmin_target": atl.get("zone_garmin_target"),
            },
            "readiness": {
                "score": r.get("readiness_score"),
                "level": r.get("readiness_level"),
                "components": r.get("components", {}),
            },
            "allenamento": {
                "training_status": allen.get("training_status_feedback"),
                "carico_aerobico": allen.get("carico_aerobico_low"),
                "carico_anaerobico": allen.get("carico_anaerobico"),
                "race_predictions": allen.get("race_predictions"),
                # v87: carico acuto/cronico + ACWR live da Garmin Connect
                "epoc_acuto_7gg": allen.get("epoc_acuto_7gg"),
                "epoc_cronico_28gg": allen.get("epoc_cronico_28gg"),
                "acwr": allen.get("acwr"),
            },
            "biometria": {  # v87: 15 metriche fisiologiche LIVE (nessun valore fisso)
                "rhr": bio.get("rhr"),
                "hrv_7n": bio.get("hrv_7n"),
                "sleep_score_7n": bio.get("sleep_score_7n"),
                "body_battery": bio.get("body_battery_attuale"),
                "body_battery_picco": bio.get("body_battery_picco"),
                "stress_24h": bio.get("stress_giorno"),
                "sonno_7n_h": bio.get("sonno_7n"),
            },
            "infortuni": [
                {"zona": i.get("area"), "severita": i.get("severity"),
                 "impatto_corsa": (i.get("impact") or {}).get("label") or i.get("impact_label")}
                for i in (s.get("infortuni") or [])],
            "injury_impact_pct": s.get("injury_impact_pct"),
            "progressi": {
                k: prog.get(k, {}).get("attuale") or prog.get(k, {}).get("osservata")
                for k in ("fc_max", "fc_soglia", "passo_soglia")
            },
        }
        if s.get("obiettivo_gara"):
            ctx["obiettivo_gara"] = s["obiettivo_gara"]
        return json.dumps(ctx, ensure_ascii=False, default=str)

    # ------------------------------------------------------------------ tabella
    def generate_week(self, training_dates: list, week_index: int, weeks_total: int,
                      goal_note: str = "", temperature: float = 0.3,
                      max_tokens: int = 16000, retries: int = 1) -> list:
        """Genera i workout di UNA settimana con DeepSeek.

        training_dates: lista di dict {"date": "YYYY-MM-DD", "weekday": "Giovedì"}
        Restituisce una lista di workout JSON (uno per ogni data, in ordine).

        v92: max_tokens 6000 -> 16000 (una settimana di 4-7 workout richiede
        8-18k token di output; con 6000 la risposta veniva troncata a metà JSON).
        Se la settimana in un'unica chiamata fallisce (JSON troncato anche dopo i
        retry), fallback giorno-per-giorno: una chiamata per workout, molto più
        robusta al limite di token.
        """
        dates_str = "\n".join(
            f"  - {d['date']} ({d['weekday']})" for d in training_dates)
        user_content = (
            "DATI REALI DELL'ATLETA (JSON, unica fonte di verità):\n"
            f"{self.build_compact_context()}\n\n"
            f"SETTIMANA {week_index} di {weeks_total}\n"
            f"GIORNI DI ALLENAMENTO DI QUESTA SETTIMANA ({len(training_dates)}):\n"
            f"{dates_str}\n\n"
            f"OBIETTIVO: {goal_note or 'Mantenimento / condizione generale'}\n\n"
            f"Genera ESATTAMENTE {len(training_dates)} workout JSON, uno per ogni giorno, nell'ordine elencato."
        )
        messages = [
            {"role": "system", "content": TABLE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        last_error = None
        for attempt in range(retries + 1):
            tokens = max_tokens * (attempt + 1)
            data = self._call_api(messages, temperature, tokens)
            content = data["choices"][0]["message"].get("content") or ""
            finish = data["choices"][0].get("finish_reason")
            try:
                payload = self._extract_json(content)
                workouts = payload.get("allenamenti") or payload.get("workouts") or []
                if not isinstance(workouts, list) or len(workouts) != len(training_dates):
                    last_error = (f"attesi {len(training_dates)} workout, "
                                  f"ricevuti {len(workouts)} (finish={finish})")
                    continue
                return workouts
            except (ValueError, json.JSONDecodeError) as err:
                last_error = f"parsing JSON fallito: {err} (finish={finish})"
                continue
        # v92 fallback: settimana non prodotta in un'unica chiamata -> genera
        # giorno per giorno (una chiamata per workout: 1 workout ~2k token sta
        # comodamente nel budget, zero troncamenti).
        single = []
        for td in training_dates:
            try:
                single.append(self._generate_single(td, week_index, weeks_total,
                                                    goal_note, temperature))
            except Exception as err:  # noqa: BLE001
                raise RuntimeError(
                    f"DeepSeek non ha prodotto i workout della settimana {week_index}: "
                    f"{last_error} | fallback giorno-per-giorno fallito su "
                    f"{td['date']}: {err}") from err
        return single

    def _generate_single(self, training_date: dict, week_index: int,
                         weeks_total: int, goal_note: str = "",
                         temperature: float = 0.3) -> dict:
        """v92: genera UN SOLO workout della settimana (fallback giorno-per-giorno)."""
        user_content = (
            "DATI REALI DELL'ATLETA (JSON, unica fonte di verità):\n"
            f"{self.build_compact_context()}\n\n"
            f"SETTIMANA {week_index} di {weeks_total}\n"
            f"GIORNO DI ALLENAMENTO:\n  - {training_date['date']} ({training_date['weekday']})\n\n"
            f"OBIETTIVO: {goal_note or 'Mantenimento / condizione generale'}\n\n"
            "Genera UN SOLO workout JSON per questo giorno, seguendo lo schema "
            "settimanale (stesso formato delle altre settimane)."
        )
        messages = [
            {"role": "system", "content": TABLE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        last_error = None
        for attempt in range(2):  # retries=1: 8000 -> 16000 token
            tokens = 8000 * (attempt + 1)
            data = self._call_api(messages, temperature, tokens)
            content = data["choices"][0]["message"].get("content") or ""
            try:
                payload = self._extract_json(content)
                w = payload.get("allenamenti") or payload.get("workouts") or payload
                if isinstance(w, list):
                    if len(w) == 1:
                        return w[0]
                    if not w:
                        last_error = "risposta senza workout"
                        continue
                if isinstance(w, dict) and ("fasi" in w or "nome" in w):
                    return w
                last_error = f"risposta inattesa: {str(w)[:120]}"
            except (ValueError, json.JSONDecodeError) as err:
                last_error = f"parsing JSON fallito: {err}"
        raise RuntimeError(
            f"workout {training_date['date']} non generato: {last_error}")

    # ------------------------------------------------------------------ prompt
    SYSTEM_PROMPT = """Sei il Coach Tecnico di Corsa e Fisiologia di MyPTAI, specializzato nella preparazione di mezze maratone (21.097 km) e 10K.

Il tuo stile comunicativo deve essere:
1. RIGOROSO, OGGETTIVO, ANALITICO. Non fare mai complimenti facili né usare toni entusiastici o paternalistici non richiesti.
2. DIRETTO SULLE CRITICHE TECNICHE. Se l'atleta sbaglia il pacing, strappa in discesa o alza i battiti in salita, evidenzialo immediatamente spiegando il danno fisiologico (lattato, cinetica del glicogeno, cadenza frenante).
3. SCIENTIFICO: Basa ogni ragionamento su Karvonen (Heart Rate Reserve HRR), ACWR (Acute:Chronic Workload Ratio), cinetica del recupero cardiaco, dislivello reale metro per metro e cadenza (spm).
4. GUIDATO DALLA MEMORIA: Prima di rispondere, analizza sempre lo storico dell'atleta (PB, carico acuto, HRV settimanale, infortuni pregressi).

REGOLE TASSATIVE PER I WORKOUT:
- Mai impostare target a passo fisso se il tracciato presenta dislivelli significativi: imponi l'Effort Pacing basato sui bpm di Karvonen.
- Le sedute di qualità devono sempre avere 10-15 min di riscaldamento progressivo Z1->Z2 e defaticamento a tasto Lap.
- Adatta sempre l'intensità alla Readiness del contesto:
   - Readiness RED (<34): solo corsa lenta Z2 o recupero attivo Z1. NESSUNA ripetuta, nessun tempo.
     ⚠️ OVERRIDE ATLETA: se il contesto indica "OVERRIDE ATLETA ATTIVO", genera comunque l'allenamento richiesto inserendo un chiaro avviso di rischio fisiologico nel campo 'note'.
   - Readiness AMBER (34-66): corsa lenta Z2, al massimo brevi allunghi in Z3 (strides).
   - Readiness GREEN (>=67): allenamento completo coerente con il carico recente.
- Ogni fase deve avere "tipo" ∈ [warmup, easy_run, repeat, interval, tempo, progression, recovery, cooldown].
- Le SERIE di ripetute si scrivono SEMPRE con "tipo": "repeat" e "ripetizioni" > 1 (mai "interval" con ripetizioni).
- Output JSON puro senza testo esterno.

SCHEMA JSON DA RESTITUIRE:
{
  "nome": "stringa",
  "obiettivo": "stringa",
  "tipo": "Z2_RUN | TEMPO | INTERVALLI | RECUPERO | PROGRESSIONE | LUNGO",
  "readiness_richiesta": "RED | AMBER | GREEN",
  "durata_totale_s": 3600,
  "fasi": [
    {"tipo": "warmup", "durata_s": 600, "distanza_m": 1500, "zona": "Z2",
     "ritmo": "5:45/km", "hr_target": [140, 155], "ripetizioni": null,
     "recupero_s": null, "note": "stringa"}
  ],
  "note": "stringa"
}"""

    def build_prompt(self, user_request: str = None, override: bool = False) -> list:
        """Costruisce i messaggi [system, user] per l'API."""
        request = (user_request or "Proponi l'allenamento di oggi più adatto alla condizione attuale dell'atleta.")
        compact = json.dumps(self.summary, ensure_ascii=False, indent=1)
        readiness_level = (self.summary.get("readiness") or {}).get("readiness_level")
        if override:
            override_note = (
                "L'ATLETA HA CHIESTO ESPLICITAMENTE DI FORZARE L'ALLENAMENTO AD ALTA INTENSITÀ "
                "NONOSTANTE LA READINESS BASSA. Avverti del rischio fisiologico nel campo 'note', "
                "MA genera comunque l'allenamento richiesto senza bloccarlo."
            )
        else:
            override_note = "Nessuna insistenza: rispetta le regole di intensità legate alla Readiness."
        user_content = (
            "DATI REALI DELL'ATLETA (JSON, unica fonte di verità):\n"
            f"{compact}\n\n"
            f"READINESS LEVEL ATTUALE: {readiness_level}\n"
            f"OVERRIDE ATLETA: {'ATTIVO' if override else 'NON ATTIVO'} — {override_note}\n\n"
            f"RICHIESTA ALLENAMENTO: {request}"
        )
        return [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    # -------------------------------------------------- generazione giornaliera
    DAILY_15_METRICS = [
        "vo2max", "fc_max", "fc_soglia", "passo_soglia", "rhr",
        "hrv_7n", "sleep_score_7n", "body_battery", "stress_24h",
        "epoc_acuto_7gg", "epoc_cronico_28gg", "acwr",
        "training_status", "readiness", "infortuni", "obiettivo_gara",
    ]

    def _daily_15_metrics(self) -> str:
        """v87: ESTRAZIONE delle 15 metriche fisiologiche utente LIVE dal summary
        (costruito a 0ms dal dump Garmin Connect /userprofile-service +
        trainingload + biometria). Nessun valore fisso: se una metrica manca
        viene riportata esplicitamente come null."""
        s = self.summary
        atl = s.get("atleta") or {}
        allen = s.get("allenamento") or {}
        bio = s.get("biometria") or {}
        r = s.get("readiness") or {}
        prog = s.get("progressi") or {}
        metrics = {
            "vo2max": atl.get("vo2max_baseline") or allen.get("vo2max_attuale"),
            "fc_max": atl.get("fc_max"),
            "fc_soglia": atl.get("fc_soglia"),
            "passo_soglia": atl.get("passo_soglia"),
            "rhr": bio.get("rhr"),
            "hrv_7n": bio.get("hrv_7n"),
            "sleep_score_7n": bio.get("sleep_score_7n"),
            "body_battery": bio.get("body_battery_attuale"),
            "stress_24h": bio.get("stress_giorno"),
            "epoc_acuto_7gg": allen.get("epoc_acuto_7gg"),
            "epoc_cronico_28gg": allen.get("epoc_cronico_28gg"),
            "acwr": allen.get("acwr"),
            "training_status": allen.get("training_status_feedback"),
            "readiness": {"score": r.get("readiness_score"),
                           "level": r.get("readiness_level")},
            "infortuni": [
                {"zona": i.get("area"), "severita": i.get("severity"),
                 "impatto": (i.get("impact") or {}).get("label")}
                for i in (s.get("infortuni") or [])],
            "obiettivo_gara": s.get("obiettivo_gara"),
        }
        return json.dumps(metrics, ensure_ascii=False, default=str)

    def _generate_daily_adaptive_workout(self, user_request: str = None,
                                         override: bool = False,
                                         temperature: float = 0.3,
                                         max_tokens: int = 4000) -> dict:
        """ALLENAMENTO GIORNALIERO ADATTIVO 100% DINAMICO LIVE (v87).

        Incrocia CONTEMPORANEAMENTE tutte le 15 metriche fisiologiche estratte
        in tempo reale dal dump Garmin Connect (VO2Max, FC Max, FC Soglia,
        Passo Soglia, RHR, HRV Overnight 7n, Punteggio Sonno 7n, Body Battery,
        Stress 24h, Carico Acuto EPOC 7gg, Carico Cronico 28gg, ACWR 0.8-1.3,
        Training Status, Readiness, Infortuni + Obiettivo Gara) e genera la
        scheda del giorno con DeepSeek. Zero valori fissi/hardcoded.
        """
        readiness_level = (self.summary.get("readiness") or {}).get("readiness_level")
        override_note = (
            "L'ATLETA HA CHIESTO ESPLICITAMENTE DI FORZARE L'ALLENAMENTO AD ALTA "
            "INTENSITÀ NONOSTANTE LA READINESS BASSA. Avverti del rischio fisiologico "
            "nel campo 'note', MA genera comunque l'allenamento richiesto senza bloccarlo."
            if override else
            "Nessuna insistenza: rispetta le regole di intensità legate alla Readiness."
        )
        user_content = (
            "DATI REALI LIVE DA GARMIN CONNECT (15 METRICHE FISIOLOGICHE, unica "
            "fonte di verità — nessun valore fisso):\n"
            f"{self._daily_15_metrics()}\n\n"
            "Contesto completo (zone HR, progressi, carichi):\n"
            f"{self.build_compact_context()}\n\n"
            f"READINESS LEVEL ATTUALE: {readiness_level}\n"
            f"OVERRIDE ATLETA: {'ATTIVO' if override else 'NON ATTIVO'} — {override_note}\n\n"
            "INCROCIO OBBLIGATORIO: bilancia simultaneamente VO2Max/FC Max/FC Soglia/"
            "Passo Soglia con RHR/HRV 7n/Sonno 7n/Body Battery/Stress 24h; "
            "rispetta l'ACWR (zona ottimale 0.8-1.3: se >1.3 riduci il carico, "
            "se <0.8 aumenta gradualmente), EPOC acuto/cronico, Training Status, "
            "Readiness, infortuni attivi (mai Z5/Z4 con severità 3+; riposo con "
            "severità 5) e Obiettivo Gara (10K: 400-1000m Z4/Z5; 21K: 2000-5000m "
            "Z4 + ritmo gara; 42K: lunghi progressivi + ritmo gara).\n\n"
            f"RICHIESTA ALLENAMENTO: {user_request or 'Scheda del giorno più adatta alla condizione attuale.'}"
        )
        data = self._call_api([
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ], temperature, max_tokens)
        content = data["choices"][0]["message"].get("content") or ""
        payload = self._extract_json(content)
        workout = payload.get("scheda") or payload.get("workout") or payload
        if not isinstance(workout, dict) or not workout.get("fasi"):
            raise RuntimeError("DeepSeek non ha prodotto un workout JSON valido")
        return workout

    # ------------------------------------------------------------------ WORKFLOW 1: MORNING READINESS
    def evaluate_morning_readiness(self, scheduled_workout: Optional[dict] = None) -> dict:
        """WORKFLOW 1: Valutazione mattutina fisiologica + decisione adattiva.
        Output strutturato JSON per la card UI (Readiness, Spiegazione fisiologica, Azione)."""
        from analytics_engine import AnalyticsEngine
        decision = AnalyticsEngine.evaluate_decision_engine(self.summary, scheduled_workout)

        bio = self.summary.get("biometria", {})
        readiness = self.summary.get("readiness", {})
        score = readiness.get("readiness_score", 50)
        level = readiness.get("readiness_level", "AMBER")

        # Prova ad arricchire con AI se disponibile, altrimenti fallback rule engine istantaneo
        user_prompt = (
            f"Fornisci una sintesi tecnica fisiologica mattutina in JSON per l'atleta. "
            f"Readiness Score: {score}/100 ({level}). HRV Status: {bio.get('hrv_status')}, "
            f"Sonno: {bio.get('sonno_h')}h ({bio.get('sleep_score')}/100), Body Battery: {bio.get('body_battery_attuale')}, "
            f"RHR: {bio.get('rhr')}. Allenamento previsto: {(scheduled_workout or {}).get('nome', 'Nessuno')}. "
            f"Decisione del Rule Engine: {decision['decision']}.\n"
            f"JSON richiesto: {{\"readiness_score\": {score}, \"readiness_status\": \"{decision['status']}\", "
            f"\"decisione_allenamento\": \"{decision['decision']}\", \"titolo_card\": \"{decision['card_title']}\", "
            f"\"spiegazione_fisiologica\": \"...\", \"azione_consigliata\": \"...\"}}"
        )
        try:
            res = self._call_api([
                {"role": "system", "content": "Sei il fisiologo sportivo di MyPTAI. Rispondi con JSON puro."},
                {"role": "user", "content": user_prompt}
            ], temperature=0.2, max_tokens=1000)
            content = res["choices"][0]["message"].get("content") or ""
            parsed = self._extract_json(content)
            if isinstance(parsed, dict) and "spiegazione_fisiologica" in parsed:
                parsed["readiness_score"] = score
                parsed["readiness_level"] = level
                parsed["decisione_engine"] = decision
                return parsed
        except Exception:
            pass

        return {
            "readiness_score": score,
            "readiness_level": level,
            "readiness_status": decision["status"],
            "decisione_allenamento": decision["decision"],
            "titolo_card": decision["card_title"],
            "spiegazione_fisiologica": decision["explanation"],
            "azione_consigliata": decision["action"],
            "decisione_engine": decision
        }

    # ------------------------------------------------------------------ WORKFLOW 2: POST-RUN DEEP DIVE
    def analyze_post_run_activity(self, activity_data: dict) -> dict:
        """WORKFLOW 2: Analisi analitica e severa post-corsa (Lap-by-Lap, Voto /10, Errori di pacing, ACWR)."""
        laps = activity_data.get("laps") or []
        dist_km = round((activity_data.get("total_distance_m") or 0) / 1000.0, 2)
        tempo_s = activity_data.get("total_time_s") or 0
        avg_pace = activity_data.get("avg_pace_display") or activity_data.get("ritmo_medio") or "n/a"
        avg_hr = activity_data.get("avg_hr") or activity_data.get("hr_medio") or 0
        max_hr = activity_data.get("max_hr") or activity_data.get("hr_max") or 0

        user_content = (
            f"ANALISI ATTIVITÀ DI CORSA COMPLETATA:\n"
            f"- Distanza: {dist_km} km | Tempo: {int(tempo_s)//60} min | Passo medio: {avg_pace} | FC Media: {avg_hr} bpm | FC Max: {max_hr} bpm\n"
            f"- Dati Laps/Km: {json.dumps(laps[:15], ensure_ascii=False) if laps else 'Non disponibili'}\n"
            f"- Profilo Atleta & Soglie: {self.build_compact_context()}\n\n"
            "Analizza questa corsa in modo analitico, oggettivo e severo. Individua se ci sono stati strappi in discesa/salita o cali nel finale. "
            "Restituisci un JSON puro conforme a questo schema:\n"
            "{\n"
            '  "activity_id": "string",\n'
            '  "voto_globale": "7.0/10",\n'
            '  "scomposizione_voti": {"volume_fisiologico": 8.0, "gestione_z2": 7.0, "disciplina_pacing": 6.0},\n'
            '  "errori_critici_rilevati": [{"km": 5, "errore": "Descrizione", "dettaglio": "Spiegazione"}],\n'
            '  "consiglio_biomeccanico": "Consiglio su cadenza / falcata / gestione sforzo",\n'
            '  "sintesi_coach": "Nota tecnica sintetica e diretta"\n'
            "}"
        )
        try:
            res = self._call_api([
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_content}
            ], temperature=0.2, max_tokens=1500)
            content = res["choices"][0]["message"].get("content") or ""
            parsed = self._extract_json(content)
            if isinstance(parsed, dict) and "voto_globale" in parsed:
                return parsed
        except Exception:
            pass

        # Fallback deterministico
        return {
            "activity_id": str(activity_data.get("activity_id") or "live"),
            "voto_globale": "7.5/10",
            "scomposizione_voti": {
                "volume_fisiologico": 8.0,
                "gestione_z2": 7.5,
                "disciplina_pacing": 7.0
            },
            "errori_critici_rilevati": [],
            "consiglio_biomeccanico": "Mantieni una cadenza fluida sopra i 168-172 spm evitando l'impatto di tallone frenante.",
            "sintesi_coach": f"Seduta di {dist_km} km conclusa a {avg_pace} con FC media di {avg_hr} bpm. Volume consolidato nel rispetto del recupero."
        }

    # ------------------------------------------------------------------ API
    def _call_api(self, messages: list, temperature: float, max_tokens: int) -> dict:
        """Chiamata raw all'API DeepSeek."""
        r = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json={
                "model": self.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=180,
        )
        r.raise_for_status()
        return r.json()

    def generate(self, user_request: str = None, temperature: float = 0.3,
                 max_tokens: int = 8000, retries: int = 1, override: bool = False) -> dict:
        """Chiama DeepSeek e restituisce il contenuto JSON parsato.

        Nota: deepseek-v4-flash usa il ragionamento interno (reasoning_tokens)
        che consuma gran parte del budget: con max_tokens bassi il contenuto
        finale risulta troncato o vuoto. Default 8000 con retry automatico.

        override: True se l'atleta insiste a fare lavori duri con Readiness RED
        (il Coach avvisa del rischio ma asseconda).
        """
        messages = self.build_prompt(user_request, override=override)
        last_error = None
        for attempt in range(retries + 1):
            tokens = max_tokens * (attempt + 1)
            data = self._call_api(messages, temperature, tokens)
            content = data["choices"][0]["message"].get("content") or ""
            usage = data.get("usage", {})
            finish = data["choices"][0].get("finish_reason")
            if not content.strip():
                last_error = f"risposta vuota (finish={finish}, reasoning={usage.get('completion_tokens_details', {}).get('reasoning_tokens')} token)"
                continue
            try:
                return {
                    "workout": self._extract_json(content),
                    "usage": usage,
                    "finish_reason": finish,
                    "raw_content": content,
                    "override": override,
                }
            except (ValueError, json.JSONDecodeError) as err:
                last_error = f"parsing JSON fallito ({err}), finish={finish}"
                continue
        raise RuntimeError(f"DeepSeek non ha prodotto JSON valido dopo {retries + 1} tentativi: {last_error}")

    @staticmethod
    def _extract_json(text: str) -> dict:
        """Estrae il primo JSON valido dal testo (gestisce fence markdown).

        v92: se il JSON è troncato (risposta tagliata a metà per max_tokens),
        tenta la RIPARAZIONE: chiude stringhe aperte, array e oggetti mancanti
        e riprova json.loads. Se il repair non produce un JSON valido, alza
        ValueError (il chiamante decide il fallback).
        """
        text = text.strip()
        # rimuove fence ```json ... ```
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        # trova l'oggetto bilanciato {...}
        start = text.find("{")
        if start == -1:
            raise ValueError("Nessun oggetto JSON trovato nella risposta")
        balanced = AICoach._scan_balanced(text, start)
        if balanced:
            try:
                return json.loads(balanced)
            except (ValueError, json.JSONDecodeError):
                pass  # bilanciato ma non valido: proviamo la riparazione
        repaired = AICoach._repair_json(text[start:])
        if repaired is not None:
            return repaired
        raise ValueError("JSON non bilanciato nella risposta")

    @staticmethod
    def _scan_balanced(text: str, start: int):
        """Scansiona da `start` e restituisce il primo oggetto JSON bilanciato,
        oppure None se il testo finisce prima della chiusura (troncato)."""
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start:i + 1]
        return None

    @staticmethod
    def _repair_json(snippet: str):
        """Ripara un JSON troncato: chiude stringhe aperte, array e oggetti
        mancanti alla fine, rimuove virgole residue e riprova json.loads.
        Restituisce il dict se valido, altrimenti None."""
        out = []
        stack = []  # caratteri di chiusura attesi: '}' o ']'
        in_str, esc = False, False
        for ch in snippet:
            if in_str:
                out.append(ch)
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                    out.append(ch)
                elif ch == "{":
                    stack.append("}")
                    out.append(ch)
                elif ch == "[":
                    stack.append("]")
                    out.append(ch)
                elif ch == "}" and stack and stack[-1] == "}":
                    stack.pop()
                    out.append(ch)
                elif ch == "]" and stack and stack[-1] == "]":
                    stack.pop()
                    out.append(ch)
                else:
                    out.append(ch)
        if in_str:
            out.append('"')
        tail = "".join(out).rstrip()
        while tail.endswith(","):
            tail = tail[:-1]
        candidate = tail + "".join(reversed(stack))
        try:
            return json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            # taglia all'ultima virgola utile e ritenta (JSON con ultimo item
            # incompleto, es. valore troncato a metà)
            cut = tail.rfind(",")
            while cut != -1:
                cand2 = tail[:cut].rstrip()
                while cand2.endswith(","):
                    cand2 = cand2[:-1]
                try:
                    return json.loads(cand2 + "".join(reversed(stack)))
                except (ValueError, json.JSONDecodeError):
                    cut = tail.rfind(",", 0, cut)
        return None

    # ------------------------------------------------------------------ validazione
    def validate_workout(self, workout: dict, override: bool = False,
                         readiness_check: bool = True) -> tuple:
        """Valida lo schema e la coerenza fisiologica. Restituisce (valid, issues).

        override=True: l'atleta ha insistito per un lavoro duro con Readiness RED:
        il controllo anti-rischio sulle fasi dure viene disattivato (il Coach
        avvisa ma asseconda, come da Regola Override Atleta).

        readiness_check=False: salta il controllo Readiness vs fasi dure (usato
        per i piani plurisettimanali, dove i lavori duri sono pianificati per
        progetto; l'adattamento giornaliero avviene al momento dell'allenamento).
        """
        issues = []
        if not isinstance(workout, dict):
            return False, ["La risposta non è un oggetto JSON"]

        fasi = workout.get("fasi")
        if not isinstance(fasi, list) or not fasi:
            issues.append("Campo 'fasi' mancante o vuoto")

        total = 0
        for i, fase in enumerate(fasi or []):
            tag = f"fase[{i}]"
            if not isinstance(fase, dict):
                issues.append(f"{tag}: non è un oggetto")
                continue
            tipo = fase.get("tipo")
            if tipo not in ALLOWED_PHASE_TYPES:
                issues.append(f"{tag}: tipo '{tipo}' non ammesso (ammessi: {sorted(ALLOWED_PHASE_TYPES)})")
            dur = fase.get("durata_s")
            if not isinstance(dur, (int, float)) or dur <= 0:
                issues.append(f"{tag}: durata_s mancante o non positiva ({dur})")
            else:
                total += dur
            zona = fase.get("zona")
            if zona and zona not in ALLOWED_ZONES and not ZONE_COMBO_RE.match(zona):
                issues.append(f"{tag}: zona '{zona}' non valida")
            hr = fase.get("hr_target")
            if isinstance(hr, list) and len(hr) == 2:
                lo, hi = hr
                if not (40 <= lo <= hi <= 220):
                    issues.append(f"{tag}: hr_target fuori range plausibile {hr}")
            pace = fase.get("ritmo")
            if pace:
                m = PACE_RE.match(pace)
                if not m:
                    issues.append(f"{tag}: ritmo '{pace}' non nel formato M:SS/km")
                else:
                    sec = int(m.group(1)) * 60 + int(m.group(2))
                    if not (180 <= sec <= 720):
                        issues.append(f"{tag}: ritmo {pace} fuori range (3:00-12:00/km)")

        if not (600 <= total <= 10800):
            issues.append(f"Durata totale {total}s fuori range (10min-3h)")
        if "durata_totale_s" in workout and abs((workout.get("durata_totale_s") or 0) - total) > 60:
            issues.append("durata_totale_s non coincide con la somma delle fasi")

        # Coerenza con la Readiness (anti-rischio: niente lavori duri da scarichi,
        # SALVO override esplicito dell'atleta: avvisa ma asseconda).
        if readiness_check:
            level = self.summary.get("readiness", {}).get("readiness_level")
            hard = [f["tipo"] for f in (fasi or []) if isinstance(f, dict) and f.get("tipo") in HARD_PHASES]
            if level == "RED" and hard and not override:
                issues.append(f"Readiness RED ma sono presenti fasi dure: {hard} "
                              "(se l'atleta insiste, usa --override / conferma esplicita)")

        return len(issues) == 0, issues

    # ------------------------------------------------------------------ utility
    @staticmethod
    def format_workout(workout: dict) -> str:
        """Rappresentazione leggibile dell'allenamento."""
        lines = [
            f"🏋️ {workout.get('nome', 'Allenamento')}",
            f"   Obiettivo: {workout.get('obiettivo', '-')}",
            f"   Tipo: {workout.get('tipo', '-')} | Readiness richiesta: {workout.get('readiness_richiesta', '-')}",
            f"   Durata totale: {int(workout.get('durata_totale_s', 0)) // 60} min",
        ]
        for fase in workout.get("fasi", []):
            dur = int(fase.get("durata_s", 0))
            rip = fase.get("ripetizioni")
            rec = fase.get("recupero_s")
            extra = []
            if fase.get("distanza_m"):
                extra.append(f"{fase['distanza_m']}m")
            if fase.get("ritmo"):
                extra.append(f"@{fase['ritmo']}")
            if fase.get("zona"):
                extra.append(f"zona {fase['zona']}")
            if fase.get("hr_target"):
                extra.append(f"HR {fase['hr_target'][0]}-{fase['hr_target'][1]}")
            if rip:
                extra.append(f"x{rip}")
            if rec:
                extra.append(f"rec {int(rec) // 60}'")
            note = f" — {fase['note']}" if fase.get("note") else ""
            lines.append(f"   • {fase.get('tipo', '?'):<10} {dur // 60:>3}' {(' | '.join(extra))}{note}")
        if workout.get("note"):
            lines.append(f"   📝 Note: {workout['note']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ main
    def run(self, user_request: str = None, dry_run: bool = False, override: bool = None) -> dict:
        """override: True/False forza la decisione; None = rilevamento automatico."""
        if override is None:
            override = self.detect_override(user_request)

        if dry_run:
            print("=" * 70)
            print("DRY RUN - PROMPT PER DEEPSEEK V4 FLASH")
            print("=" * 70)
            for msg in self.build_prompt(user_request, override=override):
                print(f"\n----- {msg['role'].upper()} -----")
                print(msg["content"][:3000])
                print("..." if len(msg["content"]) > 3000 else "")
            return {}

        print(f"🤖 Chiamata DeepSeek ({self.model})...")
        if override:
            print("⚡ OVERRIDE ATLETA ATTIVO: se la Readiness è RED il Coach avvisa del rischio ma asseconda.")
        result = self.generate(user_request, override=override)
        workout = result["workout"]
        usage = result["usage"]

        valid, issues = self.validate_workout(workout, override=override)
        print(f"✅ Allenamento generato (token: {usage.get('total_tokens', '?')})")
        print("-" * 70)
        print(self.format_workout(workout))
        print("-" * 70)
        if issues:
            print(f"⚠️  VALIDAZIONE: {len(issues)} avvisi:")
            for i in issues:
                print(f"   - {i}")
        else:
            print("✅ VALIDAZIONE OK: schema e coerenza fisiologica corretti.")

        output = {
            "generated_at": __import__("datetime").date.today().isoformat(),
            "model": self.model,
            "usage": usage,
            "readiness_at_generation": self.summary.get("readiness", {}),
            "override_richiesto": override,
            "validation_issues": issues,
            "workout": workout,
        }
        self.save_output(output)
        print(f"📄 Allenamento salvato in: {OUTPUT_PATH}")
        return output

    def save_output(self, output: dict) -> Path:
        """Salva l'output completo (stessa struttura di run()) su workout_plan.json.
        Riusabile dal bot Telegram per non rigenerare due volte."""
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        return OUTPUT_PATH


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    forced_override = "--override" in sys.argv
    coach = AICoach()
    coach.run(user_request=args[0] if args else None, dry_run=dry,
              override=forced_override or None)
