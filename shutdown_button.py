#!/usr/bin/env python3
"""
O-Health Kiosk Hardware Shutdown Button
Wiring: Physical Pin 39 (GND) and Physical Pin 40 (GPIO 21)
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
