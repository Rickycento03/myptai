#!/usr/bin/env bash
# =============================================================================
# install_service.sh - Deploy MyPT AI su VPS Oracle (Ubuntu 22.04/24.04)
# =============================================================================
# 1. Copia il progetto in /opt/garmin-ai-coach-bot
# 2. Crea utente dedicato non-root + venv + dipendenze
# 3. Installa e abilita il servizio systemd (24/7, riavvio automatico)
#
# Uso:  sudo bash deploy/install_service.sh
# =============================================================================
set -euo pipefail

APP_NAME="garmin-ai-coach-bot"
APP_DIR="/opt/${APP_NAME}"
# Nomi reali dei file .service (nel repo): garmin-ai-coach.service e garmin-ai-coach-web.service
SERVICE_NAME="garmin-ai-coach.service"
WEB_SERVICE_NAME="garmin-ai-coach-web.service"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Esclusioni condivise: dati runtime locali (data/) e artefatti di build
RSYNC_EXCLUDES=(--exclude 'venv' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '.git' --exclude '.DS_Store' --exclude '.venv' \
    --exclude 'data' --exclude 'garmin_raw_dump.json')

echo "🚀 Deploy ${APP_NAME} su $(hostname)"

# --- 1. Copia del progetto ---------------------------------------------------
if [[ -d "${APP_DIR}" ]]; then
    echo "  → Cartella esistente, aggiorno i sorgenti (escluso venv/data)..."
    rsync -a --delete "${RSYNC_EXCLUDES[@]}" "${SRC_DIR}/" "${APP_DIR}/"
else
    echo "  → Copio il progetto in ${APP_DIR}..."
    mkdir -p "${APP_DIR}"
    rsync -a --exclude '.git' --exclude '.DS_Store' "${SRC_DIR}/" "${APP_DIR}/"
fi
# I dati runtime (infortuni/obiettivi) vivono su data/ e NON vengono sovrascritti

# --- 2. Proprietà + venv + dipendenze ----------------------------------------
# Utente di esecuzione: ubuntu (stesso del bot) per condividere il tokenstore
# Garmin (/home/ubuntu/.garminconnect_session_*) e la scrittura dei file runtime.
RUN_USER="ubuntu"
chown -R ${RUN_USER}:${RUN_USER} "${APP_DIR}"

if [[ ! -d "${APP_DIR}/venv" ]]; then
    echo "  → Creo il venv..."
    python3 -m venv "${APP_DIR}/venv"
fi
echo "  → Installo le dipendenze (requirements.txt)..."
"${APP_DIR}/venv/bin/pip" install --upgrade pip >/dev/null
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

# --- 3. Servizi systemd -------------------------------------------------------
echo "  → Installo i servizi systemd (bot + web)..."
cp "${APP_DIR}/deploy/${SERVICE_NAME}" "/etc/systemd/system/${SERVICE_NAME}"
cp "${APP_DIR}/deploy/${WEB_SERVICE_NAME}" "/etc/systemd/system/${WEB_SERVICE_NAME}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" "${WEB_SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
systemctl restart "${WEB_SERVICE_NAME}"

echo ""
echo "✅ Servizi avviati!"
echo "   Bot:     systemctl status ${SERVICE_NAME}"
echo "   Web API: systemctl status ${WEB_SERVICE_NAME}  (http://127.0.0.1:8000)"
echo "   Log:     journalctl -u ${SERVICE_NAME} -f  |  journalctl -u ${WEB_SERVICE_NAME} -f"
echo "   Nginx:   sudo bash deploy/setup_nginx.sh <dominio>   (HTTPS per la PWA)"
