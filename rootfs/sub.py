import os
import sys
import shutil
import pathlib
import subprocess
import tempfile
from tqdm import tqdm
import stat
import requests
import yaml
import re
import shlex
import logging
import coloredlogs
import tarfile
import time
import datetime

# --- Konfiguration aus YAML laden ---
config_file = pathlib.Path("busybox.yaml")

try:
    with config_file.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
except FileNotFoundError:
    print(f"Fehler: Konfigurationsdatei '{config_file}' nicht gefunden.")
    sys.exit(1)
except yaml.YAMLError as e:
    print(f"Fehler beim Parsen der Konfigurationsdatei '{config_file}': {e}")
    sys.exit(1)

# Variablen aus der Konfiguration zuweisen
busybox_version = config.get("busybox_version")
busybox_url = config.get("busybox_download_url")
arch = config.get("arch")
cross_prefix = config.get("cross_compile_prefix")
disabled_features = config.get("disabled_features", [])
cross_compiler_root = config.get("cross_compiler_root")

# --- Globale Pfade und Verzeichnisse ---
build_dir = pathlib.Path("build")
rootfs_dir = build_dir / "rootfs"
rootfs_full_path = rootfs_dir.absolute()

# Pfade basierend auf den geladenen Variablen anpassen
busybox_tar_gz = build_dir / f"busybox-{busybox_version}.tar.bz2"
busybox_src_dir = build_dir / f"busybox-{busybox_version}"
busybox_defconfig_source = pathlib.Path("busybox.defconfig")
firmware_clone_dir = build_dir / "raspberrypi-firmware"

# --- Rootfs-Ordnerstruktur (bereinigt) ---
rootfs_dirs = [
    "bin", "sbin", "etc", "proc", "sys", "dev", "lib", "lib64", "home",
    "usr", "usr/bin", "usr/sbin", "usr/lib", "usr/local",
    "var", "var/log", "var/run", "var/tmp", "var/lock",
    "mnt", "opt", "srv",
    "root",
    "home/pi",
]

# --- Hilfsfunktion zum Ausführen von Shell-Befehlen ---
def run_command(commands, cwd=None, env=None, desc="Befehl ausführen", check_root=False):
    """
    Führt einen Shell-Befehl aus, kapselt die subprocess.run-Logik
    und gibt die Ausgabe des Befehls aus.
    """
    if check_root and os.geteuid() != 0:
        print(f"Fehler: Befehl '{' '.join(commands)}' erfordert Root-Rechte. Bitte Skript als root ausführen (z.B. mit sudo).")
        sys.exit(1)

    print(f"\n--- {desc} ---")
    
    cwd_str = str(cwd) if isinstance(cwd, pathlib.Path) else cwd

    try:
        result = subprocess.run(
            commands,
            env=env,
            cwd=cwd_str,
            capture_output=True,
            text=True,
            check=True
        )

        if result.stdout:
            print("Standardausgabe:")
            print(result.stdout.strip())
        
        if result.stderr:
            print("Standardfehlerausgabe:")
            print("Standardfehlerausgabe:")
            print(result.stderr.strip())

        print(f"Befehl '{' '.join(commands)}' erfolgreich abgeschlossen.")
        
    except FileNotFoundError:
        print(f"Fehler: Befehl '{commands[0]}' nicht gefunden. Stellen Sie sicher, dass er im PATH liegt oder der Pfad korrekt ist.")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"Fehler bei Befehl '{' '.join(commands)}':")
        print(f"Exit Code: {e.returncode}")
        if e.stdout:
            print("Standardausgabe:")
            print(e.stdout.strip())
        if e.stderr:
            print("Standardfehlerausgabe:")
            print(e.stderr.strip())
        sys.exit(1)


# --- Hauptfunktionen des Rootfs-Build-Prozesses ---

def create_dirs():
    """Erstellt den Build-Ordner und die Rootfs-Struktur."""
    print(f"Erstelle Build-Verzeichnis: {build_dir}")
    build_dir.mkdir(parents=True, exist_ok=True)

    print(f"Erstelle Rootfs-Verzeichnis: {rootfs_dir}")
    rootfs_dir.mkdir(parents=True, exist_ok=True)

    print("Erstelle grundlegende Rootfs-Ordner...")
    for d in tqdm(rootfs_dirs, desc="Ordner erstellen"):
        (rootfs_dir / d).mkdir(exist_ok=True)
    
    os.chmod(rootfs_dir / "tmp", 0o1777)
    os.chmod(rootfs_dir / "var/log", 0o777)
    os.chmod(rootfs_dir / "var/run", 0o777)
    os.chmod(rootfs_dir / "var/tmp", 0o1777)
    print("Spezielle Berechtigungen für /tmp, /var/log, /var/run, /var/tmp gesetzt.")


def download_busybox():
    """Lädt BusyBox herunter und entpackt es mit einem Fortschrittsbalken."""
    if busybox_tar_gz.exists():
        print(f"BusyBox Archiv bereits vorhanden: {busybox_tar_gz}")
    else:
        print(f"Lade BusyBox von {busybox_url} herunter...")
        try:
            response = requests.get(busybox_url, stream=True)
            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))
            block_size = 8192

            with tqdm(total=total_size, unit='B', unit_scale=True, desc=busybox_tar_gz.name, ncols=80) as pbar:
                with open(str(busybox_tar_gz), 'wb') as f:
                    for chunk in response.iter_content(chunk_size=block_size):
                        f.write(chunk)
                        pbar.update(len(chunk))
            print("Download abgeschlossen.")
        except requests.exceptions.RequestException as e:
            print(f"Fehler beim Download von BusyBox: {e}")
            sys.exit(1)

    if busybox_src_dir.exists():
        print(f"BusyBox Quellverzeichnis bereits vorhanden: {busybox_src_dir}")
    else:
        print(f"Entpacke BusyBox nach {busybox_src_dir}...")
        try:
            with tarfile.open(str(busybox_tar_gz), 'r:bz2') as tar:
                tar.extractall(path=busybox_src_dir.parent)
            print("Entpacken abgeschlossen.")
        except tarfile.ReadError as e:
            print(f"Fehler beim Entpacken von BusyBox: {e}. Ist die Datei korrekt? ({busybox_tar_gz})")
            sys.exit(1)


