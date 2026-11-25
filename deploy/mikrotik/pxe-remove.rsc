# Undo pxe.rsc: stop announcing PXE boot on the LAN DHCP.

/ip dhcp-server network unset [find address=192.168.1.0/24] value-name=boot-file-name
/ip dhcp-server network unset [find address=192.168.1.0/24] value-name=next-server
/ip dhcp-server network set [find address=192.168.1.0/24] dhcp-option=""
/ip dhcp-server matcher remove [find comment="pxe"]
/ip dhcp-server option sets remove [find name=pxe-efi]
/ip dhcp-server option remove [find comment~"^pxe"]
/ip tftp remove [find comment="pxe"]
