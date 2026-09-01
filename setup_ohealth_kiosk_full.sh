#!/usr/bin/env bash
###############################################################################
# O-Health Kiosk — One-Shot Provisioning Script (Full)
# Usage:
#   sudo ./setup_ohealth_kiosk_full.sh <rpi_number>
# Example:
#   sudo ./setup_ohealth_kiosk_full.sh 21
#
# This script provisions:
#   - System deps, Python packages (bleak, Adafruit, ws281x, gpiozero)
#   - SPI enable for WS281x LEDs
#   - Bluetooth persistence across reboot
#   - Git clone of kiosk firmware repo
#   - WiFi auto-connect to "OHealth" (wifi.service)
#   - Hardware shutdown button on GPIO 21 (kiosk-shutdown.service)
#   - Main kiosk vitals loop (ohealth-kiosk.service)
###############################################################################
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root: sudo $0 <rpi_number>" >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "Usage: sudo $0 <rpi_number>   (e.g. sudo $0 21 for rpi21)" >&2
  exit 1
fi

RPI_NO="$1"
RPI_USER="rpi${RPI_NO}"
HOME_DIR="/home/${RPI_USER}"
KIOSK_DIR="${HOME_DIR}/Kiosk"
REPO_URL="https://github.com/Jashwanth2727/Embedded-systems.git"

echo "==> Provisioning O-Health Kiosk for host rpi${RPI_NO} (user: ${RPI_USER})"

### 1. System update + dependencies ##########################################
echo "==> [1/9] apt update & core dependencies"
apt update
apt install -y bluetooth bluez libbluetooth-dev python3-pip avahi-daemon git network-manager
systemctl enable --now bluetooth avahi-daemon NetworkManager

### 2. Python packages ########################################################
echo "==> [2/9] Installing Python packages"
pip3 install --break-system-packages \
  bleak \
  adafruit-circuitpython-mlx90632 \
  adafruit-blinka \
  rpi_ws281x \
  gpiozero

### 3. Enable SPI #############################################################
echo "==> [3/9] Enabling SPI interface for WS281x LED support"
BOOT_CONFIG="/boot/firmware/config.txt"
[[ -f "$BOOT_CONFIG" ]] || BOOT_CONFIG="/boot/config.txt"

if [[ -f "$BOOT_CONFIG" ]]; then
  if ! grep -q "^dtparam=spi=on" "$BOOT_CONFIG"; then
    echo "dtparam=spi=on" >> "$BOOT_CONFIG"
    echo "    Added dtparam=spi=on to $BOOT_CONFIG"
  else
    echo "    SPI already enabled in $BOOT_CONFIG"
  fi
else
  echo "    WARNING: could not find config.txt to enable SPI" >&2
fi

if ! grep -q "^spi-dev" /etc/modules 2>/dev/null; then
  echo "spi-dev" >> /etc/modules
fi

### 4. Bluetooth persistence ##################################################
echo "==> [4/9] Bluetooth: unblock, bring up hci0, persist across reboot"
rfkill unblock bluetooth || true
hciconfig hci0 up || true
systemctl mask systemd-rfkill.service systemd-rfkill.socket

cat > /etc/systemd/system/bt-up.service <<'EOF'
[Unit]
Description=Bring up Bluetooth adapter on boot
After=bluetooth.service
Wants=bluetooth.service

[Service]
Type=oneshot
ExecStart=/usr/sbin/rfkill unblock bluetooth
ExecStart=/bin/hciconfig hci0 up
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable bt-up.service
systemctl start bt-up.service

### 5. Clone or preserve kiosk firmware ######################################
echo "==> [5/9] Preparing kiosk firmware in ${KIOSK_DIR}"
mkdir -p "${KIOSK_DIR}"
chown "${RPI_USER}:${RPI_USER}" "${KIOSK_DIR}"

if [[ -d "${KIOSK_DIR}/.git" ]]; then
  echo "    Existing git repo found — pulling latest"
  sudo -u "${RPI_USER}" git -C "${KIOSK_DIR}" pull --ff-only
elif [[ -n "$(ls -A "${KIOSK_DIR}" 2>/dev/null)" ]]; then
  echo "    ${KIOSK_DIR} already contains files and is not a git repo — leaving contents untouched"
else
  sudo -u "${RPI_USER}" git clone "${REPO_URL}" "${KIOSK_DIR}"
fi

### 6. WiFi Auto-Connect Setup ################################################
echo "==> [6/9] Setting up WiFi auto-connect to 'OHealth'"
cat > "${KIOSK_DIR}/wifi_autoconnect.py" <<PYEOF
#!/usr/bin/env python3
import logging
import subprocess
import time

SSID = "OHealth"
PASSWORD = "123456789"
INTERFACE = "wlan0"
LOG_FILE = "/home/rpi${RPI_NO}/Kiosk/wifi_autoconnect.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def current_connection():
    result = subprocess.run(
        ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", INTERFACE],
        capture_output=True,
        text=True
    )
    return result.stdout.strip()

