#!/usr/bin/env python3
"""
Breathing green LED test for rpi17

Strip A: GPIO 18, DMA 5, 6 LEDs (BP, MIC, SpO2)
Strip B: GPIO 13, DMA 6, channel 1, 4 LEDs (Logo, Gluc)

LEDs continuously breathe between OFF and 20% green.
"""

import time
from rpi_ws281x import PixelStrip, ws

# -----------------------------
# Configuration
# -----------------------------
NUM_A = 6
NUM_B = 4

GPIO_A = 18
GPIO_B = 13

DMA_A = 5
DMA_B = 6

MAX_BRIGHTNESS = 51   # 20% of 255
BREATH_TIME = 2.0      # seconds for one complete fade cycle
FPS = 50               # smoothness

print("Initializing strip A (GPIO 18)...")
a = PixelStrip(
    NUM_A,
    GPIO_A,
    800000,
    DMA_A,
    False,
    255,
    0,
    ws.WS2811_STRIP_GRB
)
a.begin()

print("Initializing strip B (GPIO 13, channel 1)...")
b = PixelStrip(
    NUM_B,
    GPIO_B,
    800000,
    DMA_B,
    False,
    255,
    1,
    ws.WS2811_STRIP_GRB
)
b.begin()


def set_green(brightness):
    """Set all LEDs to green at the requested brightness."""

    brightness = int(max(0, min(MAX_BRIGHTNESS, brightness)))

    green = (0 << 16) | (brightness << 8) | 0

    for i in range(NUM_A):
        a.setPixelColor(i, green)

    for i in range(NUM_B):
        b.setPixelColor(i, green)

    a.show()
    b.show()


# Start OFF
print("All LEDs OFF...")
set_green(0)

time.sleep(1)

print("Starting breathing green LED effect...")
print("Press Ctrl+C to stop.")

try:
    start_time = time.time()

    while True:
        elapsed = time.time() - start_time

        # Smooth sine-wave breathing:
        # 0 -> MAX -> 0
        phase = (elapsed % BREATH_TIME) / BREATH_TIME

        import math
        brightness = (
            (1 - math.cos(phase * 2 * math.pi)) / 2
        ) * MAX_BRIGHTNESS

        set_green(brightness)

        time.sleep(1 / FPS)

except KeyboardInterrupt:
    print("\nStopping...")

finally:
    print("All LEDs OFF.")

    for i in range(NUM_A):
        a.setPixelColor(i, 0)

    for i in range(NUM_B):
        b.setPixelColor(i, 0)

    a.show()
    b.show()

    print("Done.")
