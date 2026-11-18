# OpenHaystack HCI Script for Linux

This script enables Linux devices to send out Bluetooth Low Energy advertisements such that they can be found by [Apple's Find My network](https://developer.apple.com/find-my/).

## Disclaimer

Note that the script is just a proof-of-concept and currently only implements advertising a single static key. This means that **devices running this script are trackable** by other devices in proximity.

## Requirements

- A Linux machine with a Bluetooth Low Energy adapter (kernel Bluetooth support, e.g. the `btusb` module for USB dongles).
- Python 3 (standard library only — no extra packages).
- Root privileges (or `CAP_NET_ADMIN`), because the script talks to the adapter through a raw HCI socket.

The script does **not** need any BlueZ command line tools: `hcitool` was removed from BlueZ (>= 5.66) and is no longer shipped by current Debian releases (12/13). Instead, the script sends the HCI commands (`LE Set Random Address`, `LE Set Advertising Parameters`, `LE Set Advertising Data`, `LE Set Advertise Enable`) directly via a raw `AF_BLUETOOTH`/`BTPROTO_HCI` socket. Installing the `bluez` package is still recommended for debugging (`bluetoothctl`, `btmgmt`, `btmon`).

Tested on Raspberry Pi OS and on Debian 12/13, including inside an unprivileged/privileged LXC container on Proxmox VE (see below).

## Usage

Copy the required `ADVERTISMENT_KEY` from the app by right-clicking on your accessory and selecting _Copy advertisement key (Base64)_. Then run the script:

```bash
sudo python3 HCI.py --key <ADVERTISMENT_KEY>
```

Options:

| Option | Default | Description |
|--------|---------|-------------|
| `--key`, `-k` | (required) | Advertisement key (base64, 28 bytes) |
| `--hci` | `hci0` | Bluetooth adapter to use |
| `--interval` | `2000` | Advertising interval in units of 0.625 ms (2000 = 1.25 s) |
| `--refresh` | `30` | Re-apply the HCI commands every N seconds to survive adapter resets; `0` = configure only once |
| `--dry-run` | off | Only print the HCI packets without touching the adapter |

Verify that advertisements are sent, e.g. with `btmon` (look for the manufacturer specific data starting with `4c 00 12 19`) or by scanning for the accessory's address with `bluetoothctl`.

## Running in an LXC container on Proxmox VE (Debian 12/13)

A convenient way to run a permanent beacon is a Debian LXC on Proxmox VE with a USB Bluetooth dongle passed through.

1. **On the Proxmox host**, identify the dongle and load the driver:
   ```bash
   lsusb                      # note bus and device number, e.g. Bus 001 Device 004
   modprobe btusb
   ```
2. **Create the LXC** from a current Debian template (12/13). A privileged container is the simplest option; unprivileged works with the passthrough rules below (add `lxc.apparmor.profile: unconfined` if AppArmor blocks access to the USB device).
3. **Pass the dongle through** by adding to `/etc/pve/lxc/<VMID>.conf` (adjust bus/device numbers):
   ```
   lxc.cgroup2.device.allow: c 189:* rwm
   lxc.mount.entry: /dev/bus/usb/001/004 dev/bus/usb/001/004 none bind,create=file,optional
   ```
   Alternative without USB passthrough: keep the dongle on the host and move the adapter into the container's network namespace: `ip link set hci0 netns <pid-of-container-init>`. The raw HCI socket does not need a `/dev` node, only the `hci0` interface in the container.
4. **Start the container** and verify the adapter is visible inside:
   ```bash
   ls /sys/class/bluetooth/   # should list hci0
   ```
5. **Install the requirements** inside the container:
   ```bash
   apt update && apt install -y python3 bluez
   ```
6. **Copy the script** into the container, e.g.:
   ```bash
   mkdir -p /opt/openhaystack/Firmware/Linux_HCI
   # copy HCI.py from this repository to /opt/openhaystack/Firmware/Linux_HCI/HCI.py
   ```
7. **Run it manually** (as root):
   ```bash
   python3 /opt/openhaystack/Firmware/Linux_HCI/HCI.py --key <ADVERTISMENT_KEY>
   ```
8. **Or run it as a systemd service** (recommended):
   ```bash
   cp openhaystack-hci.service /etc/systemd/system/
   mkdir -p /etc/openhaystack
   install -m 600 advertisement-key.env.example /etc/openhaystack/advertisement-key.env
   # edit /etc/openhaystack/advertisement-key.env and set ADVERTISEMENT_KEY=<BASE64_KEY>
   systemctl daemon-reload
   systemctl enable --now openhaystack-hci
   systemctl status openhaystack-hci
   ```

The accessory should show its first location report in the OpenHaystack app within ~30 minutes.

## How it works

The script opens a raw HCI socket bound to the adapter (`hci0` by default), brings the adapter up, and then sends four LE commands: it sets a static random address derived from the advertisement key, configures non-connectable advertising on channels 37–39, writes the Find My manufacturer specific payload (Apple company ID `0x004C`, offline finding type `0x12`), and enables advertising. The commands are re-applied periodically (default: every 30 s) so the beacon survives adapter resets, and advertising is disabled again on exit (SIGINT/SIGTERM).
