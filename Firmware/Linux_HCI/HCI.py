#!/usr/bin/env python3
"""Advertise an OpenHaystack accessory via Apple's Find My network on Linux.

Uses a raw Bluetooth HCI socket (AF_BLUETOOTH, BTPROTO_HCI), so no external
tools are required. The previously used `hcitool` was removed from BlueZ
(>= 5.66) and is not shipped by current Debian releases (12/13) anymore.
Only Python 3 and a Linux kernel with Bluetooth support are needed, which
makes this script suitable for minimal systems such as an LXC container on
Proxmox VE.
"""

import argparse
import base64
import fcntl
import signal
import socket
import struct
import sys
import time

HCI_COMMAND_PKT = 0x01
HCI_EVENT_PKT = 0x04
EVT_CMD_COMPLETE = 0x0E
EVT_CMD_STATUS = 0x0F

OGF_LE = 0x08
OCF_LE_SET_ADV_PARAMS = 0x0006
OCF_LE_SET_ADV_DATA = 0x0008
OCF_LE_SET_ADV_ENABLE = 0x000A
OCF_LE_SET_RANDOM_ADDR = 0x0035

HCIDEVUP = 0x400448C9  # _IOW('H', 201, int)

ADV_TYPE_NONCONN_IND = 0x03
OWN_ADDR_TYPE_RANDOM = 0x01
ADV_CHANNEL_MAP = 0x07  # advertising channels 37, 38, 39

KEY_LEN = 28  # P-224 public key as used in the Find My advertisement
MIN_ADV_INTERVAL = 0x0020  # 20 ms in units of 0.625 ms
MAX_ADV_INTERVAL = 0x4000  # 10.24 s

HCI_STATUS_NAMES = {
    0x00: "success",
    0x01: "unknown HCI command",
    0x0C: "command disallowed",
    0x11: "unsupported feature or parameter value",
    0x1A: "remote user terminated connection",
}


def make_opcode(ogf, ocf):
    return (ogf << 10) | ocf


def command_packet(ogf, ocf, params=b""):
    return bytes([HCI_COMMAND_PKT]) + struct.pack("<HB", make_opcode(ogf, ocf), len(params)) + params


def derive_address(key):
    addr = bytearray(key[:6])
    addr[0] |= 0xC0  # static random address
    return bytes(addr)


def build_advertisement(key):
    adv = bytearray(31)
    adv[0] = 0x1E  # AD structure length
    adv[1] = 0xFF  # manufacturer specific data
    adv[2:4] = b"\x4c\x00"  # company ID (Apple)
    adv[4:6] = b"\x12\x19"  # offline finding type and length
    adv[6] = 0x00  # state
    adv[7:29] = key[6:28]
    adv[29] = key[0] >> 6  # first two bits of key[0]
    adv[30] = 0x00  # hint
    return bytes(adv)


def build_commands(key, interval):
    addr = derive_address(key)
    adv = build_advertisement(key)
    interval_enc = struct.pack("<H", interval)
    adv_params = (
        interval_enc
        + interval_enc
        + bytes([ADV_TYPE_NONCONN_IND, OWN_ADDR_TYPE_RANDOM, 0x00])
        + b"\x00" * 6  # peer address
        + bytes([ADV_CHANNEL_MAP, 0x00])  # channel map, filter policy
    )
    adv_data = bytes([len(adv)]) + adv + b"\x00" * (1 + 31)  # adv data + empty scan response
    return [
        ("LE Set Random Address", OGF_LE, OCF_LE_SET_RANDOM_ADDR, addr),
        ("LE Set Advertising Parameters", OGF_LE, OCF_LE_SET_ADV_PARAMS, adv_params),
        ("LE Set Advertising Data", OGF_LE, OCF_LE_SET_ADV_DATA, adv_data),
        ("LE Set Advertise Enable", OGF_LE, OCF_LE_SET_ADV_ENABLE, b"\x01"),
    ]


def open_hci_socket(dev_id):
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    sock.bind((dev_id,))
    return sock


def bring_adapter_up(sock, dev_id):
    try:
        fcntl.ioctl(sock.fileno(), HCIDEVUP, dev_id)
    except OSError:
        pass  # already up or not permitted; the LE commands below work regardless