def configure_and_install_busybox():
    """
    Konfiguriert BusyBox mittels einer bereitgestellten .defconfig,
    modifiziert diese basierend auf `disabled_features`,
    kompiliert es und installiert es in das Rootfs.
    """
    print("\n--- Starte BusyBox Konfiguration und Installation ---")
    
    env_for_make = os.environ.copy()
    env_for_make["ARCH"] = arch
    env_for_make["CROSS_COMPILE"] = cross_prefix

    if not busybox_defconfig_source.exists():
        print(f"Fehler: Benutzerdefinierte Konfigurationsdatei '{busybox_defconfig_source}' nicht gefunden.")
        print("Bitte erstellen Sie diese Datei im selben Verzeichnis wie 'main.py'.")
        print("Anleitung dazu finden Sie in den Kommentaren des Skripts.")
        sys.exit(1)

    print(f"Kopiere '{busybox_defconfig_source}' als '.config' nach '{busybox_src_dir}'...")
    try:
        shutil.copy2(busybox_defconfig_source, busybox_src_dir / ".config")
        print("'.config' erfolgreich kopiert.")
    except Exception as e:
        print(f"Fehler beim Kopieren der .config-Datei: {e}")
        sys.exit(1)

    print("Führe 'make oldconfig' aus, um die Konfiguration zu finalisieren...")
    run_command(
        commands=["make", "oldconfig"],
        cwd=busybox_src_dir,
        env=env_for_make,
        desc="BusyBox make oldconfig"
    )

    if disabled_features:
        config_path = busybox_src_dir / ".config"
        print(f"Modifiziere BusyBox .config zur Deaktivierung der Features: {', '.join(disabled_features)}")
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            modified_lines = []
            for line in lines:
                modified = False
                for feature in disabled_features:
                    feature_pattern = f"CONFIG_{feature.upper()}="
                    
                    if line.startswith(feature_pattern):
                        if line.strip().endswith(('=y', '=m')):
                            modified_lines.append(f"# {feature_pattern} is not set\n")
                            print(f"  - Deaktiviert: {feature}")
                            modified = True
                            break
                    elif line.strip() == f"# {feature_pattern} is not set":
                        print(f"  - {feature} bereits deaktiviert.")
                        modified = True
                        break
                
                if not modified:
                    modified_lines.append(line)

            with open(config_path, 'w', encoding='utf-8') as f:
                f.writelines(modified_lines)
            print("BusyBox .config erfolgreich modifiziert.")
        except Exception as e:
            print(f"Fehler beim Modifizieren der BusyBox .config: {e}")
            sys.exit(1)

    print("Kompiliere BusyBox...")
    run_command(
        commands=["make"],
        cwd=busybox_src_dir,
        env=env_for_make,
        desc="BusyBox Kompilierung"
    )

    print(f"Installiere BusyBox in {rootfs_dir}...")
    run_command(
        commands=["make", f"INSTALL_DIR={rootfs_dir.as_posix()}", "install"],
        cwd=busybox_src_dir,
        env=env_for_make,
        desc="BusyBox Installation"
    )
    
    print("\n--- BusyBox Konfiguration und Installation abgeschlossen ---")


def create_essential_rootfs_files():
    """Erstellt grundlegende Dateien wie fstab, inittab (für BusyBox init) und init-Skripte."""
    print("\nErstelle essenzielle Rootfs-Dateien...")

    files_to_create = {
        "etc/fstab": """# <file system> <mount point> <type> <options> <dump> <pass>
proc            /proc           proc    defaults        0       0
sysfs           /sys            sysfs   defaults        0       0
tmpfs           /tmp            tmpfs   defaults        0       0
# Gerätedateien werden normalerweise dynamisch gemountet oder von mdev/udev verwaltet
""",
        "etc/inittab": """# /etc/inittab for busybox init
::sysinit:/etc/init.d/rcS
::respawn:-/bin/sh
::ctrlaltdel:/sbin/reboot
::shutdown:/bin/sync
::shutdown:/sbin/poweroff
""",
        "etc/init.d/rcS": """#!/bin/sh
# /etc/init.d/rcS - Hauptinitialisierungsskript für BusyBox

echo "Starte BusyBox Init-Skript..."

# Mounten der Pseudo-Dateisysteme
echo "Mounting Dateisysteme..."
/bin/mount -t proc proc /proc
/bin/mount -t sysfs sysfs /sys
/bin/mount -t devtmpfs devtmpfs /dev # Wichtig für Gerätedateien

# mdev ausführen, um Geräteknoten dynamisch zu erstellen
# Stellen Sie sicher, dass mdev in Ihrer BusyBox-Konfiguration aktiviert ist!
echo "Populating /dev mit mdev..."
/sbin/mdev -s

# Aktivieren des Loopback-Interfaces
echo "Konfiguriere Loopback-Interface..."
/sbin/ifconfig lo 127.0.0.1 up

# Beispiel: Netzwerk-Interface konfigurieren (ersetzen Sie eth0 und IP-Adresse nach Bedarf)
# echo "Konfiguriere Netzwerk-Interface eth0..."
# /sbin/ifconfig eth0 192.168.1.10 netmask 255.255.255.0 up
# /sbin/route add default gw 192.168.1.1

echo "Systeminitialisierung abgeschlossen."
echo "Willkommen bei Ihrem BusyBox-System!"
""",
        "etc/profile": """# /etc/profile
export PATH=/bin:/sbin:/usr/bin:/usr/sbin:/usr/local/bin:/usr/local/sbin
PS1='\\u@\\h:\\w\\$ '
""",
        "etc/hostname": "raspberrypi",
        "etc/resolv.conf": "nameserver 8.8.8.8", # Google DNS als Beispiel
        "etc/passwd": "root:x:0:0:root:/root:/bin/sh\npi:x:1000:1000:Linux User,,,:/home/pi:/bin/sh\n",
        "etc/group": "root:x:0:\npi:x:1000:\n",
        "etc/shadow": "root:*:1:0:99999:7:::\npi:*:1:0:99999:7:::\n" # Passwörter sind gesperrt, können später mit 'passwd' geändert werden
    }

    for filename_relative, content in tqdm(files_to_create.items(), desc="Essenzielle Dateien erstellen"):
        file_path = rootfs_dir / filename_relative
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        if filename_relative == "etc/init.d/rcS":
            os.chmod(file_path, 0o755)
    print("Essenzielle Rootfs-Dateien erstellt.")


