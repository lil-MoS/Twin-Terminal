FROM alpine:latest

RUN apk add --no-cache \
    python3 \
    py3-pip \
    bash \
    curl \
    wget \
    git \
    nano \
    vim \
    htop \
    procps \
    util-linux \
    ca-certificates

RUN pip3 install --no-cache-dir --break-system-packages aiohttp

WORKDIR /app

COPY app.py /app/app.py
COPY index.html /app/index.html

RUN mkdir -p /data

ENV PORT=7681
ENV TWIN_PASSWORD=changeme

EXPOSE 7681

CMD ["python3", "/app/app.py"]
