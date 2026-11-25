#!/bin/sh
# Create the share user and start smbd in the foreground.
set -eu

SMB_USER="${SMB_USER:-pxe}"
SMB_PASS="${SMB_PASS:-pxe}"

id "$SMB_USER" >/dev/null 2>&1 || adduser -D -H -s /sbin/nologin "$SMB_USER"
printf '%s\n%s\n' "$SMB_PASS" "$SMB_PASS" | smbpasswd -a -s "$SMB_USER" >/dev/null
sed "s/@SMB_USER@/${SMB_USER}/" /etc/samba/smb.conf.template > /etc/samba/smb.conf

exec smbd --foreground --debug-stdout --no-process-group
