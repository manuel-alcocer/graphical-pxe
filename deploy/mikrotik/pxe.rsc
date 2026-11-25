# PXE boot through the MikroTik DHCP server (RouterOS 7.x).
#
# - BIOS clients get "undionly.kpxe", UEFI x64 clients get "snponly.efi",
#   both over the MikroTik's own TFTP server (next-server = the router).
# - Every client also gets private option 224 with the IP of the HTTP/SMB
#   server (Kubernetes LoadBalancer); the embedded iPXE script reads it and
#   loads http://<that IP>/boot.ipxe.
#
# Upload the binaries first (./pxe export-ipxe ipxe-bin):
#   scp ipxe-bin/undionly.kpxe ipxe-bin/snponly.efi admin@192.168.1.2:pxe/
# then: /import file-name=pxe.rsc

# Values for this LAN (no :local variables: "/import verbose=yes" runs line by line):
#   HTTP/SMB server 192.168.1.214, router 192.168.1.2, LAN 192.168.1.0/24, DHCP server "defconf"

/ip tftp
add req-filename=undionly.kpxe real-filename=pxe/undionly.kpxe ip-addresses=192.168.1.0/24 read-only=yes allow=yes comment="pxe"
add req-filename=snponly.efi real-filename=pxe/snponly.efi ip-addresses=192.168.1.0/24 read-only=yes allow=yes comment="pxe"

/ip dhcp-server option
add name=pxe-http code=224 value="s'192.168.1.214'" force=yes comment="pxe: HTTP/SMB server for iPXE"
add name=pxe-boot-efi code=67 value="'snponly.efi'" comment="pxe: UEFI x64 boot file"

/ip dhcp-server option sets
add name=pxe-efi options=pxe-http,pxe-boot-efi

# Option 93 (client system architecture): 0x0007 = EFI x64 (0x0009 is also EFI x64 on some firmware)
# address-pool must be the normal pool: the default (static-only) leaves UEFI PXE clients without an IP.
# s'...' forces a string; a bare quoted IP is stored as 4 binary bytes.
# force=yes on option 224: RouterOS only sends options the client requests, and iPXE does not request 224.
/ip dhcp-server matcher
add name=pxe-efi7 server=defconf code=93 value=0x0007 matching-type=exact address-pool=dhcp option-set=pxe-efi comment="pxe"
add name=pxe-efi9 server=defconf code=93 value=0x0009 matching-type=exact address-pool=dhcp option-set=pxe-efi comment="pxe"

/ip dhcp-server network
set [find address=192.168.1.0/24] next-server=192.168.1.2 boot-file-name=undionly.kpxe dhcp-option=pxe-http
