#!/bin/bash

# --- Konfiguration ---
# Name des Docker-Images
IMAGE_NAME="blackleakzde/test"
# Tag für das Docker-Image
IMAGE_TAG="release"
# Vollständiger Image-Name
FULL_IMAGE_NAME="${IMAGE_NAME}:${IMAGE_TAG}"
# Name des Python-Skripts im Container
PYTHON_SCRIPT="bak.py"
# Lokales Verzeichnis für die Ausgabe des Builds (wird in den Container gemountet)
LOCAL_OUTPUT_DIR="output"
# Verzeichnis im Container, wohin der lokale Output-Ordner gemountet wird
CONTAINER_BUILD_DIR="/app/build"
# Verzeichnis im Container, wohin der aktuelle Host-Ordner gemountet wird (enthält test.py)
CONTAINER_APP_DIR="/app"

# --- Funktionen ---

# Funktion zum Prüfen, ob ein Befehl existiert
command_exists () {
  command -v "$1" >/dev/null 2>&1
}

# Funktion zum Bauen des Docker-Images
build_docker_image() {
  echo "--- Baue Docker-Image ${FULL_IMAGE_NAME} ---"
  # Überprüfen, ob Docker installiert ist
  if ! command_exists docker; then
    echo "Fehler: Docker ist nicht installiert. Bitte installieren Sie Docker, um fortzufahren."
    exit 1
  fi

  # Docker-Image bauen
  # --no-cache kann nützlich sein, um sicherzustellen, dass alle Schichten neu gebaut werden,
  # besonders wenn sich die Quelldateien geändert haben.
  # Entferne --no-cache für schnellere Builds, wenn die Dockerfile-Schichten stabil sind.
  if ! docker build -t "${FULL_IMAGE_NAME}" .; then
    echo "Fehler: Docker-Image konnte nicht gebaut werden."
    exit 1
  fi
  echo "Docker-Image ${FULL_IMAGE_NAME} erfolgreich gebaut."
}

# Funktion zum Ausführen des Docker-Containers
run_docker_container() {
  echo "--- Führe Docker-Container aus ---"

  # Lokales Output-Verzeichnis erstellen, falls es nicht existiert
  mkdir -p "${LOCAL_OUTPUT_DIR}"
  echo "Stelle sicher, dass lokales Ausgabeverzeichnis '${LOCAL_OUTPUT_DIR}' existiert."

  # Docker-Container ausführen
  # --rm: Entfernt den Container nach Beendigung
  # --privileged: Erforderlich für Operationen wie mkfs.ext4, mount/umount innerhalb des Containers
  # -v "$(pwd)":/app: Mountet das aktuelle Host-Verzeichnis (wo sich dieses Skript und test.py befinden)
  #                     nach /app im Container, um sicherzustellen, dass die aktuelle test.py verwendet wird.
  # -v "$(pwd)/${LOCAL_OUTPUT_DIR}":${CONTAINER_BUILD_DIR}: Mountet das lokale Output-Verzeichnis
  #                                                          in den Build-Ordner des Containers.
  # ${FULL_IMAGE_NAME}: Das zu verwendende Docker-Image
  # python3 /app/${PYTHON_SCRIPT}: Der Befehl, der im Container ausgeführt wird (dein Python-Skript)
  if ! sudo docker run --rm --privileged \
    -v "$(pwd)":"${CONTAINER_APP_DIR}" \
    -v "$(pwd)/${LOCAL_OUTPUT_DIR}":"${CONTAINER_BUILD_DIR}" \
    "${FULL_IMAGE_NAME}" \
    python3 "${CONTAINER_APP_DIR}/${PYTHON_SCRIPT}"; then
    echo "Fehler: Docker-Container konnte nicht ausgeführt werden."
    exit 1
  fi
  echo "Docker-Container-Ausführung abgeschlossen."
}

# --- Hauptlogik ---

# Das Skript benötigt Root-Rechte, da Docker-Befehle (insbesondere mit --privileged)
# und mount/umount im Container Root-Rechte erfordern.
# Hier wird geprüft, ob es mit sudo ausgeführt wird.
if [ "$(id -u)" -ne 0 ]; then
  echo "Dieses Skript muss mit Root-Rechten ausgeführt werden (z.B. mit 'sudo')."
  echo "Bitte führen Sie es erneut aus mit: sudo ./run_builder.sh"
  exit 1
fi

# Docker-Image bauen
build_docker_image

# Docker-Container ausführen
run_docker_container

echo "--- Skript-Ausführung beendet ---"