def create_device_nodes():
    """
    Erstellt die minimal benötigten statischen Geräteknoten in /dev.
    Für ein dynamisches /dev ist mdev/udev in BusyBox nötig, das dies zur Laufzeit übernimmt.
    """
    print("\nErstelle statische Geräteknoten in /dev...")
    dev_dir = rootfs_dir / "dev"

    device_nodes = [
        ("console", 5, 1, 'c'),
        ("null", 1, 3, 'c'),
        ("zero", 1, 5, 'c'),
        ("random", 1, 8, 'c'),
        ("urandom", 1, 9, 'c'),
        ("tty", 5, 0, 'c'),
        ("tty0", 4, 0, 'c'),
        ("tty1", 4, 1, 'c'),
    ]

    for name, major, minor, type_char in tqdm(device_nodes, desc="Geräteknoten erstellen"):
        dev_path = dev_dir / name
        try:
            if not dev_path.exists():
                mode = 0o666 if type_char == 'c' else 0o660
                os.mknod(dev_path, mode | (stat.S_IFCHR if type_char == 'c' else stat.S_IFBLK), os.makedev(major, minor))
        except OSError as e:
            print(f"WARNUNG: Konnte Geräteknoten '{name}' nicht erstellen (Typ {type_char}, {major}:{minor}): {e}")
            print("         Dies ist normal, wenn 'mdev -s' (in init-Skript) diese dynamisch erstellt.")
        except Exception as e:
            print(f"Unerwarteter Fehler bei '{name}': {e}")
    print("Statische Geräteknoten-Erstellung abgeschlossen (ggf. dynamisch durch mdev/udev ergänzt).")


def set_rootfs_permissions(rootfs_base_dir: pathlib.Path):
    """
    Setzt grundlegende Dateisystemberechtigungen für ein BusyBox-Rootfs.
    """
    print(f"\nSetze grundlegende Berechtigungen für {rootfs_base_dir}...")

    print("Setze Standard-Ordnerberechtigungen (0o755)...")
    for dirpath, dirnames, filenames in os.walk(rootfs_base_dir):
        current_dir_path = pathlib.Path(dirpath)
        try:
            if current_dir_path not in [
                rootfs_base_dir / "tmp",
                rootfs_base_dir / "var" / "tmp",
                rootfs_base_dir / "var" / "log",
                rootfs_base_dir / "var" / "run",
                rootfs_base_dir / "var" / "lock",
                rootfs_base_dir / "dev"
            ]:
                 os.chmod(current_dir_path, 0o755)
        except Exception as e:
            print(f"WARNUNG: Konnte Berechtigung für Ordner '{current_dir_path}' nicht setzen: {e}")

    print("Setze Standard-Dateiberechtigungen (0o644/0o755)...")
    for dirpath, dirnames, filenames in os.walk(rootfs_base_dir):
        for filename in filenames:
            file_path = pathlib.Path(dirpath) / filename
            try:
                if (file_path.parent.name in ["bin", "sbin"]) or \
                   (file_path.parent.parent.name == "usr" and file_path.parent.name in ["bin", "sbin"]) or \
                   (file_path.name.endswith(".sh") and file_path.is_file() and b"#!/bin/sh" in file_path.read_bytes()) or \
                   (file_path.name == "rcS" and file_path.parent.name == "init.d") or \
                   (file_path.name == "init" and file_path.parent == rootfs_base_dir): # Für root-init-Skript
                    os.chmod(file_path, 0o755) # Ausführbar
                else:
                    os.chmod(file_path, 0o644) # Nur lesbar für andere, schreibbar für Besitzer
            except Exception as e:
                print(f"WARNUNG: Konnte Berechtigung für Datei '{file_path}' nicht setzen: {e}")

    print("Setze spezielle Verzeichnisberechtigungen...")
    
    special_dirs = [
        rootfs_base_dir / "tmp",
        rootfs_base_dir / "var" / "tmp"
    ]
    for s_dir in special_dirs:
        if s_dir.exists() and s_dir.is_dir():
            try:
                os.chmod(s_dir, 0o1777)
                print(f"  - '{s_dir}' auf 0o1777 (Sticky Bit) gesetzt.")
            except Exception as e:
                print(f"WARNUNG: Konnte spezielle Berechtigung für '{s_dir}' nicht setzen: {e}")

    rw_dirs = [
        rootfs_base_dir / "var" / "log",
        rootfs_base_dir / "var" / "run",
        rootfs_base_dir / "var" / "lock"
    ]
    for rw_dir in rw_dirs:
        if rw_dir.exists() and rw_dir.is_dir():
            try:
                os.chmod(rw_dir, 0o777)
                print(f"  - '{rw_dir}' auf 0o777 gesetzt.")
            except Exception as e:
                print(f"WARNUNG: Konnte spezielle Berechtigung für '{rw_dir}' nicht setzen: {e}")

    dev_dir = rootfs_base_dir / "dev"
    if dev_dir.exists() and dev_dir.is_dir():
        try:
            os.chmod(dev_dir, 0o755)
            print(f"  - '{dev_dir}' auf 0o755 gesetzt.")
        except Exception as e:
            print(f"WARNUNG: Konnte spezielle Berechtigung für '{dev_dir}' nicht setzen: {e}")
    
    if rootfs_base_dir.exists() and rootfs_base_dir.is_dir():
        try:
            os.chmod(rootfs_base_dir, 0o755)
            print(f"  - Rootfs-Basis '{rootfs_base_dir}' auf 0o755 gesetzt.")
        except Exception as e:
            print(f"WARNUNG: Konnte Berechtigung für Rootfs-Basis nicht setzen: {e}")

    print("Berechtigungseinstellung abgeschlossen.")


