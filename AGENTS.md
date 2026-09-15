# Progetto: Garmin AI Performance Coach (Clean Build)

## Obiettivo del Sistema
Un Bot Telegram attivo 24/7 su VPS Oracle. Il bot si connette a Garmin Connect (account di Riccardo), estrae i dati biometrici completi (Sleep Score, HRV notturno/settimanale, Body Battery, Training Status, Lap di corsa per le ripetute), calcola in Python l'indice di Readiness e la scheda utente dinamica (zone cardio, passo Z2), genera con DeepSeek V4 Flash l'allenamento strutturato in formato JSON e lo carica direttamente sul calendario Garmin Connect.

## Dati Fisici e Parametri Fisiologici
- **Atleta**: Riccardo (23 anni, 79kg, 186cm, FC Max 200, FC Soglia Lattato 176 bpm, Passo Soglia ~4:44 min/km)
- **Garmin Email**: `athlete@example.com`
- **Garmin Password**: `[PROTECTED_IN_CONFIG]`
- **Telegram Bot Token**: `[CONFIGURED_IN_ENV]`
- **DeepSeek API Key**: `[CONFIGURED_IN_ENV]`
- **VPS Oracle Target**: Configurazione per deploy 24/7 tramite daemon `systemd`.

## Regole di Sviluppo da Zero
1. **Nessun codice vecchio**: Il progetto parte da zero nella cartella `src/`.
2. **File di Configurazione `config.json`**: Deve contenere in modo pulito ed isolato le credenziali di Garmin, Telegram, DeepSeek e i dati di profilazione di Riccardo.
3. **Risparmio Token**: L'elaborazione algebrica (Readiness Score 0-100, Z2 pace, Laps parsing) avviene in Python. A DeepSeek viene passata solo una sintesi numerica compatta.
4. **Zero Allucinazioni**: L'AI risponde solo ed esclusivamente sui dati Garmin reali ricevuti.

## Convenzioni Zone Cardiache e Ritmi (Personalizzazione Riccardo)
- **Corse Lente / Aerobiche (Easy Runs)**: L'atleta esegue le corse lente abitualmente in **Zona 3** (o Z2/Z3 aerobia).
- **Recupero Attivo / Defaticamento (Recovery)**: Eseguito in **Zona 1 / Zona 2**.
- **Ripetute / Soglia (Qualità)**: Zona 4 / Zona 5 (ritmo attorno ai 3:55 - 4:10 min/km).

## Monitoraggio Progressi (Soglie e FC Max)
- **Pilastri di Sviluppo**: FC Max (200 bpm), Frequenza Soglia Lattato / Anaerobica (176 bpm) e Passo Soglia (~4:44 min/km) sono le metriche fondamentali usate da `analytics_engine.py` per calcolare i passi target e valutare l'aumento della forma fisica dell'atleta.
- **Riconoscimento Dinamico dei Miglioramenti**: Se le metriche recenti mostrano un passo più veloce a parità di FC (o un miglioramento di VO2Max/LTHR da Garmin), il sistema aggiorna automaticamente i valori di soglia.

- **VO2Max**: Attualmente 54.5 (rilevato da Garmin). Utilizzato come indicatore di potenza aerobica e capacità cardiovascolare per calcolare i miglioramenti nel tempo.

## Regola Override Atleta (Flessibilità del Coach)
- **Avviso con Override Consentito**: Se la Readiness è ROSSA (recupero necessario) e l'utente richiede un allenamento ad alta intensità (es. ripetute Z4/Z5), il Coach AI deve mostrare un **avviso di attenzione fisiologica**, MA **se l'utente insiste o conferma di voler fare comunque l'allenamento, il Coach DEVE assecondare l'utente e generare l'allenamento richiesto senza bloccarlo**.

