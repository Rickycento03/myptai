# PLAN: Sviluppo da Zero Garmin AI Coach

## FASE 1: Configurazione & Discovery Dati Garmin ✅ FATTO
- **Step 1.1**: Creare il file `config.json` con le credenziali ufficiali di Riccardo (Garmin, Telegram Bot, DeepSeek API Key, dati atletici) e il file `requirements.txt`.
- **Step 1.2**: Creare lo script `src/garmin_probe.py` per testare l'autenticazione con Garmin Connect ed estrarre i dati grezzi di tutti gli endpoint (Sleep, HRV, Body Battery, Training Status, Laps).
- **Step 1.3**: Eseguire `garmin_probe.py`, salvando l'output in `garmin_raw_dump.json` e verificare quali campi tornano correttamente.

## FASE 2: Engine Fisiologico Locale (`src/analytics_engine.py`) ✅ FATTO
- Calcolo Readiness Score (0-100) basato sui dati reali estratti nella Fase 1.
- Parser dei lap di corsa per separare il ritmo Z2 dalle ripetute/intervalli.
- Costruzione del prompt sintetico per l'AI.

## FASE 3: AI Coach Conversazionale & Generator (`src/ai_coach.py`) ✅ FATTO
- Chiamata a DeepSeek V4 Flash.
- Output JSON dell'allenamento con fasi strutturate (Warmup, Repeat, Interval, Recovery, Cooldown).
- **Override Atleta**: rilevamento automatico insistenza (regex) + flag `--override`; con Readiness RED e override attivo il Coach avvisa del rischio ma asseconda e genera comunque il lavoro richiesto (validazione anti-rischio disattivata solo in override).

## FASE 4: Sincronizzazione Calendario Garmin (`src/workout_sync.py`) ✅ FATTO
- Conversione JSON -> Workout Garmin (`ExecutableStepDTO`/`RepeatGroupDTO`, target `heart.rate.zone`, endCondition TIME/DISTANCE/ITERATIONS).
- Upload automatico sul calendario Garmin dell'atleta (`POST /workout-service/workout` + `POST /workout-service/schedule/{id}`).
- CLI: `--dry-run`, `--date YYYY-MM-DD`, `--list`, `--delete <id>`.
- **Testato live**: workout reali caricati e schedulati su account Riccardo (es. workoutId 1661786275, zona HR correttamente salvata dopo fix `targetValueOne` a livello step).

## FASE 5: Bot Telegram & Deploy VPS Oracle (`src/telegram_bot.py`) ✅ FATTO
- Bot Telegram asincrono (python-telegram-bot v20): `/start`, `/status`, `/allenamento <richiesta>`, testo libero conversazionale.
- Pulsanti inline: [✅ Carica su Garmin] [🔄 Rigenera] [❌ Annulla]; data di scheduling default = domani (o `--date YYYY-MM-DD`).
- **Flusso Override Atleta nel bot**: con Readiness RED e richiesta ad alta intensità senza insistenza -> avviso fisiologico con pulsanti [⚠️ Procedi comunque (override)] / [❌ No, meglio recuperare]. Se l'utente conferma, il Coach asseconda e genera comunque l'allenamento richiesto.
- Deploy VPS Oracle: `deploy/garmin-ai-coach.service` (systemd, utente non-root, restart automatico) + `deploy/install_service.sh` (copia in /opt, venv, installazione servizio).

## FASE 6: Web API FastAPI + PWA Mobile ✅ FATTO
- **Server API** `src/web_server.py` (FastAPI, porta 8000, bind 127.0.0.1 dietro nginx):
  - `GET /api/health`, `GET /api/readiness`, `POST /api/refresh` (sync live Garmin)
  - `GET /api/workouts/latest`, `POST /api/workouts/generate` (DeepSeek), `POST /api/workouts/sync` (One-Tap calendario Garmin)
  - `GET/POST/PATCH/DELETE /api/injuries` (fastidi muscolari, persistenza `data/injuries.json`)
  - `GET/POST /api/goals` (obiettivo gara 10K/21K + tempo target, iniettato nel contesto AI)
  - `POST /api/chat` (conversazionale + workout + override_required)
  - **Sicurezza**: API Key obbligatoria per `/api/*` (header `X-API-Key`, config in `config.json -> web.api_key`); statico PWA pubblico.