def create_rootfs_ext4_image(rootfs_base_dir: pathlib.Path, image_output_path: pathlib.Path, image_size_mb: int = 512):
    """
    Erstellt eine leere EXT4-Image-Datei, kopiert den Inhalt des Rootfs dorthin
    und hängt das Image anschließend aus.

    Args:
        rootfs_base_dir: Das Basisverzeichnis des Rootfs (z.B. pathlib.Path("build/rootfs")).
        image_output_path: Der Pfad, unter dem die Image-Datei erstellt werden soll
                           (z.B. pathlib.Path("build/rootfs.ext4")).
        image_size_mb: Die gewünschte Größe des Image in Megabyte.
    """
    print(f"\n--- Erstelle EXT4-Image '{image_output_path}' ---")
    
    # 1. Root-Rechte prüfen
    if os.geteuid() != 0:
        print("Fehler: Diese Funktion erfordert Root-Rechte. Bitte Skript als root ausführen (z.B. mit sudo).")
        sys.exit(1)

    if not rootfs_base_dir.is_dir():
        print(f"Fehler: Das Rootfs-Verzeichnis '{rootfs_base_dir}' existiert nicht oder ist kein Verzeichnis.")
        sys.exit(1)

    temp_mount_point = None
    try:
        # 2. Temporäres Mount-Verzeichnis erstellen
        temp_mount_point = pathlib.Path(tempfile.mkdtemp(prefix="rootfs_mount_"))
        print(f"Temporäres Mount-Verzeichnis erstellt: {temp_mount_point}")

        # 3. Leere Image-Datei erstellen
        print(f"Erstelle leere Image-Datei '{image_output_path}' mit {image_size_mb} MB...")
        try:
            # Fallocate ist schneller als dd für das Allokieren von Speicher
            run_command(["fallocate", "-l", f"{image_size_mb}M", str(image_output_path)], desc="Image-Datei allokieren", check_root=True)
        except subprocess.CalledProcessError:
            # Fallback zu dd, falls fallocate nicht verfügbar ist
            print("Fallocate nicht verfügbar oder fehlgeschlagen, verwende dd als Fallback...")
            run_command(["dd", "if=/dev/zero", f"of={image_output_path}", "bs=1M", f"count={image_size_mb}"], desc="Image-Datei allokieren (dd)", check_root=True)
        print("Image-Datei erstellt.")

        # 4. Datei als ext4-Dateisystem formatieren
        print(f"Formatiere '{image_output_path}' als ext4-Dateisystem...")
        run_command(["mkfs.ext4", "-F", str(image_output_path)], desc="ext4 formatieren", check_root=True)
        print("Image formatiert.")

        # 5. Image mounten
        print(f"Hänge Image '{image_output_path}' an '{temp_mount_point}' ein...")
        run_command(["mount", str(image_output_path), str(temp_mount_point)], desc="Image mounten", check_root=True)
        print("Image eingehängt.")

        # 6. Inhalt des Rootfs in das Image kopieren
        # rsync ist am besten für das Kopieren von Dateisystemen, da es Berechtigungen, Symlinks usw. beibehält.
        # Die abschließenden Slashes bei den Pfaden sind wichtig!
        print(f"Kopiere Inhalt von '{rootfs_base_dir}' nach '{temp_mount_point}'...")
        run_command(["rsync", "-a", "--info=progress2", f"{rootfs_base_dir.as_posix()}/", f"{temp_mount_point.as_posix()}/"], desc="Rootfs in Image kopieren", check_root=True)
        print("Inhalt erfolgreich kopiert.")

    except Exception as e:
        print(f"Kritischer Fehler beim Erstellen des EXT4-Images: {e}")
        sys.exit(1)
    finally:
        # 7. Image aushängen (immer versuchen, auch bei Fehlern)
        if temp_mount_point and temp_mount_point.is_mount(): # Prüfen, ob wirklich gemountet ist
            print(f"Hänge Image von '{temp_mount_point}' aus...")
            try:
                run_command(["umount", str(temp_mount_point)], desc="Image aushängen", check_root=True)
                print("Image erfolgreich ausgehängt.")
            except Exception as e:
                print(f"WARNUNG: Konnte Image von '{temp_mount_point}' nicht aushängen: {e}")
                print("         Bitte manuell überprüfen und ggf. 'sudo umount -l {temp_mount_point}' verwenden.")

        # 8. Temporäres Mount-Verzeichnis bereinigen (immer versuchen)
        if temp_mount_point and temp_mount_point.exists():
            print(f"Entferne temporäres Mount-Verzeichnis: {temp_mount_point}")
            try:
                shutil.rmtree(temp_mount_point)
                print("Temporäres Mount-Verzeichnis entfernt.")
            except Exception as e:
                print(f"WARNUNG: Konnte temporäres Verzeichnis '{temp_mount_point}' nicht entfernen: {e}")
                print("         Bitte manuell entfernen.")

    print(f"--- EXT4-Image '{image_output_path}' erfolgreich erstellt ---")


