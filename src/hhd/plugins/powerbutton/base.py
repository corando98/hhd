import logging
import os
import select
import subprocess
from threading import Event
from time import perf_counter, sleep
from typing import cast

import evdev
from evdev import ecodes as e

from hhd.utils import Context, expanduser

from .const import SUPPORTED_DEVICES, PowerButtonConfig

logger = logging.getLogger(__name__)

STEAM_PID = "~/.steam/steam.pid"
STEAM_EXE = "~/.steam/root/ubuntu12_32/steam"
STEAM_WAIT_DELAY = 0.5
LONG_PRESS_DELAY = 2.5


def B(b: str):
    return cast(int, getattr(evdev.ecodes, b))


def is_steam_gamescope_running(ctx: Context):
    pid = None
    try:
        with open(expanduser(STEAM_PID, ctx)) as f:
            pid = f.read().strip()

        steam_cmd_path = f"/proc/{pid}/cmdline"
        if not os.path.exists(steam_cmd_path):
            return False

        # Use this and line to determine if Steam is running in DeckUI mode.
        with open(steam_cmd_path, "rb") as f:
            steam_cmd = f.read()
        is_deck_ui = b"-gamepadui" in steam_cmd
        if not is_deck_ui:
            return False
    except Exception as e:
        return False
    return True


def run_steam_command(command: str, ctx: Context):
    global home_path
    try:
        result = subprocess.run(
            [
                "su",
                ctx.name,
                "-c",
                f"{expanduser(STEAM_EXE, ctx)} -ifrunning {command}",
            ]
        )
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Received error when running steam command `{command}`\n{e}")
    return False


def register_power_button(b: PowerButtonConfig) -> evdev.InputDevice | None:
    for device in [evdev.InputDevice(path) for path in evdev.list_devices()]:
        if str(device.phys).startswith(b.phys):
            device.grab()
            logger.info(f"Captured power button '{device.name}': '{device.phys}'")
            return device
    return None


def register_hold_button(b: PowerButtonConfig) -> evdev.InputDevice | None:
    if not b.hold_phys or not b.hold_events or b.hold_grab is None:
        logger.error(
            f"Device configuration tuple does not contain required parameters:\n{b}"
        )
        return None

    for device in [evdev.InputDevice(path) for path in evdev.list_devices()]:
        if str(device.phys).startswith(b.hold_phys):
            if b.hold_grab:
                device.grab()
            logger.info(f"Captured hold keyboard '{device.name}': '{device.phys}'")
            return device
    return None


def get_config() -> PowerButtonConfig | None:
    with open("/sys/devices/virtual/dmi/id/product_name") as f:
        prod = f.read().strip()

    for d in SUPPORTED_DEVICES:
        if d.prod_name == prod:
            return d

    return None


def run_steam_shortpress(perms: Context):
    return run_steam_command("steam://shortpowerpress", perms)


def run_steam_longpress(perms: Context):
    return run_steam_command("steam://longpowerpress", perms)


def power_button_run(cfg: PowerButtonConfig, ctx: Context, should_exit: Event):
    match cfg.type:
        case "hold_emitted":
            logger.info(
                f"Starting timer based powerbutton handler for device '{cfg.device}'."
            )
            power_button_timer(cfg, ctx, should_exit)
        case "hold_isa":
            logger.info(
                f"Starting isa keyboard powerbutton handler for device '{cfg.device}'."
            )
            power_button_isa(cfg, ctx, should_exit)
        case _:
            logger.error(f"Invalid type in config '{cfg.type}'. Exiting.")


