import os
import sys
import shutil
import tempfile
import pathlib
import time
import datetime
import requests
import yaml
import re
import shlex
import logging
import tarfile
import subprocess
import stat # Benötigt für os.mknod, um Dateitypen zu spezifizieren

from tqdm import tqdm
from subprocess import Popen, STDOUT, PIPE
from pathlib import Path




config_file = Path("busybox.yaml")

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

build_dir = Path("build")
output_dir = Path("output")
rootfs_dir = build_dir / "rootfs"
rootfs_full_path = rootfs_dir.absolute()
bootfs_dir = build_dir / "bootfs"
bootfs_full_path = bootfs_dir.absolute()
initramfs_dir = build_dir / "initramfs"
initramfs_dir_full_path = initramfs_dir.absolute()
kernel_dir = build_dir / "kernel"
kernel_full_path = kernel_dir.absolute()

output_image_dir = output_dir / "images"
output_image_dir_full_path = output_image_dir.absolute()
output_bootfs_dir = output_dir / "bootfs"
output_bootfs_dir_full_path = output_bootfs_dir.absolute()
output_initramfs_dir = output_dir / "initramfs"
output_initramfs_dir_full_path = output_initramfs_dir.absolute()
output_kernel_dir = output_dir / "kernel"
output_kernel_dir_full_path = output_kernel_dir.absolute()
output_rootfs_dir = output_dir / "rootfs"
output_rootfs_dir_full_path = output_rootfs_dir.absolute()



busybox_tar_gz = build_dir / f"busybox-{busybox_version}.tar.bz2"
busybox_src_dir = build_dir / f"busybox-{busybox_version}"
busybox_defconfig_source = pathlib.Path("busybox.defconfig") # Die vorbereitete defconfig-Datei


rootfs_dirs = [
    "bin", "sbin", "etc", "proc", "sys", "dev", "lib", "lib64", "home", "tmp", "var",
    "boot",
    "usr", "usr/bin", "usr/sbin", "usr/lib", "usr/local",
    "var", "var/log", "var/run", "var/tmp", "var/lock",
    "mnt", "opt", "srv",
    "root",
    "home/pi",
]


def run_command(commands, cwd=None, env=None, desc="Befehl ausführen", check_root=False):
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


def create_dirs():
    """Erstellt den Build-Ordner und die Rootfs-Struktur."""
    print(f"Erstelle Build-Verzeichnis: {build_dir}")
    build_dir.mkdir(parents=True, exist_ok=True)

    print(f"Erstelle Rootfs-Verzeichnis: {rootfs_dir}")
    rootfs_dir.mkdir(parents=True, exist_ok=True)

    print("Erstelle grundlegende Rootfs-Ordner...")
    for d in tqdm(rootfs_dirs, desc="Ordner erstellen"):
        # *********** WICHTIG: Hier muss parents=True stehen ***********
        (rootfs_dir / d).mkdir(parents=True, exist_ok=True)
    
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
            block_size = 8192 # 8KB chunks

            with tqdm(total=total_size, unit='B', unit_scale=True, desc=busybox_tar_gz.name, ncols=80) as pbar:
                with open(str(busybox_tar_gz), 'wb') as f: # Konvertiere Path-Objekt explizit zu String
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
            with tarfile.open(str(busybox_tar_gz), 'r:bz2') as tar: # Konvertiere Path-Objekt explizit zu String
                tar.extractall(path=busybox_src_dir.parent)
            print("Entpacken abgeschlossen.")
        except tarfile.ReadError as e:
            print(f"Fehler beim Entpacken von BusyBox: {e}. Ist die Datei korrekt? ({busybox_tar_gz})")
            sys.exit(1)