def create_bootfs_vfat_image(output_image_path: pathlib.Path, firmware_clone_dir: pathlib.Path, image_size_mb: int = 256):
    """
    Klont das Raspberry Pi Firmware-Repository, erstellt ein FAT32-Image,
    kopiert den Inhalt des 'boot'-Ordners dorthin und hängt es wieder aus.
    Erstellt zusätzlich cmdline.txt und config.txt für Raspberry Pi 5.

    Args:
        output_image_path: Der Pfad, unter dem die bootfs.vfat-Datei erstellt werden soll.
        firmware_clone_dir: Das Verzeichnis, in das das Firmware-Repository geklont wird.
        image_size_mb: Die gewünschte Größe des FAT32-Images in Megabyte.
    """
    print(f"\n--- Erstelle BOOTFS (FAT32) Image '{output_image_path}' ---")
    
    # 1. Root-Rechte prüfen
    if os.geteuid() != 0:
        print("Fehler: Diese Funktion erfordert Root-Rechte. Bitte Skript als root ausführen (z.B. mit sudo).")
        sys.exit(1)

    firmware_repo_url = "https://github.com/raspberrypi/firmware.git"
    temp_mount_point = None

    try:
        # 2. Raspberry Pi Firmware-Repository klonen oder aktualisieren
        if firmware_clone_dir.exists():
            print(f"Firmware-Verzeichnis '{firmware_clone_dir}' existiert bereits. Versuche zu aktualisieren...")
            run_command(["git", "pull"], cwd=firmware_clone_dir, desc="Firmware aktualisieren")
        else:
            print(f"Klone Raspberry Pi Firmware von '{firmware_repo_url}' nach '{firmware_clone_dir}'...")
            run_command(["git", "clone", firmware_repo_url, str(firmware_clone_dir)], desc="Firmware klonen")

        source_boot_dir = firmware_clone_dir / "boot"
        if not source_boot_dir.is_dir():
            print(f"Fehler: Der 'boot'-Ordner wurde im geklonten Repository nicht gefunden: {source_boot_dir}")
            sys.exit(1)
        print(f"Quell-Boot-Verzeichnis: {source_boot_dir}")

        # 3. Temporäres Mount-Verzeichnis erstellen
        temp_mount_point = pathlib.Path(tempfile.mkdtemp(prefix="bootfs_mount_"))
        print(f"Temporäres Mount-Verzeichnis erstellt: {temp_mount_point}")

        # 4. Leere Image-Datei erstellen
        print(f"Erstelle leere Image-Datei '{output_image_path}' mit {image_size_mb} MB...")
        try:
            run_command(["fallocate", "-l", f"{image_size_mb}M", str(output_image_path)], desc="Image-Datei allokieren", check_root=True)
        except subprocess.CalledProcessError:
            print("Fallocate nicht verfügbar oder fehlgeschlagen, verwende dd als Fallback...")
            run_command(["dd", "if=/dev/zero", f"of={output_image_path}", "bs=1M", f"count={image_size_mb}"], desc="Image-Datei allokieren (dd)", check_root=True)
        print("Image-Datei erstellt.")

        # 5. Datei als FAT32 (vfat) Dateisystem formatieren
        print(f"Formatiere '{output_image_path}' als FAT32-Dateisystem...")
        run_command(["mkfs.vfat", "-F", "32", str(output_image_path)], desc="FAT32 formatieren", check_root=True)
        print("Image formatiert.")

        # 6. Image mounten
        print(f"Hänge Image '{output_image_path}' an '{temp_mount_point}' ein...")
        run_command(["mount", str(output_image_path), str(temp_mount_point)], desc="Image mounten", check_root=True)
        print("Image eingehängt.")

        # 7. Inhalt des 'boot'-Ordners in das Image kopieren
        print(f"Kopiere Inhalt von '{source_boot_dir}' nach '{temp_mount_point}'...")
        # rsync kopiert Dateien und Ordner rekursiv, -a behält Attribute bei
        run_command(["rsync", "-a", "--info=progress2", f"{source_boot_dir.as_posix()}/", f"{temp_mount_point.as_posix()}/"], desc="Boot-Dateien in Image kopieren", check_root=True)
        print("Inhalt erfolgreich kopiert.")

        # 8. Erstellen von cmdline.txt und config.txt
        print("Erstelle cmdline.txt und config.txt...")
        
        # cmdline.txt Inhalt
        cmdline_content = (
            "console=ttyS0,115200 console=tty1 "
            "root=/dev/mmcblk0p2 rootfstype=ext4 "
            "rootwait init=/init "
            "cma=128M "
            "quiet rw "
            "initrd=initramfs.gz" # Ihr Initramfs-Name
        )
        (temp_mount_point / "cmdline.txt").write_text(cmdline_content + "\n")
        print("cmdline.txt erstellt.")

        # config.txt Inhalt
        config_content = """
# Raspberry Pi 5 specific configuration for minimal boot
arm_64bit=1
kernel=kernel8.img
initramfs initramfs.gz followkernel

# Enable UART for serial console
enable_uart=1
# Route Bluetooth to miniUART to free up ttyS0 for console (if applicable)
# For RPi5, this might be handled differently or not needed depending on firmware/kernel
# If you run into issues, try removing this line or researching RPi5 UART setup.
# dtoverlay=miniuart-bt

# Generic settings
dtparam=audio=on

# Disable any unnecessary boot messages or splash screens
disable_splash=1
disable_overscan=1
overscan_left=0
overscan_right=0
overscan_top=0
overscan_bottom=0

# Maximize compatibility
# force_turbo=1 # Consider this if you need maximum performance, but it voids warranty.
"""
        (temp_mount_point / "config.txt").write_text(config_content.strip() + "\n")
        print("config.txt erstellt.")


    except Exception as e:
        print(f"Kritischer Fehler beim Erstellen des BOOTFS-Images: {e}")
        sys.exit(1)
    finally:
        # 9. Image aushängen (immer versuchen, auch bei Fehlern)
        if temp_mount_point and temp_mount_point.is_mount():
            print(f"Hänge Image von '{temp_mount_point}' aus...")
            try:
                run_command(["umount", str(temp_mount_point)], desc="Image aushängen", check_root=True)
                print("Image erfolgreich ausgehängt.")
            except Exception as e:
                print(f"WARNUNG: Konnte Image von '{temp_mount_point}' nicht aushängen: {e}")
                print("         Bitte manuell überprüfen und ggf. 'sudo umount -l {temp_mount_point}' verwenden.")

        # 10. Temporäres Mount-Verzeichnis bereinigen
        if temp_mount_point and temp_mount_point.exists():
            print(f"Entferne temporäres Mount-Verzeichnis: {temp_mount_point}")
            try:
                shutil.rmtree(temp_mount_point)
                print("Temporäres Mount-Verzeichnis entfernt.")
            except Exception as e:
                print(f"WARNUNG: Konnte temporäres Verzeichnis '{temp_mount_point}' nicht entfernen: {e}")
                print("         Bitte manuell entfernen.")

    print(f"--- BOOTFS (FAT32) Image '{output_image_path}' erfolgreich erstellt ---")


