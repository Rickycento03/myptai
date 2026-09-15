#!/usr/bin/env bash
# =============================================================================
# setup_nginx.sh - Reverse proxy Nginx + HTTPS (Let's Encrypt) per la PWA
# =============================================================================
# Requisiti:
#   - Il DNS del dominio DEVE puntare già all'IP della VPS (record A)
#   - Porte 80/443 aperte nel Security List / firewall Oracle
#
# Uso:
#   sudo bash deploy/setup_nginx.sh coach.tuodominio.it
# =============================================================================
set -euo pipefail

DOMAIN="${1:-}"
if [[ -z "${DOMAIN}" ]]; then
    echo "❌ Specifica il dominio:  sudo bash deploy/setup_nginx.sh coach.tuodominio.it"
    exit 1
fi
if [[ "${DOMAIN}" == "SERVER_NAME" || "${DOMAIN}" == "coach.rickycento03.it" ]]; then
    echo "⚠️  Il dominio sembra un segnaposto. Verifica il record DNS A prima di continuare."
    read -rp "Procedi comunque? [y/N] " ans
    [[ "${ans}" =~ ^[Yy]$ ]] || exit 1
fi

echo "🌐 Configurazione Nginx + HTTPS per ${DOMAIN}"

# --- 0. Apertura porte 80/443 in iptables (Oracle usa regole locali) ---------
echo "  → Apro le porte 80/443 in iptables..."
sudo iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 5 -p tcp --dport 80 -j ACCEPT
sudo iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT
if command -v netfilter-persistent >/dev/null 2>&1; then
    sudo netfilter-persistent save >/dev/null 2>&1 && echo "  → Regole iptables persistite"
elif [[ -d /etc/iptables ]]; then
    sudo bash -c 'iptables-save > /etc/iptables/rules.v4' && echo "  → Regole iptables salvate in /etc/iptables/rules.v4"
else
    echo "  ⚠️  Impossibile persistere le regole iptables: riapplicale al riavvio o installa iptables-persistent"
fi

# --- 1. Installazione nginx (se assente) ------------------------------------
if ! command -v nginx >/dev/null 2>&1; then
    echo "  → Installo nginx..."
    apt-get update -qq
    apt-get install -y -qq nginx certbot python3-certbot-nginx
fi

# --- 2. Configurazione reverse proxy ------------------------------------------
CONF="/etc/nginx/sites-available/garmin-ai-coach"
sed "s/SERVER_NAME/${DOMAIN}/g" \
    /opt/garmin-ai-coach-bot/deploy/nginx-garmin-ai-coach.conf > "${CONF}"

ln -sf "${CONF}" /etc/nginx/sites-enabled/garmin-ai-coach
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl reload nginx
echo "  ✅ Nginx configurato su http://${DOMAIN}"

# --- 3. Certificato HTTPS (Let's Encrypt) ------------------------------------
echo "  → Richiedo il certificato HTTPS con certbot..."
certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos \
    --register-unsafely-without-email --redirect || {
        echo "⚠️  certbot non è riuscito (DNS non ancora propagato?)."
        echo "   Riprova più tardi con:  sudo certbot --nginx -d ${DOMAIN}"
        exit 0
    }

echo ""
echo "✅ PWA disponibile su https://${DOMAIN}"
echo "   Verifica:  curl -s https://${DOMAIN}/api/health"
