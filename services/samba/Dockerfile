FROM alpine:3.22
RUN apk add --no-cache samba rsync
COPY smb.conf /etc/samba/smb.conf.template
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
