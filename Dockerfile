FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow

# Системные зависимости: python3, pip, а также библиотеки,
# нужные curl_cffi (собственный libcurl-импл, но иногда требуются
# базовые ssl/ca-certificates пакеты) и lxml (libxml2/libxslt).
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    ca-certificates \
    libxml2 \
    libxslt1.1 \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/bin/python

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app/ .

# Непривилегированный пользователь
RUN useradd --create-home --shell /bin/bash parser \
    && mkdir -p /data/output \
    && chown -R parser:parser /app /data
USER parser

VOLUME ["/data/output"]

ENTRYPOINT ["python3", "main.py"]
CMD ["--help"]