def power_button_isa(cfg: PowerButtonConfig, perms: Context, should_exit: Event):
    if not cfg.hold_events:
        logger.error(f"Invalid hold events in config. Exiting.\n:{cfg.hold_events}")
        return

    press_dev = None
    hold_dev = None
    try:
        hold_state = 0
        while not should_exit.is_set():
            # Initial check for steam
            if not is_steam_gamescope_running(perms):
                # Close devices
                if press_dev:
                    press_dev.close()
                    press_dev = None
                if hold_dev:
                    hold_dev.close()
                    hold_dev = None
                logger.info(f"Waiting for steam to launch.")
                while not is_steam_gamescope_running(perms):
                    if should_exit.is_set():
                        return
                    sleep(STEAM_WAIT_DELAY)

            if not press_dev or not hold_dev:
                logger.info(f"Steam is running, hooking power button.")
                press_dev = register_power_button(cfg)
                hold_dev = register_hold_button(cfg)
            if not press_dev or not hold_dev:
                logger.error(f"Power button interfaces not found, disabling plugin.")
                return

            # Add timeout to release the button if steam exits.
            r = select.select([press_dev.fd, hold_dev.fd], [], [], STEAM_WAIT_DELAY)[0]
            
            if not r:
                continue
            fd = r[0]  # handle one button at a time

            # Handle button event
            issue_systemctl = False
            if fd == press_dev.fd:
                ev = press_dev.read_one()
                if ev.type == B("EV_KEY") and ev.code == B("KEY_POWER") and ev.value:
                    logger.info("Executing short press.")
                    issue_systemctl = not run_steam_shortpress(perms)
            elif fd == hold_dev.fd:
                ev = hold_dev.read_one()
                chk = (ev.type, ev.code, ev.value)

                if hold_state >= len(cfg.hold_events):
                    hold_state = 0

                if chk == cfg.hold_events[hold_state]:
                    hold_state += 1
                else:
                    hold_state = 0

                if hold_state == len(cfg.hold_events):
                    hold_state = 0
                    logger.info("Executing long press.")
                    issue_systemctl = not run_steam_longpress(perms)

            if issue_systemctl:
                logger.error(
                    "Power button action did not work. Calling `systemctl suspend`"
                )
                os.system("systemctl suspend")
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"Received exception, exitting:\n{e}")


def power_button_timer(cfg: PowerButtonConfig, perms: Context, should_exit: Event):
    dev = None
    try:
        pressed_time = None
        while not should_exit.is_set():
            # Initial check for steam
            if not is_steam_gamescope_running(perms):
                # Close devices
                if dev:
                    dev.close()
                    dev = None
                logger.info(f"Waiting for steam to launch.")
                while not is_steam_gamescope_running(perms):
                    sleep(STEAM_WAIT_DELAY)

            if not dev:
                logger.info(f"Steam is running, hooking power button.")
                dev = register_power_button(cfg)
            if not dev:
                logger.error(f"Power button not found, disabling plugin.")
                return

            # Add timeout to release the button if steam exits.
            delay = LONG_PRESS_DELAY if pressed_time else STEAM_WAIT_DELAY
            r = select.select([dev.fd], [], [], delay)[0]

            # Handle press logic
            if r:
                # Handle button event
                ev = dev.read_one()
                if ev.type == B("EV_KEY") and ev.code == B("KEY_POWER"):
                    curr_time = perf_counter()
                    if ev.value:
                        pressed_time = curr_time
                        press_type = "initial_press"
                    elif pressed_time:
                        if curr_time - pressed_time > LONG_PRESS_DELAY:
                            press_type = "long_press"
                        else:
                            press_type = "short_press"
                        pressed_time = None
                    else:
                        press_type = "release_without_press"
                else:
                    press_type = "no_press"
            elif pressed_time:
                # Button was pressed but we hit a timeout, that means
                # it is a long press
                press_type = "long_press"
            else:
                # Otherwise, no press
                press_type = "no_press"

            issue_systemctl = False
            match press_type:
                case "long_press":
                    logger.info("Executing long press.")
                    issue_systemctl = not run_steam_longpress(perms)
                case "short_press":
                    logger.info("Executing short press.")
                    issue_systemctl = not run_steam_shortpress(perms)
                case "initial_press":
                    logger.info("Power button pressed down.")
                case "release_without_press":
                    logger.error("Button released without being pressed.")

            if issue_systemctl:
                logger.error(
                    "Power button action did not work. Calling `systemctl suspend`"
                )
                os.system("systemctl suspend")
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"Received exception, exitting:\n{e}")
    finally:
        if dev:
            dev.close()