def configure_and_install_busybox():
    """
    Konfiguriert BusyBox mittels einer bereitgestellten .defconfig,
    kompiliert es und installiert es in das Rootfs.
    """
    print("\n--- Starte BusyBox Konfiguration und Installation ---")
    
    # Umgebungsvariablen für die Cross-Kompilierung
    env_for_make = os.environ.copy()
    env_for_make["ARCH"] = arch # 'arch' kommt aus der YAML
    env_for_make["CROSS_COMPILE"] = cross_prefix # 'cross_prefix' kommt aus der YAML

    # 1. Prüfe, ob die benutzerdefinierte busybox.defconfig existiert
    if not busybox_defconfig_source.exists():
        print(f"Fehler: Benutzerdefinierte Konfigurationsdatei '{busybox_defconfig_source}' nicht gefunden.")
        print("Bitte erstellen Sie diese Datei im selben Verzeichnis wie 'main.py'.")
        print("Anleitung dazu finden Sie in den Kommentaren des Skripts.")
        sys.exit(1)

    # 2. Kopiere die benutzerdefinierte busybox.defconfig als .config in das Quellverzeichnis
    print(f"Kopiere '{busybox_defconfig_source}' als '.config' nach '{busybox_src_dir}'...")
    try:
        shutil.copy2(busybox_defconfig_source, busybox_src_dir / ".config")
        print("'.config' erfolgreich kopiert.")
    except Exception as e:
        print(f"Fehler beim Kopieren der .config-Datei: {e}")
        sys.exit(1)

    # 3. Führe 'make oldconfig' aus, um die .config zu aktualisieren und fehlende Optionen zu setzen
    # Dies ist besser als make defconfig, wenn eine spezifische .config verwendet wird.
    # Es fragt interaktiv nach neuen Optionen, aber mit stdin auf /dev/null wird es Standardwerte verwenden.
    print("Führe 'make oldconfig' aus, um die Konfiguration zu finalisieren...")
    # run_command verwenden
    run_command(
        commands=["make", "oldconfig"],
        cwd=busybox_src_dir,
        env=env_for_make,
        desc="BusyBox make oldconfig"
    )

    # 4. Kompilieren von BusyBox
    run_command(
        commands=["make"],
        cwd=busybox_src_dir,
        env=env_for_make,
        desc="BusyBox Kompilierung"
    )

    # 5. Installieren von BusyBox in das Rootfs
    run_command(
        commands=["make", f"CONFIG_PREFIX={rootfs_full_path}", "install"], # rootfs_dir als POSIX-String für den Befehl
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
# /etc/init.d/rcS - main initialization script for BusyBox
# Basic mounts
mount -a
# Mount pseudo-filesystems
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev # Use devtmpfs for dynamic device nodes

# Ensure /dev is properly populated by mdev (if configured in BusyBox)
# This will create common device nodes dynamically
echo "Populating /dev with mdev..."
/sbin/mdev -s

echo "Booting finished. Type 'help' for a list of commands."
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
        file_path = rootfs_dir / filename_relative # Korrekte Pfadverknüpfung
        file_path.parent.mkdir(parents=True, exist_ok=True) # Stellen Sie sicher, dass das übergeordnete Verzeichnis existiert
        file_path.write_text(content)
        if filename_relative == "etc/init.d/rcS": # Prüfe den relativen Pfad
            os.chmod(file_path, 0o755) # Ausführbar machen
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
                # os.mknod benötigt Superuser-Rechte (root) für Geräte
                # Wenn das Skript nicht als root läuft, wird dies fehlschlagen.
                # 'mdev -s' (im rcS-Skript) ist der bevorzugte Weg.
                os.mknod(dev_path, mode | (stat.S_IFCHR if type_char == 'c' else stat.S_IFBLK), os.makedev(major, minor))
        except OSError as e:
            # Oft Fehler, wenn nicht root oder Gerät bereits existiert
            print(f"WARNUNG: Konnte Geräteknoten '{name}' nicht erstellen (Typ {type_char}, {major}:{minor}): {e}")
            print("         Dies ist normal, wenn 'mdev -s' (in init-Skript) diese dynamisch erstellt.")
        except Exception as e:
            print(f"Unerwarteter Fehler bei '{name}': {e}")
    print("Statische Geräteknoten-Erstellung abgeschlossen (ggf. dynamisch durch mdev/udev ergänzt).")


def create_root_init_script(rootfs_base_dir: Path):
    """
    Erstellt ein 'init'-Skript direkt im Wurzelverzeichnis des Rootfs.
    DIES IST NICHT DIE STANDARDMETHODE FÜR BUSYBOX-INIT UND WIRD IM ALLGEMEINEN NICHT EMPFOHLEN.
    Der Kernel muss explizit mit 'init=/init' gebootet werden, damit dieses Skript gefunden wird.

    Args:
        rootfs_base_dir: Das Basisverzeichnis des Rootfs (z.B. pathlib.Path("build/rootfs")).
    """
    init_script_path = rootfs_base_dir / "init" # Direkt im Wurzelverzeichnis
    
    print(f"\nWARNUNG: Erstelle ein unkonventionelles 'init'-Skript direkt im Wurzelverzeichnis: {init_script_path}")
    print("         Dies ist NICHT der Standardweg für BusyBox-Init und erfordert spezielle Kernel-Boot-Parameter.")

    # Inhalt des init-Skripts im Wurzelverzeichnis
    # Es muss die grundlegenden Schritte ausführen, die BusyBox init oder /etc/init.d/rcS normalerweise tut.
    init_script_content = """#!/bin/sh
# /init - Das erste Skript, das vom Kernel aufgerufen wird.
# ACHTUNG: Dies ist eine unkonventionelle init-Skript-Platzierung.

echo "Starte init-Skript im Wurzelverzeichnis..."

# Mounten der Pseudo-Dateisysteme
echo "Mounting grundlegende Dateisysteme..."
/bin/mount -t proc proc /proc
/bin/mount -t sysfs sysfs /sys
/bin/mount -t devtmpfs devtmpfs /dev # Wichtig für Gerätedateien

# mdev ausführen, um Geräteknoten dynamisch zu erstellen
# Stellen Sie sicher, dass mdev in Ihrer BusyBox-Konfiguration aktiviert ist!
echo "Populating /dev mit mdev..."
/sbin/mdev -s

# Optional: Grundlegende Netzwerkkonfiguration
# echo "Konfiguriere Loopback-Interface..."
# /sbin/ifconfig lo 127.0.0.1 up

echo "Grundlegende Systeminitialisierung abgeschlossen."
echo "Bitte beachten Sie, dass dieses Skript keine /etc/inittab oder komplexe Runlevel verarbeitet."
echo "Das System ist jetzt in einem minimalen Zustand. Sie können nun Befehle ausführen."

# Starten einer Shell, falls Sie nicht inittab verwenden
# ACHTUNG: Dies startet KEINEN Init-Prozess im Hintergrund.
#          Ein Exit aus dieser Shell würde das System herunterfahren!
/bin/sh
# Oder Sie können ein bestimmtes Programm starten, z.B. eine Anwendung
# /usr/bin/my_application

echo "System wird heruntergefahren, da init-Shell beendet wurde."
/bin/sync
/sbin/poweroff
"""

    try:
        # Kein .parent.mkdir() nötig, da es direkt in rootfs_base_dir liegt,
        # welches bereits von create_dirs() erstellt wurde.
        
        # Schreiben des Skriptinhalts
        init_script_path.write_text(init_script_content)
        
        # Skript ausführbar machen
        os.chmod(init_script_path, 0o755) # rwxr-xr-x
        
        print(f"Init-Skript '{init_script_path}' erfolgreich erstellt und ausführbar gemacht.")
    except Exception as e:
        print(f"Fehler beim Erstellen des Wurzel-Init-Skripts: {e}")
        sys.exit(1)



def set_rootfs_permissions(rootfs_full_path: Path):
    """
    Setzt grundlegende Dateisystemberechtigungen für ein BusyBox-Rootfs.
    """
    print(f"\nSetze grundlegende Berechtigungen für {rootfs_full_path}...")

    # --- Standardberechtigungen für Ordner und Dateien ---
    # Die meisten Ordner: 0o755 (rwxr-xr-x)
    # Die meisten Dateien: 0o644 (rw-r--r--)
    # Ausführbare Dateien/Skripte: 0o755 (rwxr-xr-x)

    # 1. Standardberechtigungen rekursiv setzen
    # Wir werden dies in zwei Schritten tun: zuerst alle Ordner, dann alle Dateien
    
    # Ordnerrekursion: Alle Ordner 0o755
    print("Setze Standard-Ordnerberechtigungen (0o755)...")
    for dirpath, dirnames, filenames in os.walk(rootfs_full_path):
        current_dir_path = pathlib.Path(dirpath)
        try:
            # os.chmod(current_dir_path, 0o755) # Dies könnte spezielle Berechtigungen überschreiben
            # Besser: nur für neu erstellte Ordner, oder wenn sie nicht speziell behandelt werden.
            # Für die meisten Zwecke reicht es, wenn die Erstellungsroutine die Rechte setzt.
            # Hier setzen wir es für alle, außer den unten explizit genannten.
            if current_dir_path not in [
                rootfs_full_path / "tmp",
                rootfs_full_path / "var" / "tmp",
                rootfs_full_path / "var" / "log",
                rootfs_full_path / "var" / "run",
                rootfs_full_path / "var" / "lock",
                rootfs_full_path / "dev"
            ]:
                 os.chmod(current_dir_path, 0o755)
        except Exception as e:
            print(f"WARNUNG: Konnte Berechtigung für Ordner '{current_dir_path}' nicht setzen: {e}")

    print("Setze Standard-Dateiberechtigungen (0o644/0o755)...")
    for dirpath, dirnames, filenames in os.walk(rootfs_full_path):
        for filename in filenames:
            file_path = Path(dirpath) / filename
            try:
                if (file_path.parent.name in ["bin", "sbin", "usr/bin", "usr/sbin"]) or \
                   (file_path.name.endswith(".sh") and file_path.is_file() and b"#!/bin/sh" in file_path.read_bytes()) or \
                   (file_path.name == "rcS" and file_path.parent.name == "init.d"):
                    os.chmod(file_path, 0o755) # Ausführbar
                else:
                    os.chmod(file_path, 0o644) # Nur lesbar für andere, schreibbar für Besitzer
            except Exception as e:
                print(f"WARNUNG: Konnte Berechtigung für Datei '{file_path}' nicht setzen: {e}")

    print("Setze spezielle Verzeichnisberechtigungen...")
    
    special_dirs = [
        rootfs_full_path / "tmp",
        rootfs_full_path / "var" / "tmp"
    ]
    for s_dir in special_dirs:
        if s_dir.exists() and s_dir.is_dir():
            try:
                os.chmod(s_dir, 0o1777) # rwxrwxrwt
                print(f"  - '{s_dir}' auf 0o1777 (Sticky Bit) gesetzt.")
            except Exception as e:
                print(f"WARNUNG: Konnte spezielle Berechtigung für '{s_dir}' nicht setzen: {e}")

    rw_dirs = [
        rootfs_full_path / "var" / "log",
        rootfs_full_path / "var" / "run",
        rootfs_full_path / "var" / "lock"
    ]
    for rw_dir in rw_dirs:
        if rw_dir.exists() and rw_dir.is_dir():
            try:
                os.chmod(rw_dir, 0o777) # rwxrwxrwx
                print(f"  - '{rw_dir}' auf 0o777 gesetzt.")
            except Exception as e:
                print(f"WARNUNG: Konnte spezielle Berechtigung für '{rw_dir}' nicht setzen: {e}")

    dev_dir = rootfs_full_path / "dev"
    if dev_dir.exists() and dev_dir.is_dir():
        try:
            os.chmod(dev_dir, 0o755) # rwxr-xr-x
            print(f"  - '{dev_dir}' auf 0o755 gesetzt.")
        except Exception as e:
            print(f"WARNUNG: Konnte spezielle Berechtigung für '{dev_dir}' nicht setzen: {e}")
    
    if rootfs_full_path.exists() and rootfs_full_path.is_dir():
        try:
            os.chmod(rootfs_full_path, 0o755) # rwxr-xr-x
            print(f"  - Rootfs-Basis '{rootfs_full_path}' auf 0o755 gesetzt.")
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

# --- Globale Pfade und Verzeichnisse ---
build_dir = pathlib.Path("build")
rootfs_dir = build_dir / "rootfs"
rootfs_full_path = rootfs_dir.absolute()

# Pfade basierend auf den geladenen Variablen anpassen
busybox_tar_gz = build_dir / f"busybox-{busybox_version}.tar.bz2"
busybox_src_dir = build_dir / f"busybox-{busybox_version}"
busybox_defconfig_source = pathlib.Path("busybox.defconfig")
firmware_clone_dir = build_dir / "raspberrypi-firmware" # Neues Verzeichnis für den Firmware-Klon

# --- Rootfs-Ordnerstruktur (bereinigt) ---
rootfs_dirs = [
    "bin", "sbin", "etc", "proc", "sys", "dev", "lib", "lib64",
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


def set_rootfs_permissions(rootfs_base_dir: Path):
    """
    Setzt grundlegende Dateisystemberechtigungen für ein BusyBox-Rootfs.
    """
    print(f"\nSetze grundlegende Berechtigungen für {rootfs_base_dir}...")

    print("Setze Standard-Ordnerberechtigungen (0o755)...")
    for dirpath, dirnames, filenames in os.walk(rootfs_base_dir):
        current_dir_path = Path(dirpath)
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
            file_path = Path(dirpath) / filename
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

def create_rootfs_ext4_image(rootfs_base_dir: Path, image_output_path: pathlib.Path, image_size_mb: int = 512):
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
        temp_mount_point = Path(tempfile.mkdtemp(prefix="rootfs_mount_"))
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
        # Bevor wir sys.exit(1) aufrufen, stellen wir sicher, dass wir trotzdem aufräumen.
        # Dies wird im finally-Block behandelt, aber eine explizite Meldung ist hilfreich.
        pass # Weiter zum finally-Block
    finally:
        # 7. Image aushängen (immer versuchen, auch bei Fehlern)
        # ZUERST AUSHÄNGEN!
        if temp_mount_point and temp_mount_point.is_mount(): # Prüfen, ob wirklich gemountet ist
            print(f"Hänge Image von '{temp_mount_point}' aus...")
            try:
                run_command(["umount", str(temp_mount_point)], desc="Image aushängen", check_root=True)
                print("Image erfolgreich ausgehängt.")
            except Exception as e:
                print(f"WARNUNG: Konnte Image von '{temp_mount_point}' nicht aushängen: {e}")
                print("         Bitte manuell überprüfen und ggf. 'sudo umount -l {temp_mount_point}' verwenden.")

        # 8. Temporäres Mount-Verzeichnis bereinigen (immer versuchen)
        # NACH DEM AUSHÄNGEN!
        if temp_mount_point and temp_mount_point.exists():
            print(f"Entferne temporäres Mount-Verzeichnis: {temp_mount_point}")
            try:
                shutil.rmtree(temp_mount_point)
                print("Temporäres Mount-Verzeichnis entfernt.")
            except Exception as e:
                print(f"WARNUNG: Konnte temporäres Verzeichnis '{temp_mount_point}' nicht entfernen: {e}")
                print("         Bitte manuell entfernen.")
        
        # Wenn ein Fehler im try-Block aufgetreten ist und wir hierher gekommen sind,
        # dann muss das Skript immer noch beendet werden, da die Operation nicht abgeschlossen wurde.
        # Nur wenn es keinen Fehler gab, wird der finally-Block normal beendet.
        if 'e' in locals(): # Überprüfen, ob die Exception 'e' in diesem Scope definiert wurde (also ein Fehler auftrat)
             sys.exit(1)


    print(f"--- EXT4-Image '{image_output_path}' erfolgreich erstellt ---")
    
def create_rootfs_ext4_imagex(rootfs_full_path: Path, image_output_path: Path, image_size_mb: int = 512):
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

    if not rootfs_full_path.is_dir():
        print(f"Fehler: Das Rootfs-Verzeichnis '{rootfs_full_path}' existiert nicht oder ist kein Verzeichnis.")
        sys.exit(1)

    temp_mount_point = None
    try:
        # 2. Temporäres Mount-Verzeichnis erstellen
        temp_mount_point = Path(tempfile.mkdtemp(prefix="rootfs_mount_"))
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
        print(f"Kopiere Inhalt von '{rootfs_full_path}' nach '{temp_mount_point}'...")
        run_command(["rsync", "-a", "--info=progress2", f"{rootfs_full_path}/", f"{temp_mount_point.as_posix()}/"], desc="Rootfs in Image kopieren", check_root=True)
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

    except Exception as e:
        print(f"Kritischer Fehler beim Erstellen des BOOTFS-Images: {e}")
        sys.exit(1)
    finally:
        # 8. Image aushängen (immer versuchen, auch bei Fehlern)
        if temp_mount_point and temp_mount_point.is_mount():
            print(f"Hänge Image von '{temp_mount_point}' aus...")
            try:
                run_command(["umount", str(temp_mount_point)], desc="Image aushängen", check_root=True)
                print("Image erfolgreich ausgehängt.")
            except Exception as e:
                print(f"WARNUNG: Konnte Image von '{temp_mount_point}' nicht aushängen: {e}")
                print("         Bitte manuell überprüfen und ggf. 'sudo umount -l {temp_mount_point}' verwenden.")

        # 9. Temporäres Mount-Verzeichnis bereinigen
        if temp_mount_point and temp_mount_point.exists():
            print(f"Entferne temporäres Mount-Verzeichnis: {temp_mount_point}")
            try:
                shutil.rmtree(temp_mount_point)
                print("Temporäres Mount-Verzeichnis entfernt.")
            except Exception as e:
                print(f"WARNUNG: Konnte temporäres Verzeichnis '{temp_mount_point}' nicht entfernen: {e}")
                print("         Bitte manuell entfernen.")

    print(f"--- BOOTFS (FAT32) Image '{output_image_path}' erfolgreich erstellt ---")



# --- Hauptfunktion des Skripts ---
def main():
    print("--- Starte Rootfs-Erstellung für Raspberry Pi 5 (ARM64) ---")
    print(f"Ziel-Rootfs-Pfad: {rootfs_dir}")

    create_dirs()
    download_busybox()
    configure_and_install_busybox()
    create_essential_rootfs_files()
    create_device_nodes()
    create_root_init_script(rootfs_full_path)
    set_rootfs_permissions(rootfs_full_path)



    output_image_path = build_dir / "rootfs.ext4"
    image_size = 512 
    create_rootfs_ext4_image(rootfs_full_path, output_image_path, image_size)

    output_bootfs_image_path = build_dir / "bootfs.vfat"
    bootfs_image_size = 256 
    create_bootfs_vfat_image(output_bootfs_image_path, firmware_clone_dir, bootfs_image_size)


    print("\n--- Rootfs-Erstellungsprozess abgeschlossen ---")
    print(f"Ihr minimales BusyBox-Rootfs befindet sich in: {rootfs_dir}")
    print("\nNächste Schritte (manuell):")
    print("1. Benötigte Bibliotheken (z.B. glibc, falls nicht statisch kompiliert) kopieren.")
    print("   (Wenn CONFIG_STATIC=y in BusyBox gesetzt, ist dies meist nicht nötig.)")
    print("2. Einen Linux-Kernel und Bootloader für den Raspberry Pi 5 herunterladen oder kompilieren.")
    print("3. Ein Dateisystem-Image (z.B. ext4) aus dem Rootfs erstellen (z.B. mit 'dd' und 'mkfs.ext4').")
    print("4. Das Image auf eine SD-Karte flashen.")
    print("\nDenken Sie daran, dass die Erstellung eines wirklich bootfähigen Images komplexer ist und die")
    print("genaue Konfiguration des Kernels und des Bootloaders spezifisch für RPi5 sein muss.")

if __name__ == "__main__":
    main()