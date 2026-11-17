#!/usr/bin/env python3
"""
telegram_bot.py - FASE 5: Bot Telegram 24/7 per VPS Oracle
===========================================================
Interfaccia conversazionale di MyPT AI (Coach personale · dati reali Garmin):

  /start          -> presentazione + guida rapida
  /status         -> Readiness + biometria + sessioni recenti (analytics_engine)
  /allenamento    -> genera l'allenamento con DeepSeek e propone conferma/modifica
                     (es. "/allenamento 6x1000m in Z4 con recupero 3'")
  <testo libero>  -> stesso flusso di /allenamento (il bot è conversazionale)

Flusso con pulsanti inline:
  • dopo la generazione:  [✅ Carica su Garmin] [🔄 Rigenera] [❌ Annulla]
  • se Readiness RED e richiesta ad alta intensità (senza insistenza esplicita):
    il Coach AVVISIA del rischio e chiede conferma:
    [⚠️ Procedi comunque (override)] [❌ No, meglio recuperare]
  • Regola Override Atleta: se l'utente conferma, il Coach asseconda e genera
    comunque l'allenamento richiesto (ripetute Z4/Z5 incluse).

Deploy su VPS Oracle: vedi deploy/install_service.sh (servizio systemd).

CLI:
  python3 src/telegram_bot.py            # avvia il bot in polling (24/7)
  python3 src/telegram_bot.py --dry-run  # stampa la config e termina
"""

import argparse
import asyncio
import json
import logging
import sys
from html import escape
from datetime import date, timedelta
from pathlib import Path

from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("garmin_ai_coach")

# Domani come data di default per lo scheduling sul calendario Garmin.
DEFAULT_OFFSET_DAYS = 1

# Callback data dei pulsanti inline
CB_CONFIRM = "confirm_upload"
CB_REGENERATE = "regenerate"
CB_CANCEL = "cancel"
CB_OVERRIDE_YES = "override_yes"
CB_OVERRIDE_NO = "override_no"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def is_allowed(update: Update, config: dict) -> bool:
    """Accesso ristretto agli allowed_chat_ids; vuoto = accesso aperto (dev)."""
    ids = config.get("telegram", {}).get("allowed_chat_ids", [])
    if not ids:
        return True
    chat_id = update.effective_chat.id if update.effective_chat else None
    return chat_id in ids


async def _guard(update: Update) -> bool:
    """Gate di autorizzazione per tutti gli handler: blocca i chat fuori whitelist."""
    try:
        config = load_config()
    except Exception as err:  # noqa: BLE001
        logger.warning(f"_guard: config non leggibile ({err})")
        return False
    if is_allowed(update, config):
        return True
    query = update.callback_query
    if query is not None:
        await query.answer("🔒 Accesso non autorizzato", show_alert=True)
    elif update.effective_message is not None:
        await update.effective_message.reply_text("🔒 Accesso non autorizzato a questo bot.")
    return False


