#!/bin/sh
# Create the share user and start smbd in the foreground.
#
# If PXE_LOG_DIR is set (shared with the controller), smbd's log (including the
# full_audit lines: who connects, which files are opened) is also appended to
# $PXE_LOG_DIR/smb.log, and a `smbstatus --json` snapshot (live sessions and
# open files) is written to $PXE_LOG_DIR/smbstatus.json every few seconds.
set -eu

SMB_USER="${SMB_USER:-pxe}"
SMB_PASS="${SMB_PASS:-pxe}"
SMBSTATUS_INTERVAL="${SMBSTATUS_INTERVAL:-10}"

id "$SMB_USER" >/dev/null 2>&1 || adduser -D -H -s /sbin/nologin "$SMB_USER"
printf '%s\n%s\n' "$SMB_PASS" "$SMB_PASS" | smbpasswd -a -s "$SMB_USER" >/dev/null
sed "s/@SMB_USER@/${SMB_USER}/" /etc/samba/smb.conf.template > /etc/samba/smb.conf

if [ -n "${PXE_LOG_DIR:-}" ]; then
    mkdir -p "$PXE_LOG_DIR"
    fifo=/run/smbd-log.fifo
    rm -f "$fifo"
    mkfifo "$fifo"
    # tee keeps the log on stdout (kubectl/docker logs) and in the shared file
    tee -a "$PXE_LOG_DIR/smb.log" < "$fifo" &
    (
        while sleep "$SMBSTATUS_INTERVAL"; do
            smbstatus --json > "$PXE_LOG_DIR/.smbstatus.tmp" 2>/dev/null \
                && mv "$PXE_LOG_DIR/.smbstatus.tmp" "$PXE_LOG_DIR/smbstatus.json"
        done
    ) &
    exec smbd --foreground --debug-stdout --no-process-group > "$fifo" 2>&1
fi

exec smbd --foreground --debug-stdout --no-process-group
