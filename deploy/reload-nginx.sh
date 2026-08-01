#!/bin/sh
# certbot deploy-hook: runs automatically after every successful renewal
# (dropped into /etc/letsencrypt/renewal-hooks/deploy/, which certbot
# executes unconditionally post-renewal -- no per-cert config needed).
# The IP-address cert is short-lived (~6-7 days); nginx must reload to
# pick up the new cert/key files each time, or it keeps serving the
# expiring one out of its already-open file descriptors.
systemctl reload nginx
