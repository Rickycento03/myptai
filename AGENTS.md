# MyPTAI

Web app FastAPI e PWA con integrazione Garmin Connect, analisi biometriche e coaching.

- Usa esclusivamente i dati reali dell'account corrente e i suoi riferimenti personali. Dati mancanti non devono diventare dati di un altro atleta.
- Le soglie ufficiali provengono da Garmin o dalla configurazione privata; le stime locali sono confronti e non le sostituiscono.
- Conserva la mappatura prevista dal prodotto: corsa facile in Zona 3, soglia in Zona 4, massimale in Zona 5, recupero in Zona 1/2.
- Esegui i calcoli numerici in Python e invia al provider AI soltanto il contesto necessario alla richiesta.
- Mantieni sessioni, consenso, token, file e dati isolati per account. Una sessione Garmin salvata da sola non autentica un nuovo client.
- Non inserire nel repository credenziali, token, chiavi, dati biometrici reali, questionari personali o rapporti dettagliati di sicurezza. Configurazioni di esempio e test devono usare dati sintetici.
- Preserva i dati privati esistenti e prepara un recupero prima di modifiche in produzione.
- Valida i cambiamenti con test mirati e controlli appropriati. Le prove non devono inviare messaggi, generare piani a pagamento o scrivere su Garmin senza un incarico esplicito.
- Il modello personale dell'energia rimane sperimentale fino alla verifica concordata con l'utente.
