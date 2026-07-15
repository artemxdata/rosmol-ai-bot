#!/bin/sh

certificate="/etc/letsencrypt/live/139.100.225.44/fullchain.pem"
private_key="/etc/letsencrypt/live/139.100.225.44/privkey.pem"
force_http="/etc/letsencrypt/.force-http"

if [ ! -e "$force_http" ] && [ -s "$certificate" ] && [ -s "$private_key" ]; then
    cp /etc/nginx/rosmol/admin-tls.conf /etc/nginx/conf.d/default.conf
else
    cp /etc/nginx/rosmol/default.conf /etc/nginx/conf.d/default.conf
fi

exec /docker-entrypoint.sh "$@"