def find_dynamic_libraries(binary_path, cross_compiler_root):
    """
    Findet die dynamischen Bibliotheken, die von einer Binärdatei benötigt werden,
    indem es 'readelf -d' verwendet und die Pfade im Cross-Compiler-Root sucht.
    Gibt eine Liste von pathlib.Path-Objekten zurück.
    """
    needed_libs = set()
    
    # Sicherstellen, dass readelf im PATH ist, oder den vollständigen Pfad verwenden
    # Beispiel: aarch64-linux-gnu-readelf
    readelf_cmd = f"{cross_prefix}readelf"
    
    try:
        result = subprocess.run(
            [readelf_cmd, "-d", str(binary_path)],
            capture_output=True,
            text=True,
            check=True
        )
        
        for line in result.stdout.splitlines():
            if "NEEDED" in line:
                match = re.search(r"Shared library: \[(.*?)]", line)
                if match:
                    lib_name = match.group(1)
                    needed_libs.add(lib_name)
                    
    except FileNotFoundError:
        print(f"WARNUNG: '{readelf_cmd}' nicht gefunden. Kann dynamische Bibliotheken nicht identifizieren.")
        return []
    except subprocess.CalledProcessError as e:
        print(f"WARNUNG: Fehler bei Ausführung von '{readelf_cmd} -d {binary_path}': {e.stderr}")
        return []
    
    found_lib_paths = []
    if not cross_compiler_root:
        print("WARNUNG: 'cross_compiler_root' ist nicht in busybox.yaml definiert. Dynamische Bibliotheken können nicht gefunden werden.")
        print("         Stellen Sie sicher, dass Ihre BusyBox statisch kompiliert ist oder fügen Sie 'cross_compiler_root' hinzu.")
        return []

    # Suchen der Bibliotheken im Cross-Compiler sysroot
    # Typische Pfade: lib/, usr/lib/, aarch64-linux-gnu/lib/, aarch64-linux-gnu/usr/lib/
    search_paths = [
        pathlib.Path(cross_compiler_root) / f"{cross_prefix.rstrip('-')}" / "libc", # /usr/aarch64-linux-gnu/libc
        pathlib.Path(cross_compiler_root) / f"{cross_prefix.rstrip('-')}" / "lib", # /usr/aarch64-linux-gnu/lib
        pathlib.Path(cross_compiler_root) / "lib",
        pathlib.Path(cross_compiler_root) / "usr" / "lib",
        pathlib.Path(cross_compiler_root) / "usr" / f"{cross_prefix.rstrip('-')}" / "lib",
        pathlib.Path(cross_compiler_root) / "usr" / f"{cross_prefix.rstrip('-')}" / "libc"
    ]
    # Suchen Sie nach dem typischen sysroot-Pfad, z.B. /usr/aarch64-linux-gnu/libc/lib
    sysroot_path = pathlib.Path(cross_compiler_root) / "aarch64-linux-gnu" / "libc"
    if sysroot_path.is_dir():
         search_paths.append(sysroot_path / "lib")
         search_paths.append(sysroot_path / "usr" / "lib")
    else: # Fallback for different sysroot structures
        for p in pathlib.Path(cross_compiler_root).glob(f"**/{cross_prefix.rstrip('-')}*"):
            if p.is_dir() and (p / "lib").is_dir():
                search_paths.append(p / "lib")
                search_paths.append(p / "usr" / "lib")

    print(f"Suche nach dynamischen Bibliotheken in: {search_paths}")

    for lib_name in needed_libs:
        found = False
        for search_path in search_paths:
            lib_path = search_path / lib_name
            if lib_path.exists():
                found_lib_paths.append(lib_path)
                found = True
                break
        if not found:
            print(f"WARNUNG: Benötigte Bibliothek '{lib_name}' nicht im Cross-Compiler-Root gefunden.")

    return list(set(found_lib_paths)) # Entferne Duplikate


def create_initramfs_output_dir(output_dir: pathlib.Path):
    """
    Erstellt das Ausgabeverzeichnis für das Initramfs, falls es noch nicht existiert.

    Args:
        output_dir: Der Pfad zum Verzeichnis, in dem das Initramfs gespeichert werden soll.
    """
    print(f"\n--- Erstelle Initramfs-Ausgabeverzeichnis: {output_dir} ---")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Verzeichnis '{output_dir}' erfolgreich erstellt oder existiert bereits.")
    except Exception as e:
        print(f"Fehler: Konnte Initramfs-Ausgabeverzeichnis '{output_dir}' nicht erstellen: {e}")
        sys.exit(1)