## Mappatura Esatta Zone Garmin per Riccardo (MAPPING REALE)
Nel payload del workout Garmin (`src/workout_sync.py`), le zone numeriche Garmin (zoneNumber / targetValueOne) devono seguire questa mappatura rigorosa per rispecchiare le preferenze di Riccardo:
- **Corse Lente / Aerobiche / Main Easy Run** ➔ **Garmin Zona 3** (`zoneNumber: 3` / target HR Zona 3).
- **Soglia Anaerobica / Ripetute di Soglia** ➔ **Garmin Zona 4** (`zoneNumber: 4` / FC Soglia ~176 bpm, ~4:44 min/km).
- **Massimale / Vo2Max / Sprint** ➔ **Garmin Zona 5** (`zoneNumber: 5` / FC Max ~200 bpm).
- **Recuperi / Defaticamento / Riscaldamento leggero** ➔ **Garmin Zona 1 o Zona 2** (`zoneNumber: 1` o `2`).

## Algoritmo di Progressione Dinamica e Overperformance
- **Riconoscimento Prestazioni Superiori (Overperformance)**: Se in una sessione recente l'atleta corre a ritmi più veloci dei target impostati a parità di Frequenza Cardiaca (o registra nuovi PB nei lap), l'engine `analytics_engine.py` deve ricalibrare automaticamente il Passo Soglia verso l'alto (es. da 4:44 a 4:35/km).
- **Adattamento dei Nuovi Allenamenti**: Nei giorni con Readiness VERDE/AMBER, il Coach AI DeepSeek utilizzerà i nuovi ritmi velocizzati per proporre allenamenti più sfidanti, garantendo una crescita atletica continua ed evitando lo stallo dei carichi.

## Gestione Obiettivo di Gara e Periodizzazione (Goal Engine)
- **Definizione Obiettivo Utente**: L'atleta puo impostare il proprio obiettivo target (es. 10K, Mezza Maratona 21K, Maratona 42K), il Tempo/Passo Target desiderato e la Data della Gara.
- **Periodizzazione Differenziata**:
  - *Obiettivo 10K*: Prescrizione di ripetute veloci e brevi (400m, 800m, 1000m) ad alta intensità Z4/Z5 con focus su VO2Max e tolleranza al lattato.
  - *Obiettivo Mezza Maratona (21K)*: Prescrizione di ripetute medie/lunghe (2000m, 3000m, 5000m), corse a ritmo gara (RGM), medio in Zona 3 e lunghi progressivi.
- **Integrazione PWA**: L'obiettivo attivo viene mostrato in testata nella Home dell'App e salvato in `config.json` e `analytics_summary.json` per istruire DeepSeek.

## Regola Ferrea Soglia Ufficiale Garmin
- **Fonte di Verità Unica**: La Frequenza Cardiaca di Soglia (176 bpm) ed il Passo Soglia (~4:44 min/km) usati dal sistema devono rimanere **SEMPRE quelli ufficiali calcolati da Garmin Connect** (o impostati in `config.json`).
- **Nessuna alterazione locale della Soglia**: L'engine locale ed il Coach AI NON devono modificare arbitrariamente la soglia dell'atleta. La soglia ufficiale Garmin è considerata la fonte di verità assoluta e fidata per tutti i calcoli delle zone e delle ripetute.

## Sincronizzazione Dinamica della Soglia Garmin
- **Sincronizzazione Automatica da Garmin**: Se l'orologio Garmin rileva un nuovo valore di Soglia Lattato (FC o Passo Soglia) e lo aggiorna su Garmin Connect, l'engine `analytics_engine.py` recepisce automaticamente il nuovo valore ufficiale Garmin e lo applica a tutti i calcoli successivi del sistema.