# =============================================================================
# Comandi
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    text = (
        "🏃 <b>MyPT AI — Coach personale · dati reali Garmin</b>\n\n"
        "Sono il tuo coach basato sui dati reali di Garmin Connect "
        "(sleep, HRV, Body Battery, Training Status, laps).\n\n"
        "Comandi:\n"
        "• <code>/status</code> — Readiness e biometria di oggi\n"
        "• <code>/allenamento &lt;richiesta&gt;</code> — genero un allenamento\n"
        "  es. <code>/allenamento 6x1000m in Z4 con recupero 3'</code>\n"
        "• Oppure scrivimi direttamente cosa vuoi fare oggi.\n\n"
        "🔄 Dopo la generazione potrai confermare e caricare l'allenamento "
        "direttamente sul calendario Garmin."
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await update.message.reply_text("📊 Sincronizzo i dati live da Garmin Connect...")
    if not await refresh_garmin_data():
        await update.message.reply_text(
            "⚠️ Sincronizzazione Garmin non riuscita: uso gli ultimi dati disponibili.")
    await update.message.reply_text("📊 Analizzo i dati Garmin...")
    try:
        from analytics_engine import AnalyticsEngine

        engine = AnalyticsEngine()
        summary = await asyncio.to_thread(engine.build_context)
        text = engine.build_context_text()
        r = summary["readiness"]
        emoji = {"GREEN": "🟢", "AMBER": "🟠", "RED": "🔴"}.get(r["readiness_level"], "⚪")
        score = r["readiness_score"]
        label = "Readiness non disponibile" if score is None else f"Readiness {score}/100 ({r['readiness_level']})"
        header = f"{emoji} <b>{label}</b>\n\n"
        await update.message.reply_text(header + "<pre>" + escape(text) + "</pre>",
                                        parse_mode="HTML")
    except Exception as err:  # noqa: BLE001
        logger.exception("Errore /status")
        await update.message.reply_text(f"❌ Errore nel recupero dello stato: {escape(str(err))}")


async def cmd_workout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    request = " ".join([a for a in (context.args or []) if not a.startswith("--")])
    # supporto opzionale --date YYYY-MM-DD
    date_str = _extract_date_arg(context.args)
    context.user_data["date_str"] = date_str
    await _workout_flow(update, context, request or None)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Testo libero = richiesta allenamento (bot conversazionale)."""
    if not await _guard(update):
        return
    request = update.message.text.strip()
    if not request or request.lower() in {"ciao", "hello", "hi", "buongiorno", "buonasera"}:
        await cmd_start(update, context)
        return
    await _workout_flow(update, context, request)


# =============================================================================
# Flusso di generazione + override + conferma
# =============================================================================
async def _workout_flow(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        request: str = None) -> None:
    """Genera l'allenamento (con eventuale warning RED -> override) e propone
    la conferma con pulsanti inline."""
    # Sincronizzazione live: dati freschi Garmin prima di calcolare Readiness
    # e generare l'allenamento (dump + summary rigenerati).
    await update.effective_chat.send_message("🔄 Sincronizzo i dati live da Garmin Connect...")
    if not await refresh_garmin_data():
        await update.effective_chat.send_message(
            "⚠️ Sincronizzazione Garmin non riuscita: uso gli ultimi dati disponibili.")

    from ai_coach import AICoach

    coach = AICoach()
    readiness = coach.summary.get("readiness", {})
    level = readiness.get("readiness_level")
    score = readiness.get("readiness_score")

    hard = AICoach.detect_hard_intent(request) if request else False
    override = AICoach.detect_override(request) if request else False

    # Readiness RED + lavoro duro + NESSUNA insistenza esplicita -> avviso e conferma
    if level == "RED" and hard and not override:
        # conserva la richiesta per i pulsanti override (sì/no)
        context.user_data["pending"] = {"request": request, "override": False}
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ Procedi comunque (override)", callback_data=CB_OVERRIDE_YES)],
            [InlineKeyboardButton("❌ No, meglio recuperare", callback_data=CB_OVERRIDE_NO)],
        ])
        await update.message.reply_text(
            f"🔴 <b>Readiness RED ({score}/100)</b>: recupero insufficiente.\n\n"
            f'Hai chiesto un lavoro ad alta intensità: <b>"{escape(request)}"</b>.\n'
            "⚠️ <b>Attenzione fisiologica</b>: con questa Readiness il rischio di "
            "sovrallenamento è aumentato (HRV basso, sonno scarso).\n\n"
            "Se confermi, procedo comunque con l'allenamento richiesto. "
            "Altrimenti ti propongo un recupero.",
            parse_mode="HTML", reply_markup=kb,
        )
        return

    await _generate_and_show(update, context, request, override)


async def _generate_and_show(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             request: str = None, override: bool = False) -> None:
    """Chiama DeepSeek, valida, mostra il piano e propone i pulsanti di conferma."""
    from ai_coach import AICoach

    coach = AICoach()
    await update.effective_chat.send_message(
        "🤖 Il coach sta elaborando il piano (DeepSeek)...")

    try:
        result = await asyncio.to_thread(coach.generate, request, override=override)
        workout = result["workout"]
        valid, issues = coach.validate_workout(workout, override=override)
    except Exception as err:  # noqa: BLE001
        logger.exception("Errore generazione allenamento")
        await update.effective_chat.send_message(f"❌ Generazione fallita: {escape(str(err))}")
        return

    plan_output = {
        "generated_at": date.today().isoformat(),
        "model": coach.model,
        "usage": result.get("usage", {}),
        "readiness_at_generation": coach.summary.get("readiness", {}),
        "override_richiesto": override,
        "validation_issues": issues,
        "workout": workout,
    }
    context.user_data["pending"] = {
        "request": request,
        "override": override,
        "plan_output": plan_output,
    }

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Carica su Garmin", callback_data=CB_CONFIRM)],
        [InlineKeyboardButton("🔄 Rigenera", callback_data=CB_REGENERATE),
         InlineKeyboardButton("❌ Annulla", callback_data=CB_CANCEL)],
    ])

    text = escape(AICoach.format_workout(workout))
    if override:
        text += "\n\n⚠️ <b>Override atleta attivo</b>: allenamento generato su tua richiesta."
    if issues:
        text += f"\n\n⚠️ Avvisi: {len(issues)}"
    await update.effective_chat.send_message(text, parse_mode="HTML",
                                             reply_markup=kb)


# =============================================================================
# Callback dei pulsanti inline
# =============================================================================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data
    pending = context.user_data.get("pending") or {}

    if data == CB_CANCEL:
        context.user_data.pop("pending", None)
        await query.edit_message_text("❌ Operazione annullata.")
        return

    if data == CB_OVERRIDE_NO:
        context.user_data.pop("pending", None)
        await query.edit_message_text(
            "🧘 Ok, niente lavori duri oggi. Genero un allenamento di recupero...")
        request = pending.get("request")
        await _generate_and_show(update, context, request, override=False)
        return

    if data == CB_OVERRIDE_YES:
        await query.edit_message_text(
            "⚡ Override confermato: il coach avvisa del rischio ma ti asseconda. "
            "Genero l'allenamento richiesto...")
        await _generate_and_show(update, context, pending.get("request"), override=True)
        return

    if data == CB_REGENERATE:
        await query.edit_message_text("🔄 Rigenero con la stessa richiesta...")
        await _generate_and_show(update, context,
                                 pending.get("request"), pending.get("override", False))
        return

    if data == CB_CONFIRM:
        await _confirm_upload(update, context, pending)
        return


async def _confirm_upload(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          pending: dict) -> None:
    """Salva il piano generato su workout_plan.json e lo carica sul calendario Garmin."""
    from ai_coach import AICoach
    from workout_sync import WorkoutSync

    query = update.callback_query
    if not pending:
        await query.edit_message_text("❌ Nessun allenamento in attesa di conferma. Usa /allenamento.")
        return

    plan_output = pending["plan_output"]
    await query.edit_message_text("⬆️ Salvo il piano e carico su Garmin Connect...")

    try:
        coach = AICoach()
        coach.save_output(plan_output)

        date_str = context.user_data.get("date_str") or \
            (date.today() + timedelta(days=DEFAULT_OFFSET_DAYS)).isoformat()

        sync = WorkoutSync()
        result = await asyncio.to_thread(sync.sync, date_str)
        wid = result.get("workout_id")
        await context.bot.send_message(
            update.effective_chat.id,
            f"🎉 <b>Allenamento caricato sul calendario Garmin!</b>\n"
            f"📅 Data: {result['date']}\n"
            f"🏷️ workoutId: <code>{escape(str(wid))}</code>\n\n"
            "Sincronizza l'orologio Garmin per vederlo sul device.",
            parse_mode="HTML")
    except Exception as err:  # noqa: BLE001
        logger.exception("Errore upload su Garmin")
        await query.edit_message_text(
            f"❌ Errore durante il caricamento su Garmin: {escape(str(err))}\n"
            "Il piano è comunque salvato in workout_plan.json.")


# =============================================================================
# Sincronizzazione live Garmin Connect
# =============================================================================
async def refresh_garmin_data() -> bool:
    """Scarica i dati freschi della giornata da Garmin Connect e rigenera il
    contesto analitico locale.

    1. garmin_probe.run_probe()  -> sovrascrive garmin_raw_dump.json
    2. AnalyticsEngine.run()      -> rigenera analytics_summary.json dal dump

    Esegue entrambe in thread separati (asyncio.to_thread) per non bloccare il
    loop asincrono del bot. In caso di errore (Garmin irraggiungibile, token
    scaduto, credenziali mancanti) ritorna False: il bot continua con gli
    ultimi dati locali disponibili invece di crashare.
    """
    try:
        from garmin_probe import run_probe
        from analytics_engine import AnalyticsEngine

        await asyncio.to_thread(run_probe)
        # Rigenera analytics_summary.json (letto da AICoach) dal dump fresco.
        await asyncio.to_thread(AnalyticsEngine().run)
        logger.info("🔄 Dati Garmin sincronizzati (dump + summary rigenerati)")
        return True
    except SystemExit:
        # run_probe chiama sys.exit(1) se credenziali mancanti o login fallito.
        logger.warning("⚠️ run_probe terminato con SystemExit: uso dati locali")
        return False
    except Exception as err:  # noqa: BLE001
        logger.warning(f"⚠️ Sincronizzazione Garmin fallita, uso dati locali: {err}")
        return False


# =============================================================================
# Utility
# =============================================================================
def _extract_date_arg(args) -> Optional[str]:
    """Estrae --date YYYY-MM-DD dagli argomenti del comando."""
    if not args:
        return None
    try:
        i = args.index("--date")
        if i + 1 < len(args):
            return args[i + 1]
    except ValueError:
        pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="MyPT AI - Coach personale · Bot Telegram")
    parser.add_argument("--dry-run", action="store_true",
                        help="Stampa la configurazione e termina senza avviare il bot")
    args = parser.parse_args()

    config = load_config()
    telegram = config.get("telegram", {})
    token = telegram.get("bot_token")
    if not token:
        print("❌ bot_token mancante in config.json")
        return 1

    if args.dry_run:
        print("=" * 60)
        print("CONFIG MyPT AI BOT (dry-run)")
        print("=" * 60)
        print(f"  bot_token: {token[:10]}...{token[-4:]}")
        print(f"  allowed_chat_ids: {telegram.get('allowed_chat_ids') or 'TUTTI (dev)'}")
        print(f"  modello AI: {config.get('deepseek', {}).get('model')}")
        print("  nessun avvio del polling.")
        return 0

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("allenamento", cmd_workout))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    logger.info("🤖 MyPT AI bot avviato in polling (24/7). Premere Ctrl+C per fermare.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
