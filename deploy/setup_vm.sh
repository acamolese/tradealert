#!/usr/bin/env bash
# Setup TradeAlert su VM Oracle Free Tier (Ubuntu 22.04 ARM o x86).
#
# Uso (dopo aver fatto SSH alla VM):
#   curl -fsSL https://raw.githubusercontent.com/acamolese/tradealert/main/deploy/setup_vm.sh | bash
# oppure clone manuale del repo e bash deploy/setup_vm.sh
#
# Cosa fa:
# - Installa Python 3.13, git, system deps
# - Clona o aggiorna il repo in ~/tradealert
# - Crea venv + pip install
# - Crea cartella logs/
# - Imposta timezone Europe/Rome
# - Crea il file .env (template che dovrai compilare)
# - Installa crontab e systemd unit per il listener
#
# Idempotente: si puo' rilanciare in sicurezza per aggiornare.

set -euo pipefail

REPO_URL="https://github.com/acamolese/tradealert.git"
HOME_DIR="${HOME}"
TARGET_DIR="${HOME_DIR}/tradealert"

echo "==> System update e dipendenze base"
sudo apt-get update -y
sudo apt-get install -y software-properties-common ca-certificates curl gnupg \
    git build-essential libssl-dev libffi-dev tzdata

# Python 3.13 via deadsnakes (Ubuntu 22.04 default e' 3.10)
if ! command -v python3.13 >/dev/null 2>&1; then
    echo "==> Installo Python 3.13 via deadsnakes PPA"
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -y
    sudo apt-get install -y python3.13 python3.13-venv python3.13-dev
fi

echo "==> Timezone Europe/Rome"
sudo timedatectl set-timezone Europe/Rome

echo "==> Clone o pull repo"
if [ ! -d "$TARGET_DIR/.git" ]; then
    git clone "$REPO_URL" "$TARGET_DIR"
else
    git -C "$TARGET_DIR" pull --ff-only
fi

cd "$TARGET_DIR"
mkdir -p logs

echo "==> Crea/aggiorna venv"
if [ ! -d ".venv" ]; then
    python3.13 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
    echo "==> Creo .env da template (.env.example). DOVRAI COMPILARLO."
    cp .env.example .env
    echo
    echo "!!! IMPORTANTE !!!"
    echo "Apri ${TARGET_DIR}/.env e inserisci tutti i valori (CAPITAL_*, TELEGRAM_*,"
    echo "SUPABASE_*, ANTHROPIC_API_KEY, FINNHUB_API_KEY)."
    echo "Poi rilancia: bash deploy/setup_vm.sh"
    echo
    exit 0
fi

echo "==> Installo crontab"
crontab deploy/crontab.txt

echo "==> Installo systemd unit per il listener"
sudo cp deploy/tradealert-listener.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tradealert-listener.service
sudo systemctl restart tradealert-listener.service

echo
echo "==> Done."
echo "Crontab installato:"
crontab -l | grep -E "^[^#]" | head -10 || true
echo
echo "Status listener daemon:"
sudo systemctl status tradealert-listener.service --no-pager | head -10
echo
echo "Tail log listener (Ctrl+C per uscire):"
echo "  tail -f ${TARGET_DIR}/logs/listener.log"