def hci_command(sock, ogf, ocf, params=b"", timeout=2.0):
    op = make_opcode(ogf, ocf)
    sock.send(command_packet(ogf, ocf, params))
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timeout waiting for answer to HCI command 0x{op:04x}")
        sock.settimeout(remaining)
        try:
            data = sock.recv(512)
        except socket.timeout:
            raise TimeoutError(f"timeout waiting for answer to HCI command 0x{op:04x}")
        if not data or data[0] != HCI_EVENT_PKT:
            continue
        event = data[1]
        if event == EVT_CMD_STATUS and len(data) >= 6 and struct.unpack("<H", data[4:6])[0] == op:
            if data[3] != 0:
                return data[3]
            continue
        if event == EVT_CMD_COMPLETE and len(data) >= 7 and struct.unpack("<H", data[4:6])[0] == op:
            return data[6]


def status_str(status):
    return f"0x{status:02x} ({HCI_STATUS_NAMES.get(status, 'unknown')})"


def apply_commands(sock, commands):
    for name, ogf, ocf, params in commands:
        status = hci_command(sock, ogf, ocf, params)
        if status != 0:
            raise RuntimeError(f"{name} failed with status {status_str(status)}")
        print(f"{name}: OK")


def dev_id_from_name(name):
    if name.startswith("hci") and name[3:].isdigit():
        return int(name[3:])
    raise argparse.ArgumentTypeError(f"invalid adapter name '{name}' (expected e.g. 'hci0')")


def main(argv):
    parser = argparse.ArgumentParser(
        description="Advertise an OpenHaystack accessory via Apple's Find My network using raw HCI commands."
    )
    parser.add_argument(
        "--key",
        "-k",
        required=True,
        help="Advertisement key (base64, 28 bytes). In the app: right-click an accessory -> "
        "'Copy advertisement key (Base64)'.",
    )
    parser.add_argument("--hci", type=dev_id_from_name, default=0, metavar="hciN",
                        help="Bluetooth adapter to use (default: hci0)")
    parser.add_argument("--interval", type=int, default=2000, metavar="UNITS",
                        help="Advertising interval in units of 0.625 ms, 32-16384 (default: 2000 = 1.25 s)")
    parser.add_argument("--refresh", type=float, default=30, metavar="SECONDS",
                        help="Re-apply the HCI commands every N seconds to survive adapter resets (0 = only once, default: 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only print the HCI packets without touching the adapter")
    args = parser.parse_args(argv)

    try:
        key = base64.b64decode(args.key, validate=True)
    except Exception:
        parser.error("--key is not valid base64")
    if len(key) != KEY_LEN:
        parser.error(f"--key must decode to {KEY_LEN} bytes, got {len(key)}")
    if not MIN_ADV_INTERVAL <= args.interval <= MAX_ADV_INTERVAL:
        parser.error(f"--interval must be between {MIN_ADV_INTERVAL} and {MAX_ADV_INTERVAL} (0.625 ms units)")

    key = bytes(key)
    commands = build_commands(key, args.interval)

    print(f"key     ({len(key):2}) {key.hex()}")
    print(f"address ({len(derive_address(key)):2}) {derive_address(key).hex()}")
    print(f"payload ({len(build_advertisement(key)):2}) {build_advertisement(key).hex()}")

    if args.dry_run:
        for name, ogf, ocf, params in commands:
            print(f"{name}: {command_packet(ogf, ocf, params).hex()}")
        return 0

    try:
        sock = open_hci_socket(args.hci)
    except OSError as e:
        sys.exit(
            f"Cannot open raw HCI socket on hci{args.hci}: {e}\n"
            "Run as root (or with CAP_NET_ADMIN) and make sure a Bluetooth adapter is "
            "present and, in an LXC container, passed through from the Proxmox host."
        )

    bring_adapter_up(sock, args.hci)

    stop = False

    def handle_signal(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while not stop:
            apply_commands(sock, commands)
            print("Advertising. Press Ctrl+C to stop.")
            if args.refresh <= 0:
                break
            deadline = time.monotonic() + args.refresh
            while not stop and time.monotonic() < deadline:
                time.sleep(0.5)
    except (RuntimeError, TimeoutError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            hci_command(sock, OGF_LE, OCF_LE_SET_ADV_ENABLE, b"\x00", timeout=1.0)
        except (OSError, TimeoutError):
            pass
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
