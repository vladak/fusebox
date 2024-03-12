# SPDX-FileCopyrightText: 2022 Vladimír Kotal
# SPDX-License-Identifier: Unlicense
"""
Acquire pulse count and optionally also data from various temperature/humidity sensors,
publish it to MQTT topic.

This is meant to be run on Adafruit device powered from stable source (e.g. USB).
"""
import json
import time
import traceback

import adafruit_logging as logging
import board

# pylint: disable=import-error
import countio
import microcontroller
import neopixel

# pylint: disable=import-error
import socketpool

# pylint: disable=import-error
import supervisor

try:
    import wifi
except MemoryError as e:
    # Let this fall through to main() so that appropriate reset can be performed.
    IMPORT_EXCEPTION = e

# from digitalio import DigitalInOut
# pylint: disable=no-name-in-module
from microcontroller import watchdog
from watchdog import WatchDogMode, WatchDogTimeout

from logutil import get_log_level
from mqtt import mqtt_client_setup
from mqtt_handler import MQTTHandler
from sensors import get_measurements

try:
    from secrets import secrets
except ImportError:
    print(
        "WiFi credentials and configuration are kept in secrets.py, please add them there!"
    )
    raise

# Estimated run time in seconds with some extra room.
# This is used to compute the watchdog timeout.
ESTIMATED_RUN_TIME = 60

# For storing import exceptions so that they can be raised from main().
IMPORT_EXCEPTION = None


def blink(pixel, timeout=0.5):
    """
    Blink the Neo pixel blue.
    """

    logger = logging.getLogger(__name__)
    logger.info("Blinking the neopixel")

    pixel.brightness = 0.3
    pixel.fill((0, 0, 255))
    time.sleep(timeout)
    pixel.fill(0)


def main():
    """
    Collect sensor readings and publish to MQTT topic in endless loop.
    """

    log_level = get_log_level(secrets["log_level"])
    logger = logging.getLogger(__name__)
    logger.setLevel(log_level)

    logger.info("Running")

    if IMPORT_EXCEPTION:
        raise IMPORT_EXCEPTION

    watchdog.timeout = ESTIMATED_RUN_TIME
    watchdog.mode = WatchDogMode.RAISE

    sleep_duration = secrets["sleep_duration"]

    # Create sensor objects, using the board's default I2C bus.
    i2c = board.I2C()
    pin_counter = countio.Counter(board.D37, edge=countio.Edge.RISE)
    pixel = neopixel.NeoPixel(board.NEOPIXEL, 1)

    # Connect to Wi-Fi
    logger.info("Connecting to wifi")
    wifi.radio.connect(secrets["ssid"], secrets["password"], timeout=10)
    logger.info(f"Connected to {secrets['ssid']}")
    logger.debug(f"IP: {wifi.radio.ipv4_address}")

    # Create a socket pool
    pool = socketpool.SocketPool(wifi.radio)  # pylint: disable=no-member

    mqtt_client = mqtt_client_setup(pool, secrets["broker"], secrets["broker_port"])

    logger.info(f"Attempting to connect to MQTT broker {mqtt_client.broker}")
    mqtt_client.connect()
    if secrets.get("log_topic"):
        # Log both to the console as well as via MQTT messages.
        # Up to now the logger was using the default (built-in) handler,
        # now it is necessary to add the Stream handler explicitly as
        # with a non-default handler set only the non-default handlers will be used.
        logger.addHandler(logging.StreamHandler())
        logger.addHandler(MQTTHandler(mqtt_client, secrets["log_topic"]))

    while True:
        humidity, temperature = get_measurements(i2c)

        data = {}

        # It is assumed that the counter will wrap around as it reaches some value.
        # The data collection service (e.g. Prometheus) should be able to deal with that,
        # being aware that it is working with a counter.
        # If the counter was reset() here on every iteration, that would probably confuse
        # the data collection service.
        if pin_counter.count < 0:
            # Resetting the counter once it wraps to negative value might lose
            # some pulses, however it is undesirable to hard code a maximum value either.
            logger.info("Counter is negative, resetting")
            pin_counter.reset()

        count = pin_counter.count
        logger.info(f"Got pulse count: {count}")
        data["pulses"] = count

        fill_data_dict(data, humidity, temperature)

        if len(data) > 0:
            mqtt_topic = secrets["mqtt_topic"]
            logger.info(f"Publishing to {mqtt_topic}")
            mqtt_client.publish(mqtt_topic, json.dumps(data))

        logger.info("Feeding the watchdog")
        watchdog.feed()

        # Blink the LED.
        blink(pixel)

        logger.info(f"Entering sleep for {sleep_duration} seconds")
        time.sleep(sleep_duration)


def fill_data_dict(data, humidity, temperature):
    """
    Put the metrics into dictionary.
    """

    logger = logging.getLogger(__name__)

    if temperature:
        logger.info(f"Temperature: {temperature:.1f} C")
        data["temperature"] = f"{temperature:.1f}"
    if humidity:
        logger.info(f"Humidity: {humidity:.1f} %%")
        data["humidity"] = f"{humidity:.1f}"

    logger.debug(f"data: {data}")


def hard_reset(exception):
    """
    Sometimes soft reset is not enough. Perform hard reset.
    """
    watchdog.mode = None
    print(f"Got exception: {exception}")
    reset_time = 15
    print(f"Performing hard reset in {reset_time} seconds")
    time.sleep(reset_time)
    microcontroller.reset()  # pylint: disable=no-member


try:
    main()
except ConnectionError as e:
    # When this happens, it usually means that the microcontroller's wifi/networking is botched.
    # The only way to recover is to perform hard reset.
    hard_reset(e)
except MemoryError as e:
    # This is usually the case of delayed exception from the 'import wifi' statement,
    # possibly caused by a bug (resource leak) in CircuitPython that manifests
    # after a sequence of ConnectionError exceptions thrown from withing the wifi module.
    # Should not happen given the above 'except ConnectionError',
    # however adding that here just in case.
    hard_reset(e)
except Exception as e:  # pylint: disable=broad-except
    # This assumes that such exceptions are quite rare.
    # Otherwise, this would drain the battery quickly by restarting
    # over and over in a quick succession.
    watchdog.mode = None
    print("Code stopped by unhandled exception:")
    print(traceback.format_exception(None, e, e.__traceback__))
    RELOAD_TIME = 10
    print(f"Performing a supervisor reload in {RELOAD_TIME} seconds")
    time.sleep(RELOAD_TIME)
    supervisor.reload()
except WatchDogTimeout as e:
    hard_reset(e)
