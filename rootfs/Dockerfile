# Verwende ein Ubuntu-Basisimage, das für Build-Zwecke geeignet ist
FROM ubuntu:latest

# Setze das Arbeitsverzeichnis im Container
WORKDIR /app

# Installiere benötigte Systempakete
# - build-essential: Enthält gcc, make, etc.
# - crossbuild-essential-arm64: Der ARM64 Cross-Compiler (aarch64-linux-gnu-gcc)
# - python3, python3-pip: Python und Pip
# - e2fsprogs: Enthält mkfs.ext4
# - rsync: Für das Kopieren der Rootfs-Dateien in das Image
# - util-linux: Enthält fallocate
# - bzip2, tar: Für das Entpacken von BusyBox
# - wget/curl: Für den Download, falls requests fehlschlägt oder nicht verwendet wird
RUN apt update && \
    apt install -y \
        sudo \
        build-essential \
        crossbuild-essential-arm64 \
        python3-dev \
        python3-pip \
        python3-venv \
        e2fsprogs \
        rsync \
        util-linux \
        bzip2 \
        gzip \
        xz-utils \
        p7zip-full \
        gcc \
        clang \
        make \
        cmake \
        gcc-11-aarch64-linux-gnu \
        libc6-dev \
        software-properties-common \
        wget \
        curl \
        tar \
        wget && \
    rm -rf /var/lib/apt/lists/*

# Installiere Python-Pakete (z.B. PyYAML, tqdm, requests)
# Stellen Sie sicher, dass alle von Ihrem Skript benötigten Python-Libs hier aufgeführt sind


# Kopiere alle benötigten Dateien in das Arbeitsverzeichnis des Containers
# Das sind Ihr Python-Skript, die YAML-Konfiguration und die defconfig-Datei
COPY . .

RUN pip3 install --break-system-packages PyYAML tqdm requests coloredlogs
# Das Kommando, das ausgeführt wird, wenn der Container gestartet wird
# Das Skript benötigt Root-Rechte, um das ext4-Image zu erstellen und zu mounten.
# Da der Docker-Container selbst mit Privilegien gestartet wird,
# werden die Befehle innerhalb des Containers auch mit Root-Rechten ausgeführt.
CMD ["python3", "new.py"]