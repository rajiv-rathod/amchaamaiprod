#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${1:-info.adminoabc.org}"
APP_PORT="${2:-8080}"
NGINX_CONF="/etc/nginx/sites-available/${DOMAIN}.conf"
NGINX_LINK="/etc/nginx/sites-enabled/${DOMAIN}.conf"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./scripts/setup_nginx.sh ${DOMAIN} ${APP_PORT}"
  exit 1
fi

cat > "${NGINX_CONF}" <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

ln -sf "${NGINX_CONF}" "${NGINX_LINK}"
if ! nginx -t; then
  echo "Nginx config test failed. Please fix the config and rerun."
  exit 1
fi
systemctl reload nginx

echo "Nginx configured. Point DNS A record for ${DOMAIN} to this server IP."
echo "Optional HTTPS: sudo certbot --nginx -d ${DOMAIN}"
