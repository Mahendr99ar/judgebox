# One image for the API and the workers.
# Ubuntu 24.04 ships the compilers contestants expect: g++ 13, Python 3.12, OpenJDK 21.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv g++ openjdk-21-jdk-headless ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY scripts/build_pch.sh /tmp/build_pch.sh
RUN /tmp/build_pch.sh

WORKDIR /app
COPY pyproject.toml ./
COPY judgebox ./judgebox
RUN python3 -m venv /opt/venv && pip install --no-cache-dir .

COPY web ./web
COPY problems ./problems

EXPOSE 8000
CMD ["uvicorn", "judgebox.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
