#!/usr/bin/env python3
import time
import board
import busio
import neopixel_spi as neopixel

GPIO10_LED_COUNT = 30
GPIO20_LED_COUNT = 30  # Change if this strip has a different number of LEDs

strip10 = None
strip20 = None

try:
    # GPIO 10 / physical pin 19: SPI0 MOSI
    spi0 = board.SPI()

    # GPIO 20 / physical pin 38: SPI1 MOSI
    spi1 = busio.SPI(
        board.SCK_1,
        MOSI=board.MOSI_1,
        MISO=board.MISO_1,
    )

    strip10 = neopixel.NeoPixel_SPI(
        spi=spi0,
        n=GPIO10_LED_COUNT,
        pixel_order=neopixel.GRB,
        brightness=1.0,
        auto_write=False,
    )

    strip20 = neopixel.NeoPixel_SPI(
        spi=spi1,
        n=GPIO20_LED_COUNT,
        pixel_order=neopixel.GRB,
        brightness=1.0,
        auto_write=False,
    )

    strip10.fill((255, 255, 255))
    strip20.fill((255, 255, 255))
    strip10.show()
    strip20.show()

    print("GPIO 10 and GPIO 20 LED strips ON (white). Press Ctrl+C to switch OFF.")
    while True:
        time.sleep(1)

except KeyboardInterrupt:
    print("\nSwitching both strips OFF...")

finally:
    for strip in (strip10, strip20):
        if strip is not None:
            strip.fill((0, 0, 0))
            strip.show()
