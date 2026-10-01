FROM alpine:latest

RUN apk add --no-cache \
    ttyd \
    bash \
    curl \
    wget \
    git \
    nano \
    vim \
    htop \
    ca-certificates \
    tzdata

WORKDIR /data

COPY start.sh /start.sh
COPY index.html /data/index.html

RUN chmod +x /start.sh

ENV PORT=7681
ENV TZ=UTC

EXPOSE 7681

CMD ["/start.sh"]
