#!/bin/sh
# User data for a newly created TailBridge host; cloud-init runs it once as root.
# Never apply it to an existing machine. Replace <machine> before passing it.
set -eu
name='<machine>'

# Keep the machine name across reboots; cloud-init would otherwise restore the metadata hostname
hostnamectl set-hostname "$name"
printf 'preserve_hostname: true\n' > /etc/cloud/cloud.cfg.d/99-tailbridge.cfg

# Key-only SSH for the image's default user; earlier drop-ins take precedence
cat > /etc/ssh/sshd_config.d/10-tailbridge.conf <<'CONF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
X11Forwarding no
CONF
sshd -t
systemctl try-reload-or-restart ssh