def create_initramfs(rootfs_base_dir: pathlib.Path, output_initramfs_path: pathlib.Path, cross_compiler_root: str):
    """
    Erstellt ein Initramfs (Initial RAM Filesystem) aus ausgewählten Dateien des Rootfs
    und den benötigten dynamischen Bibliotheken.

    Args:
        rootfs_base_dir: Das Basisverzeichnis Ihres erstellten Rootfs (z.B. build/rootfs).
        output_initramfs_path: Der Pfad, unter dem das initramfs.gz erstellt werden soll.
        cross_compiler_root: Der Pfad zum Root-Verzeichnis Ihres Cross-Compilers (sysroot),
                             um dynamische Bibliotheken zu finden.
                             (z.B. "/usr/aarch64-linux-gnu" oder "/opt/cross/aarch64-linux-gnu/libc")
    """
    print(f"\n--- Erstelle Initramfs '{output_initramfs_path}' ---")

    if os.geteuid() != 0:
        print("Fehler: Diese Funktion erfordert Root-Rechte. Bitte Skript als root ausführen (z.B. mit sudo).")
        sys.exit(1)

    temp_initramfs_staging_dir = None
    try:
        temp_initramfs_staging_dir = pathlib.Path(tempfile.mkdtemp(prefix="initramfs_staging_"))
        print(f"Temporäres Initramfs-Staging-Verzeichnis erstellt: {temp_initramfs_staging_dir}")

        # 1. BusyBox Binärdatei und Symlinks kopieren
        print("Kopiere BusyBox-Binärdatei und Symlinks...")
        # Stellen Sie sicher, dass busybox in /bin oder /sbin im rootfs_base_dir ist
        busybox_bin = rootfs_base_dir / "bin" / "busybox"
        if not busybox_bin.exists():
            print(f"Fehler: BusyBox-Binärdatei nicht gefunden in {busybox_bin}. Kann kein Initramfs erstellen.")
            sys.exit(1)
        
        # Erstelle die Verzeichnisstruktur im Staging-Bereich
        (temp_initramfs_staging_dir / "bin").mkdir(exist_ok=True)
        (temp_initramfs_staging_dir / "sbin").mkdir(exist_ok=True)
        (temp_initramfs_staging_dir / "lib").mkdir(exist_ok=True)
        (temp_initramfs_staging_dir / "lib64").mkdir(exist_ok=True) # Für AArch64
        (temp_initramfs_staging_dir / "dev").mkdir(exist_ok=True)
        (temp_initramfs_staging_dir / "proc").mkdir(exist_ok=True)
        (temp_initramfs_staging_dir / "sys").mkdir(exist_ok=True)
        (temp_initramfs_staging_dir / "mnt").mkdir(exist_ok=True) # Für Root-Mount-Point
        
        # Kopiere die BusyBox-Binärdatei
        shutil.copy2(busybox_bin, temp_initramfs_staging_dir / "bin" / "busybox")
        os.chmod(temp_initramfs_staging_dir / "bin" / "busybox", 0o755)

        # Erstelle Symlinks für BusyBox-Applets (z.B. sh, mount, mkdir)
        print("Erstelle BusyBox Symlinks...")
        run_command(
            commands=[f"{temp_initramfs_staging_dir}/bin/busybox", "--install", "-s", str(temp_initramfs_staging_dir / "bin")],
            desc="BusyBox Symlinks in /bin erstellen",
            cwd=temp_initramfs_staging_dir,
            check_root=False
        )
        run_command(
            commands=[f"{temp_initramfs_staging_dir}/bin/busybox", "--install", "-s", str(temp_initramfs_staging_dir / "sbin")],
            desc="BusyBox Symlinks in /sbin erstellen",
            cwd=temp_initramfs_staging_dir,
            check_root=False
        )

        # 2. Dynamische Bibliotheken kopieren (falls BusyBox dynamisch gelinkt ist)
        print("Kopiere benötigte dynamische Bibliotheken...")
        needed_libs = find_dynamic_libraries(busybox_bin, cross_compiler_root)
        
        for lib_path in tqdm(needed_libs, desc="Bibliotheken kopieren"):
            dest_dir = temp_initramfs_staging_dir / lib_path.parent.relative_to(pathlib.Path(cross_compiler_root).root)
            if not dest_dir.is_dir():
                if "lib64" in lib_path.parts:
                    dest_dir = temp_initramfs_staging_dir / "lib64"
                elif "lib" in lib_path.parts:
                    dest_dir = temp_initramfs_staging_dir / "lib"
                dest_dir.mkdir(parents=True, exist_ok=True)
                
            shutil.copy2(lib_path, dest_dir)
            if lib_path.is_symlink():
                link_target = os.readlink(str(lib_path))
                target_path = dest_dir / os.path.basename(lib_path)
                if target_path.exists():
                    target_path.unlink()
                os.symlink(link_target, target_path)
        print("Dynamische Bibliotheken kopiert.")

        # 3. Init-Skript für Initramfs erstellen
        print("Erstelle /init Skript für das Initramfs...")
        init_script_content = f"""#!/bin/sh
# /init for Initramfs

# Mount essential filesystems
/bin/mount -t proc proc /proc
/bin/mount -t sysfs sysfs /sys
/bin/mount -t devtmpfs devtmpfs /dev

# Populate /dev (mdev requires busybox to be compiled with mdev support)
echo "Populating /dev with mdev..."
/sbin/mdev -s

# Optional: Add any necessary kernel modules here if your rootfs is on complex hardware
# E.g., /sbin/modprobe xhci_hcd
# E.g., /sbin/modprobe ext4

# Mount the real root filesystem
# The 'root' parameter in cmdline.txt will define where the real rootfs is.
# Ensure the partition matches the one defined in cmdline.txt (e.g., /dev/mmcblk0p2)
echo "Attempting to mount root filesystem..."
/bin/mount -o ro /dev/mmcblk0p2 /mnt

# Check if mount was successful
if [ $? -ne 0 ]; then
    echo "Failed to mount root filesystem. Dropping to a shell."
    /bin/sh
else
    echo "Root filesystem mounted successfully. Pivoting..."
    # Pivot to the real root filesystem
    exec /bin/busybox switch_root /mnt /sbin/init # Or /mnt/init
fi

echo "Initramfs has finished its job. If you see this, something went wrong with switch_root."
/bin/sh # Fallback shell
"""
        init_script_path = temp_initramfs_staging_dir / "init"
        init_script_path.write_text(init_script_content)
        os.chmod(init_script_path, 0o755)
        print("/init Skript erstellt.")

        # 4. Statische Gerätedateien erstellen (minimal)
        print("Erstelle statische Gerätedateien für Initramfs /dev...")
        dev_dir = temp_initramfs_staging_dir / "dev"
        device_nodes = [
            ("console", 5, 1, 'c'),
            ("null", 1, 3, 'c'),
            ("zero", 1, 5, 'c'),
            ("tty", 5, 0, 'c'),
        ]
        for name, major, minor, type_char in tqdm(device_nodes, desc="Initramfs Geräteknoten"):
            dev_path = dev_dir / name
            if not dev_path.exists():
                mode = 0o666 if type_char == 'c' else 0o660
                os.mknod(dev_path, mode | (stat.S_IFCHR if type_char == 'c' else stat.S_IFBLK), os.makedev(major, minor))
        print("Initramfs Gerätedateien erstellt.")

        # 5. CPIO-Archiv erstellen
        print("Erstelle CPIO-Archiv...")
        cpio_archive_path = temp_initramfs_staging_dir / "initramfs.cpio"
        
        file_list_cmd = ["find", ".", "-print0"]
        cpio_cmd = ["cpio", "--null", "-o", "--format=newc"]

        find_proc = subprocess.Popen(file_list_cmd, stdout=subprocess.PIPE, cwd=temp_initramfs_staging_dir)
        cpio_proc = subprocess.Popen(cpio_cmd, stdin=find_proc.stdout, stdout=subprocess.PIPE, cwd=temp_initramfs_staging_dir)
        find_proc.stdout.close()

        cpio_output, cpio_err = cpio_proc.communicate()

        if find_proc.returncode != 0:
            raise subprocess.CalledProcessError(find_proc.returncode, file_list_cmd, stderr=find_proc.stderr)
        if cpio_proc.returncode != 0:
            raise subprocess.CalledProcessError(cpio_proc.returncode, cpio_cmd, stderr=cpio_err)

        with open(str(cpio_archive_path), "wb") as f:
            f.write(cpio_output)
        print("CPIO-Archiv erstellt.")


        # 6. CPIO-Archiv mit Gzip komprimieren
        print(f"Komprimiere CPIO-Archiv zu '{output_initramfs_path}'...")
        run_command(
            commands=["gzip", "-f", str(cpio_archive_path)],
            desc="Initramfs komprimieren",
            cwd=temp_initramfs_staging_dir,
            check_root=False
        )
        shutil.move(str(cpio_archive_path) + ".gz", str(output_initramfs_path))
        print("Initramfs erfolgreich komprimiert.")

    except Exception as e:
        print(f"Kritischer Fehler beim Erstellen des Initramfs: {e}")
        sys.exit(1)
    finally:
        if temp_initramfs_staging_dir and temp_initramfs_staging_dir.exists():
            print(f"Entferne temporäres Initramfs-Staging-Verzeichnis: {temp_initramfs_staging_dir}")
            try:
                shutil.rmtree(temp_initramfs_staging_dir)
                print("Temporäres Initramfs-Staging-Verzeichnis entfernt.")
            except Exception as e:
                print(f"WARNUNG: Konnte temporäres Verzeichnis '{temp_initramfs_staging_dir}' nicht entfernen: {e}")
                print("         Bitte manuell entfernen.")

    print(f"--- Initramfs '{output_initramfs_path}' erfolgreich erstellt ---")