## Regole per Tabelle Multisettimanali Adattive (Adaptive Multi-Week Plan)
- **Selezione Giorni Utente**: L'atleta seleziona i giorni della settimana in cui e disponibile a correre (es. Martedi, Giovedi, Sabato, Domenica). Il generatore crea la tabella rispettando rigorosamente i giorni di riposo scelti.
- **Rilevamento Discrepanza & Adattamento Singolo Giorno**:
  - Quando l'atleta apre la sezione Allenamento o preme "Aggiorna Dati", il sistema confronta la Readiness live di oggi con l'allenamento che era schedulato in tabella per oggi.
  - Se la Readiness e ROSSA/AMBER ed il piano prevedeva un allenamento duro (es. Lungo o Ripetute Z4), l'App avvisa l'utente della discrepanza e propone un adattamento specifico del singolo allenamento (es. trasformare il Lungo in Corsa Rigenerante Z1 o Riposo).
  - L'atleta puo scegliere: `[ 🔄 Adatta Allenamento di Oggi ]` oppure `[ ⚡️ Mantieni Originale (Override) ]`.

## Struttura della Sezione Allenamento (Layout Dall'Alto in Basso)
1. **CARD TOP — Stato Garmin Calendar**: In cima alla sezione appare l'allenamento programmato su Garmin per la giornata di oggi (se caricato via Sync) oppure la notifica "Nessun allenamento programmato su Garmin".
2. **CARD MIDDLE — Consiglio del Coach Adattivo**: Confronta l'allenamento del giorno con la Readiness di OGGI. Indica se confermarlo, alleggerirlo o potenziarlo in base ai parametri live, con i pulsanti `[ 🔄 Adatta Seduta ]` o `[ 🚀 Conferma/Sync ]`.
3. **CARD BOTTOM — Generatore su Richiesta & Chat**: Casella di testo libera per richiedere qualsiasi allenamento custom. L'AI genera la scheda a blocchi colorati ed il pulsante `[ 🚀 CARICA SU GARMIN ]`.

## Regola per il Pulsante "Adatta Seduta"
- Quando l'utente clicca su `[ 🔄 Adatta Seduta ]`:
  1. **Se non c'e nulla da adattare** (es. Readiness GREEN e seduta gia ottimale): Mostra un messaggio di notifica chiaro *"✅ L'allenamento in programma e gia perfettamente allineato alla tua Readiness di oggi (86.3 GREEN). Nessun adattamento necessario."*
  2. **Se ci sono modifiche consigliate (non obbligatorie)**: Mostra il popup dei cambiamenti suggeriti (es. potenziare volume/ritmi se GREEN, oppure alleggerire ripetute se RED/AMBER) con due opzioni:
     - `[ 🔄 Applica Modifica Consigliata ]`
     - `[ ❌ Mantieni Originale ]`

## Sistema Avanzato Infortuni & Impatto Biomeccanico sulla Corsa (Injury Lab 2.0)
- **Classificazione dell'Impatto sulla Corsa per Distretto Anatomico**:
  - *Distretti Ad Alto Impatto Biomeccanico (Arti Inferiori)*: Piede, Dita del Piede, Caviglia, Tendine d'Achille, Polpaccio, Tibia, Ginocchio, Quadricipite, Ischiocrurali, Anca, Zona Lombare.
    - Severita 1-2: Riduzione del volume del 10-20% e divieto di Zona 5.
    - Severita 3-4: Blocco delle ripetute veloci in Z4/Z5, trasformazione in Corsa Lenta Z2/Z3 o Medio.
    - Severita 5: Blocco della corsa (Impatto 100%), prescrizione di riposo o cross-training (nuoto/bici).
  - *Distretti A Basso/Nullo Impatto (Arti Superiori / Mani)*: Dita della Mano, Polso, Gomito, Spalla, Collo.
    - Severita 1-5: Nessuna o minima alterazione dei ritmi di corsa (es. dita della mano a severita 5 non modificano l'allenamento di corsa).
- **Referto Anatomico AI**: Quando viene segnalato un fastidio, DeepSeek o l'Engine generano il referto anatomico con le strutture interessate, le probabili cause e le azioni di recupero.
- **Integrazione con "Adatta Seduta"**: L'endpoint `/api/workouts/adapt-check` ed il pulsante `[ 🔄 Adatta Seduta ]` leggono gli infortuni attivi ed il loro peso biomeccanico per suggerire l'adattamento specifico del giorno.