- **PWA mobile-first** `web/` (tema scuro): cerchio Readiness SVG, allenamento con Sync One-Tap, sagoma corpo umano SVG interattiva (fronte/retro, 43 zone) per infortuni, sezione Obiettivo gara, chat AI Coach, schermata di sblocco API Key.
- `web/manifest.json` + `web/sw.js` + icone 192/512/180 (`web/icons/generate_icons.py`) -> installabile su iPhone/Android (richiede HTTPS).
- **Deploy**: `deploy/garmin-ai-coach-web.service` (systemd, utente `ubuntu` come il bot per condividere il tokenstore Garmin), `deploy/setup_nginx.sh <dominio>` (nginx + iptables 80/443 + certbot HTTPS), install_service.sh aggiornato per installare bot + web.
- **REGOLA SOGLIA**: FC soglia (176 bpm) e passo soglia (4:44/km) sono valori UFFICIALI Garmin SEMPRE fissi; stime locali solo informative, prompt AI li impone come riferimento.
- Dominio: `riccardo-coach.duckdns.org` -> 204.216.214.31 (HTTPS via Let's Encrypt dopo apertura Security List OCI 80/443).

## Nota Mappatura Zone Garmin (correzione da screenshot)
- **easy_run / Main Easy Run -> Garmin ZONA 3** (zoneNumber 3, HR 142-158) — non Zona 2!
- **soglia/ripetute -> Garmin ZONA 4** (zoneNumber 4, FC 176, ~4:44/km)
- **massimale/sprint -> Garmin ZONA 5** (zoneNumber 5, FC Max 200)
- **recupero/defaticamento -> Garmin ZONA 1 o 2** (HR 114-141)
- Implementata in `src/workout_sync.py` (PHASE_TO_GARMIN_ZONE per tipo di fase, non etichetta AI) e in `src/analytics_engine.py` (classificazione lap EASY_Z3, istruzioni AI, ATHLETE_ZONE_TARGETS).
- Workout caricati e verificati live su account Garmin (es. id 1661793530: warmup=2, main=3, cooldown=1).

## Modifica Mappatura Zone in `src/workout_sync.py` e `src/analytics_engine.py`
- Nel converter del workout Garmin (`WorkoutConverter`):
  - I tratti di `easy_run` / Corsa Lenta devono essere convertiti in **Garmin Zone 3** (`zoneNumber: 3`).
  - I tratti di `interval` / Soglia devono puntare a **Garmin Zone 4** (`zoneNumber: 4`).
  - I tratti di `sprint` / Massimale devono puntare a **Garmin Zone 5** (`zoneNumber: 5`).
  - I tratti di `warmup` / `recovery` / `cooldown` devono puntare a **Garmin Zone 1 o Zone 2**.

## Nota Progressione Adattiva in `analytics_engine.py` e `ai_coach.py`
- L'engine rileva il superamento delle aspettative dai lap delle ultime attività Garmin e segnala l'overperformance nel file `analytics_summary.json` per alzare i target di passo e volume nei successivi allenamenti.

## Nota Obiettivo di Gara e Periodizzazione in `web_server.py` e `ai_coach.py`
- Aggiunti endpoint `/api/goal` (GET/POST) per salvare l'obiettivo (distanza, tempo target, data gara) e passarlo a `AnalyticsEngine` e `AICoach` per la periodizzazione specifica delle ripetute (10K vs 21K).

## Nota Soglia Ufficiale Garmin
- `analytics_engine.py` e `ai_coach.py` devono utilizzare rigorosamente la Frequenza ed il Passo di Soglia ufficiali restituiti da Garmin senza modificarli localmente.

## Nota Sincronizzazione Dinamica della Soglia
- Quando Garmin Connect aggiorna la Frequenza o il Passo di Soglia dell'atleta, il sistema aggiorna automaticamente i valori di riferimento mantenendo sempre il dato ufficiale Garmin sincronizzato.

## Nota Tabelle Adattive in `web_server.py`, `ai_coach.py` e PWA
- Supporto per la selezione dei giorni disponibili della settimana (es. 3 o 4 giorni/settimana).
- Endpoint `/api/workouts/plan/adaptive-check` che confronta la Readiness di oggi con il piano schedulato e propone l'adattamento intelligente del singolo giorno con opzione di override.

## Layout Sezione Allenamento in `web/index.html` e `web/js/app.js`
- 1. Card Garmin Calendar (Stato del giorno).
- 2. Card Consiglio Adattivo (Confronto Readiness live vs Piano con suggerimento di upgrade/downgrade).
- 3. Card Generatore Custom & Chat AI con pulsante di Caricamento Garmin.

## UX "Adatta Seduta" in `web/js/app.js` e `src/web_server.py`
- Gestione risposte `POST /api/workouts/adapt-today`: restituisce `needed: false` con messaggio se gia ottimale, oppure `needed: true` con le modifiche suggerite ed opzioni di applicazione o mantenimento del piano originale.

## Nota Injury Lab 2.0 in `web/js/app.js`, `src/web_server.py` e `src/ai_coach.py`
- Mappatura della severita e del peso biomeccanico degli infortuni sulla corsa (distinzione piedi/gambe vs mani/braccia).
- Generazione referto anatomico AI con cause e raccomandazioni di recupero.
- Integrazione automatica con `/api/workouts/adapt-check` per il pulsante `[ 🔄 Adatta Seduta ]`.