# --- Hauptfunktion des Skripts ---
def main():
    print("--- Starte Rootfs-Erstellung für Raspberry Pi 5 (ARM64) ---")
    print(f"Ziel-Rootfs-Pfad: {rootfs_full_path}")

    create_dirs()
    download_busybox()
    configure_and_install_busybox()
    create_essential_rootfs_files()
    create_device_nodes()
    set_rootfs_permissions(rootfs_dir)
    
    # 1. Erstellen des Rootfs.ext4 Images
    output_rootfs_image_path = build_dir / "rootfs.ext4"
    rootfs_image_size = 512 # Größe des Rootfs-Images in MB
    create_rootfs_ext4_image(rootfs_dir, output_rootfs_image_path, rootfs_image_size)

    # 2. Erstellen des Initramfs
    output_initramfs_path = build_dir / "initramfs.gz"
    # Vor dem Erstellen des Initramfs das Ausgabeverzeichnis sicherstellen
    create_initramfs_output_dir(output_initramfs_path.parent) # Das Elternverzeichnis ist das Ausgabeverzeichnis
    create_initramfs(rootfs_dir, output_initramfs_path, cross_compiler_root)

    # 3. Erstellen des Bootfs.vfat Images
    output_bootfs_image_path = build_dir / "bootfs.vfat"
    bootfs_image_size = 256 # Größe des Bootfs-Images in MB
    create_bootfs_vfat_image(output_bootfs_image_path, firmware_clone_dir, bootfs_image_size)


    print("\n--- Gesamt-Image-Erstellungsprozess abgeschlossen ---")
    print(f"Ihr minimales BusyBox-Rootfs befindet sich in: {rootfs_full_path}")
    print(f"Das erstellte EXT4-Image finden Sie unter: {output_rootfs_image_path.absolute()}")
    print(f"Das erstellte BOOTFS (FAT32) Image finden Sie unter: {output_bootfs_image_path.absolute()}")
    print(f"Das erstellte Initramfs finden Sie unter: {output_initramfs_path.absolute()}")
    print("\nNächste Schritte (manuell):")
    print("1. Beide Images müssen auf separate Partitionen einer SD-Karte geschrieben werden.")
    print("   Typische Partitionierung für RPi:")
    print("   - Partition 1 (BOOT): FAT32, ca. 256MB, enthält den Inhalt von bootfs.vfat")
    print("     Stellen Sie sicher, dass 'initramfs.gz' und 'kernel8.img' in der Boot-Partition sind.")
    print("   - Partition 2 (ROOT): EXT4, restlicher Speicherplatz, enthält den Inhalt von rootfs.ext4")
    print("   Dies kann mit Tools wie 'fdisk', 'mkfs', 'dd' und 'mount' (oder 'Raspberry Pi Imager') manuell erfolgen.")
    print("   **Wichtiger Hinweis**: Raspberry Pi Imager ist der einfachste Weg, aber er verwendet vorgefertigte Images.")
    print("   Für dieses benutzerdefinierte Build müssen Sie manuell partitionieren und kopieren.")
    print("\nDenken Sie daran, dass die Erstellung eines wirklich bootfähigen Images komplexer ist und die")
    print("genaue Konfiguration des Kernels und des Bootloaders spezifisch für RPi5 sein muss.")
    print("\nFehlermeldungen im Initramfs deuten oft auf fehlende Bibliotheken oder fehlgeschlagene Mount-Befehle hin.")
    print("Überprüfen Sie 'readelf -d' auf Ihre BusyBox-Binärdatei im Rootfs, um benötigte Bibliotheken zu identifizieren.")

if __name__ == "__main__":
    main()