# Architettura MyPTAI

1. Garmin Connect autentica e fornisce le misure del singolo account.
2. Il backend Python normalizza le misure, calcola gli indicatori e conserva i dati per utente.
3. Il coach riceve il contesto necessario e produce allenamenti secondo lo schema del prodotto.
4. La PWA presenta dati, obiettivi, piani e stato della sincronizzazione.
5. Le scritture nel calendario Garmin avvengono su richiesta dell'utente.

Credenziali, dati reali, riferimenti fisiologici, cronologia degli allenamenti e dettagli operativi della VPS rimangono nella configurazione e nella memoria private. Questo documento non contiene risultati personali o identificativi di allenamenti reali.

Le soglie ufficiali dell'atleta sono la fonte di verità. Il coaching adattivo considera recupero, carico, obiettivi e fastidi presenti nel contesto corrente. Una scelta esplicita dell'atleta può confermare un allenamento dopo l'avviso previsto dal prodotto.

Il modello personale dell'energia viene calibrato separatamente con questionari e dati privati prima dell'integrazione.
