FROM alpine:latest

RUN apk add --no-cache \
    python3 \
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

COPY app.py .
COPY index.html .

RUN mkdir -p /data

ENV PORT=7681

EXPOSE 7681

CMD ["python3", "/app/app.py"]