def connect_to_wifi():
    logging.info("Trying to connect to %s", SSID)
    result = subprocess.run(
        ["nmcli", "device", "wifi", "connect", SSID, "password", PASSWORD, "ifname", INTERFACE],
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
        logging.info("Connected successfully to %s", SSID)
    else:
        logging.error("Connection failed: %s", result.stderr.strip())

def main():
    logging.info("wifi_autoconnect.py started")
    while True:
        connection = current_connection()
        if connection == SSID:
            logging.info("Already connected to %s", SSID)
        else:
            logging.info("Current connection: '%s'. %s is not active.", connection, SSID)
            connect_to_wifi()
        time.sleep(30)

if __name__ == "__main__":
    main()
PYEOF

chmod +x "${KIOSK_DIR}/wifi_autoconnect.py"
chown "${RPI_USER}:${RPI_USER}" "${KIOSK_DIR}/wifi_autoconnect.py"

cat > /etc/systemd/system/wifi.service <<WIFIEOF
[Unit]
Description=O-Health WiFi Auto Connection
After=NetworkManager.service network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${KIOSK_DIR}
ExecStart=/usr/bin/python3 -u ${KIOSK_DIR}/wifi_autoconnect.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
WIFIEOF

systemctl daemon-reload
systemctl enable wifi.service
systemctl start wifi.service

### 7. Hardware Shutdown Button (GPIO 21) #####################################
echo "==> [7/9] Setting up hardware shutdown button on GPIO 21 (Pin 40)"
cat > "${KIOSK_DIR}/shutdown_button.py" <<'SHUTEOF'
#!/usr/bin/env python3
"""
O-Health Kiosk Hardware Shutdown Button
Wiring: Physical Pin 39 (GND) and Physical Pin 40 (GPIO 21 / BCM 21)
"""
from gpiozero import Button
import subprocess
import time

SHUTDOWN_GPIO_PIN = 21

def safe_shutdown():
    print("[!] Shutdown button held for 2 seconds. Safely halting the Raspberry Pi...")
    subprocess.run(["sudo", "shutdown", "-h", "now"])

print(f"[*] Initializing Shutdown Button on GPIO {SHUTDOWN_GPIO_PIN} (Pin 40)")

kiosk_button = Button(SHUTDOWN_GPIO_PIN, pull_up=True, hold_time=2.0)
kiosk_button.when_held = safe_shutdown

print("[*] Shutdown listener active in the background. Waiting for input...")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\n[+] Shutdown listener terminated.")
SHUTEOF

chmod +x "${KIOSK_DIR}/shutdown_button.py"
chown "${RPI_USER}:${RPI_USER}" "${KIOSK_DIR}/shutdown_button.py"

cat > /etc/systemd/system/kiosk-shutdown.service <<SHUTEOF
[Unit]
Description=O-Health Kiosk Hardware Shutdown Button
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 ${KIOSK_DIR}/shutdown_button.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SHUTEOF

systemctl daemon-reload
systemctl enable kiosk-shutdown.service
systemctl start kiosk-shutdown.service

### 8. Install kiosk service ##################################################
echo "==> [8/9] Installing ohealth-kiosk.service"
cat > /etc/systemd/system/ohealth-kiosk.service <<EOF
[Unit]
Description=O-Health Kiosk Vitals Capture Loop
After=bluetooth.service network-online.target
Wants=network-online.target bluetooth.service

[Service]
Type=simple
User=root
WorkingDirectory=${KIOSK_DIR}
Environment="PYTHONUNBUFFERED=1"
ExecStartPre=/bin/sleep 15
ExecStart=/usr/bin/python3 ${KIOSK_DIR}/kiosk_integrate.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ohealth-kiosk.service
systemctl restart ohealth-kiosk.service

### 9. Verify ##################################################################
echo "==> [9/9] Verification"
echo "---- Bluetooth ----"
hciconfig -a | grep -E "hci0|UP|DOWN" || true
echo "---- SPI devices (may need reboot) ----"
ls /dev/spi* 2>/dev/null || echo "    /dev/spi* not present yet — reboot required"
echo "---- WiFi service status ----"
systemctl --no-pager status wifi.service || true
echo "---- Shutdown button service status ----"
systemctl --no-pager status kiosk-shutdown.service || true
echo "---- Kiosk service status ----"
systemctl --no-pager status ohealth-kiosk.service || true

echo ""
echo "=============================================================="
echo " Setup complete for rpi${RPI_NO}."
echo " Reboot if SPI was newly enabled: sudo reboot"
echo " After reboot, verify with:"
echo "   hciconfig -a | grep \"UP RUNNING\""
echo "   ls /dev/spi*"
echo "   nmcli connection show --active"
echo "   sudo systemctl status wifi.service kiosk-shutdown.service ohealth-kiosk.service"
echo "   sudo journalctl -u wifi.service -f"
echo "=============================================================="
