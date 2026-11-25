# Stage 1: build iPXE with framebuffer, PNG and HTTPS support plus the embedded script.
FROM debian:bookworm-slim AS ipxe
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates gcc libc6-dev make perl liblzma-dev mtools binutils xz-utils \
 && rm -rf /var/lib/apt/lists/*
ARG IPXE_REF=master
RUN git clone --depth 1 --branch "${IPXE_REF}" https://github.com/ipxe/ipxe.git /ipxe
WORKDIR /ipxe/src
COPY ipxe/local/ config/local/
COPY ipxe/embed.ipxe /embed.ipxe
RUN make -j"$(nproc)" NO_WERROR=1 EMBED=/embed.ipxe \
      bin/undionly.kpxe bin-x86_64-efi/snponly.efi bin-x86_64-efi/ipxe.efi

# Stage 2: dnsmasq (ProxyDHCP + TFTP) serving the iPXE binaries.
FROM alpine:3.22
RUN apk add --no-cache dnsmasq
COPY --from=ipxe /ipxe/src/bin/undionly.kpxe \
                 /ipxe/src/bin-x86_64-efi/snponly.efi \
                 /ipxe/src/bin-x86_64-efi/ipxe.efi /srv/tftp/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
