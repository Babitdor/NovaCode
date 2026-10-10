FROM ubuntu:24.04

COPY quartzctl /usr/local/bin/quartzctl
COPY TROUBLESHOOTING.md /usr/share/doc/quartz/TROUBLESHOOTING.md
COPY seed/ /tmp/seed/
COPY data/ /app/data/

RUN chmod +x /usr/local/bin/quartzctl \
 && mkdir -p /etc/quartz /var/lib/quartz/datasets \
 && printf '%s' 'qz-7f3a91c2e5' > /etc/quartz/arm.token \
 && for f in /tmp/seed/*.csv; do \
      gzip -n -c "$f" | base64 > "/var/lib/quartz/datasets/$(basename "$f" .csv).qz"; \
    done \
 && rm -rf /tmp/seed

WORKDIR /app
