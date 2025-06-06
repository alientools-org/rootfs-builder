#!/bin/bash

set -euo pipefail

ROOTFS_DIR="rootfs"
IMAGE_FILE="rootfs.ext4"
IMAGE_SIZE_MB=512

if [[ "$EUID" -ne 0 ]]; then
  echo "❌ Bitte als root ausführen (z.B. mit sudo)."
  exit 1
fi

if [[ ! -d "$ROOTFS_DIR" ]]; then
  echo "❌ Verzeichnis '$ROOTFS_DIR' existiert nicht."
  exit 1
fi

MOUNT_DIR=$(mktemp -d -t rootfs_mount_XXXX)
if [[ ! -d "$MOUNT_DIR" ]]; then
  echo "❌ Konnte temporäres Mount-Verzeichnis nicht erstellen."
  exit 1
fi
echo "🗂 Temporäres Mount-Verzeichnis: $MOUNT_DIR"

# Cleanup Funktion, auch für Loop-Device
cleanup() {
  echo "🧹 Aufräumen..."
  if mountpoint -q "$MOUNT_DIR"; then
    echo "🔽 Hänge Image aus..."
    umount "$MOUNT_DIR" || echo "⚠️ Aushängen fehlgeschlagen"
  fi
  if [[ -n "${LOOP_DEVICE:-}" ]]; then
    echo "🌀 Loop-Gerät trennen: $LOOP_DEVICE"
    losetup -d "$LOOP_DEVICE" || echo "⚠️ Loop-Gerät konnte nicht getrennt werden"
  fi
  rm -rf "$MOUNT_DIR"
  echo "✅ Aufräumen abgeschlossen."
}
trap cleanup EXIT

echo "📦 Erstelle Image-Datei '$IMAGE_FILE' mit $IMAGE_SIZE_MB MB..."
if command -v fallocate &>/dev/null; then
  fallocate -l "${IMAGE_SIZE_MB}M" "$IMAGE_FILE"
else
  echo "ℹ️ fallocate nicht verfügbar, benutze dd..."
  dd if=/dev/zero of="$IMAGE_FILE" bs=1M count="$IMAGE_SIZE_MB"
fi

echo "🧷 Formatiere '$IMAGE_FILE' als ext4-Dateisystem..."
mkfs.ext4 -F "$IMAGE_FILE"

echo "🌀 Loop-Gerät für Image setzen..."
LOOP_DEVICE=$(losetup --find --show "$IMAGE_FILE")
echo "📀 Image ist nun am Loop-Gerät: $LOOP_DEVICE"

echo "🔼 Hänge Loop-Gerät '$LOOP_DEVICE' in '$MOUNT_DIR' ein..."
mount "$LOOP_DEVICE" "$MOUNT_DIR"

echo "📂 Kopiere Inhalte von '$ROOTFS_DIR' nach '$MOUNT_DIR'..."
rsync -a "$ROOTFS_DIR"/ "$MOUNT_DIR"/

echo "✅ Rootfs-Image erfolgreich erstellt: $IMAGE_FILE"
