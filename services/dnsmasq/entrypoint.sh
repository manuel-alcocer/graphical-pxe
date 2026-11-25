#!/bin/sh
# Render the dnsmasq configuration from environment variables and start it.
set -eu

: "${INTERFACE:?INTERFACE must be set in .env}"
DHCP_MODE="${DHCP_MODE:-proxy}"
CONF=/etc/pxe-dnsmasq.conf

cat > "$CONF" <<CFG
# DNS disabled: we only do DHCP/ProxyDHCP and TFTP
port=0
log-dhcp
log-facility=-
interface=${INTERFACE}
bind-interfaces
enable-tftp
tftp-root=/srv/tftp
dhcp-match=set:efi64,option:client-arch,7
dhcp-match=set:efi64,option:client-arch,9
CFG

case "$DHCP_MODE" in
  proxy)
    : "${PROXY_SUBNET:?PROXY_SUBNET must be set in .env for proxy mode}"
    cat >> "$CONF" <<CFG
# ProxyDHCP: the existing DHCP server hands out IPs, we only add PXE boot info
dhcp-range=${PROXY_SUBNET},proxy
pxe-service=x86PC,"Graphical PXE (BIOS)",undionly.kpxe
pxe-service=x86-64_EFI,"Graphical PXE (UEFI)",snponly.efi
pxe-service=BC_EFI,"Graphical PXE (UEFI)",snponly.efi
CFG
    ;;
  full)
    : "${DHCP_RANGE:?DHCP_RANGE must be set in .env for full mode}"
    {
      echo "dhcp-range=${DHCP_RANGE}"
      echo "dhcp-boot=tag:efi64,snponly.efi"
      echo "dhcp-boot=tag:!efi64,undionly.kpxe"
      [ -n "${DHCP_GATEWAY:-}" ] && echo "dhcp-option=option:router,${DHCP_GATEWAY}"
      [ -n "${DHCP_DNS:-}" ] && echo "dhcp-option=option:dns-server,${DHCP_DNS}"
      true
    } >> "$CONF"
    ;;
  *)
    echo "Unknown DHCP_MODE '$DHCP_MODE' (expected 'proxy' or 'full')" >&2
    exit 1
    ;;
esac

echo "---- dnsmasq configuration ----"
cat "$CONF"
echo "-------------------------------"
exec dnsmasq --keep-in-foreground --conf-file="$CONF"
