"""Offline firmware inventory for RTL8851BU userspace driver.

The add-on includes Alpine linux-firmware-rtw89; this module never flashes or
executes firmware. The header is parsed according to Linux rtw89/fw.h.
"""
import hashlib
import struct
import subprocess
from pathlib import Path

FIRMWARE_NAMES = ("rtw8851b_fw.bin", "rtw8851b_fw-1.bin")
FIRMWARE_DIRS = (Path("/lib/firmware/rtw89"), Path("/usr/lib/firmware/rtw89"))
MAX_IMAGE_SIZE = 8 * 1024 * 1024


def parse_header(data):
    """Validate rtw89 firmware header shape; not the authenticity of firmware."""
    if not isinstance(data, bytes) or len(data) < 48:
        raise ValueError("Firmware file too short for rtw89 header")
    words = struct.unpack_from("<12I", data)
    header_version = (words[3] >> 24) & 0xff
    if header_version not in (0, 1):
        raise ValueError("Unsupported rtw89 firmware header format: %d" % header_version)
    section_count = (words[6] >> 8) & 0xff
    if not 1 <= section_count <= 10:
        raise ValueError("Invalid rtw89 firmware section count: %d" % section_count)
    header_size = (48 if header_version == 1 else 32) + section_count * 16
    if len(data) < header_size:
        raise ValueError("Firmware truncated before section descriptors")
    ver = words[1]
    result = {
        "header_version": header_version,
        "firmware_version": ".".join(str((ver >> shift) & 0xff) for shift in (0, 8, 16, 24)),
        "commit_id": "0x%08x" % words[2],
        "section_count": section_count,
        "header_min_bytes": header_size,
        "header_shape_valid": True,
    }
    return result


def read_image(path):
    """Read only packaged firmware. Limit source and expanded file size."""
    if path.stat().st_size > MAX_IMAGE_SIZE:
        raise ValueError("Firmware source too large")
    if path.suffix == ".zst":
        try:
            res = subprocess.run(["zstd", "-dc", str(path)], capture_output=True,
                                 timeout=15, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise ValueError("zstd decompression unavailable: " + str(exc))
        if res.returncode:
            raise ValueError("zstd returned error: " + res.stderr.decode(errors="replace")[-300:])
        data = res.stdout
    else:
        data = path.read_bytes()
    if not 48 <= len(data) <= MAX_IMAGE_SIZE:
        raise ValueError("Unexpected expanded firmware size: %d" % len(data))
    return data


def inventory():
    """Return metadata only. No register writes, USB transfers or firmware upload."""
    found = []
    for directory in FIRMWARE_DIRS:
        for name in FIRMWARE_NAMES:
            for suffix in ("", ".zst"):
                path = directory / (name + suffix)
                if not path.is_file():
                    continue
                entry = {"name": name, "path": str(path), "compressed": suffix == ".zst"}
                try:
                    data = read_image(path)
                    entry.update(parse_header(data))
                    entry.update({"size": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest(),
                                  "prepared": True})
                except (OSError, ValueError) as exc:
                    entry.update({"prepared": False, "error": str(exc)})
                found.append(entry)
    unique = list({entry["path"]: entry for entry in found}.values())
    ready = any(x.get("prepared") for x in unique)
    return {
        "stage": "firmware_inventory",
        "chip": "RTL8851BU",
        "firmware_candidates": unique,
        "firmware_available": ready,
        "firmware_uploaded_to_device": False,
        "ap_ready": False,
        "message": ("Файл прошивки найден и распознан. Драйвер ещё не загружает его в чип."
                    if ready else
                    "Подходящая firmware не распознана. Проверь пакет linux-firmware-rtw89."),
    }
