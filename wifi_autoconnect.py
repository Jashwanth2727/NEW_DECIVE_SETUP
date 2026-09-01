#!/usr/bin/env python3

import logging
import subprocess
import time

SSID = "OHealth"
PASSWORD = "123456789"
INTERFACE = "wlan0"
LOG_FILE = "/home/rpi13/Kiosk/wifi_autoconnect.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)


def current_connection():
    result = subprocess.run(
        [
            "nmcli",
            "-g",
            "GENERAL.CONNECTION",
            "device",
            "show",
            INTERFACE
        ],
        capture_output=True,
        text=True
    )
    return result.stdout.strip()


def connect_to_wifi():
    logging.info("Trying to connect to %s", SSID)

    result = subprocess.run(
        [
            "nmcli",
            "device",
            "wifi",
            "connect",
            SSID,
            "password",
            PASSWORD,
            "ifname",
            INTERFACE
        ],
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
            logging.info(
                "Current connection: '%s'. %s is not active.",
                connection,
                SSID
            )
            connect_to_wifi()

        time.sleep(30)


if __name__ == "__main__":
    main()
