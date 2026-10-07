#!/usr/bin/env bash
# Instala el dashboard como servicio systemd en Ubuntu.
# Uso:  sudo bash instalar.sh
set -euo pipefail

DESTINO=/opt/dashboard-ia
ORIGEN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then echo "Ejecuta con sudo: sudo bash instalar.sh"; exit 1; fi
command -v python3 >/dev/null || { echo "Falta python3 (sudo apt install python3)"; exit 1; }

id -u dashboard-ia >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin dashboard-ia

mkdir -p "$DESTINO/data"
install -m 644 "$ORIGEN/proxy.py" "$ORIGEN/index.html" "$DESTINO/"
[[ -f "$DESTINO/config.json" ]] || install -m 644 "$ORIGEN/config.ejemplo.json" "$DESTINO/config.json"
[[ -f "$ORIGEN/data/chatgpt_miembros.csv" && ! -f "$DESTINO/data/chatgpt_miembros.csv" ]] && \
  install -m 640 "$ORIGEN/data/chatgpt_miembros.csv" "$DESTINO/data/"
chown -R root:root "$DESTINO"
chown -R dashboard-ia:dashboard-ia "$DESTINO/data"
chmod 750 "$DESTINO/data"

if [[ ! -f /etc/dashboard-ia.env ]]; then
  install -m 600 -o root -g root "$ORIGEN/dashboard-ia.env.ejemplo" /etc/dashboard-ia.env
  echo ">> Creado /etc/dashboard-ia.env: edítalo y pon la Admin key y la contraseña."
fi

install -m 644 "$ORIGEN/dashboard-ia.service" /etc/systemd/system/dashboard-ia.service
systemctl daemon-reload
systemctl enable --now dashboard-ia.service
systemctl restart dashboard-ia.service

PUERTO=$(python3 -c "import json;print(json.load(open('$DESTINO/config.json')).get('puerto',8090))")
echo
echo "Instalado. Estado:   systemctl status dashboard-ia"
echo "Registro:            journalctl -u dashboard-ia -f"
echo "Dashboard:           http://$(hostname -I | awk '{print $1}'):$PUERTO"
