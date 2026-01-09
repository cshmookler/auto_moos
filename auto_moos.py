#!/usr/bin/python

"""Automatically installs MOOS"""

import atexit
import curses
import json
import os
import re
import shutil
import subprocess
from argparse import Action, ArgumentParser, Namespace
from dataclasses import dataclass, fields
from enum import auto, Enum, IntEnum
from queue import Empty, Queue
from signal import SIGINT, signal, SIGTERM
from time import sleep
from typing import Any, Callable, Dict, List, Optional, Tuple


# Global paths.
this_dir = os.path.dirname(__file__)
home_dir = os.path.expanduser("~")


# Subprocess and filesystem utilities
# ----------------------------------------------------------------------------


def run(
    *args,
    input: str | None = None,
    quiet: bool = False,
    env: Dict[str, str] | None = None,
) -> bool:
    return (
        subprocess.run(
            args,
            capture_output=quiet,
            env=env,
            input=input,
            text=True if input else None,
        ).returncode
        == 0
    )


def get(*args, quiet: bool = False) -> str | None:
    result = subprocess.run(args, capture_output=True)
    if result.returncode == 0:
        return result.stdout.decode().strip()
    return None


def write(path: str, mode: str, text: str) -> bool:
    try:
        with open(path, mode) as file:
            file.write(text)
    except:
        return False
    return True


def copy(src: str, dst: str, quiet: bool = False) -> bool:
    return run("cp", "-r", src, dst)


def remove(path: str, quiet: bool = False) -> bool:
    return run("rm", "-rf", path)


def make_absolute(path: str) -> str:
    if os.path.isabs(path):
        return path
    else:
        return this_dir + "/" + path


# Message reporting and processing utilities
# ----------------------------------------------------------------------------


class Level(IntEnum):
    normal = auto()
    success = auto()
    error = auto()
    warning = auto()
    info = auto()
    verbose = auto()


@dataclass
class Message:
    raw: str
    level: Level


class Logger:
    def __init__(self, level: Level) -> None:
        self.level: Level = level
        self.cache: list[Message] = list()
        self.log_file = "/dev/null"

    def set_log_file(self, path: str) -> None:
        self.log_file = path

    def clear_log_file(self) -> None:
        with open(self.log_file, "w") as file:
            pass

    def set_level(self, level: Level) -> None:
        self.level = level

    def _log(self, msg: str, level: Level) -> None:
        self.cache.append(Message(msg, level))
        with open(self.log_file, "a") as file:
            file.write(msg + "\n")

    def normal(self, msg: str) -> None:
        self._log(msg, Level.normal)

    def success(self, msg: str) -> None:
        self._log(msg, Level.success)

    def error(self, msg: str) -> None:
        self._log("  [Error] " + msg + ".", Level.error)

    def warning(self, msg: str) -> None:
        self._log("[Warning] " + msg + ".", Level.warning)

    def info(self, msg: str) -> None:
        self._log("   [Info] " + msg + ".", Level.info)

    def verbose(self, msg: str) -> None:
        self._log("[Verbose] " + msg + ".", Level.verbose)

    @staticmethod
    def _green(msg: str) -> str:
        return "\033[1;32m" + msg + "\033[0m"

    @staticmethod
    def _red(msg: str) -> str:
        return "\033[1;31m" + msg + "\033[0m"

    @staticmethod
    def _yellow(msg: str) -> str:
        return "\033[1;33m" + msg + "\033[0m"

    @staticmethod
    def _blue(msg: str) -> str:
        return "\033[1;34m" + msg + "\033[0m"

    def dump_cache(
        self, color_setter: Callable[[Level], None], writer: Callable[[str], None]
    ) -> None:
        for msg in self.cache:
            if msg.level > self.level:
                break

            color_setter(msg.level)
            writer(msg.raw + "\n")
            color_setter(Level.normal)

        self.cache = list()

    def print_cache(self) -> None:
        for msg in self.cache:
            if msg.level > self.level:
                break

            if msg.level == Level.normal:
                print(msg.raw)
            elif msg.level == Level.success:
                print(Logger._green(msg.raw))
            elif msg.level == Level.error:
                print(Logger._red(msg.raw))
            elif msg.level == Level.warning:
                print(Logger._yellow(msg.raw))
            elif msg.level == Level.info:
                print(Logger._blue(msg.raw))
            elif msg.level == Level.verbose:
                print(msg.raw)
            else:
                print(msg.raw)

        self.cache = list()


# The global logger object.
logger = Logger(Level.verbose)


# ----------------------------------------------------------------------------


def list_all_devices() -> Optional[List[str]]:
    devices = get(
        "lsblk",
        "--noheadings",
        "--nodeps",
        "--output",
        "path",
        quiet=True,
    )
    if not devices:
        logger.error("Failed to get device information from lsblk")
        return None

    return str(devices).splitlines()


def is_device_valid(dev_path: str, min_dev_bytes: int) -> bool:
    dev_info = get(
        "lsblk",
        "--noheadings",
        "--nodeps",
        "--bytes",
        "--output",
        "path,size",
        dev_path,
        quiet=True,
    )
    if not dev_info:
        logger.error(
            "Failed to get device information from lsblk for device: " + dev_path
        )
        return False

    dev_info = str(dev_info).split()
    if len(dev_info) <= 1:
        logger.error("Not enough fields given by lsblk for device: " + dev_path)
        return False

    if dev_path != dev_info[0]:
        logger.error(
            "Wrong device given by lsblk."
            + "\nExpected: "
            + dev_path
            + "\n   Given:"
            + dev_info[0]
        )
        return False

    dev_size = dev_info[1]

    if int(dev_size) < min_dev_bytes:
        logger.error(
            "Not enough space on device: "
            + dev_path
            + "\n   Minimum required: "
            + str(min_dev_bytes)
            + " bytes"
            + "\nAvailable on device: "
            + dev_size
            + " bytes"
        )
        return False

    return True


def device_lacks_partitions(dev_path: str) -> Optional[bool]:
    parts = get("lsblk", "--noheadings", "--output", "path", dev_path, quiet=True)
    if not parts:
        logger.error("Failed to use lsblk to list partitions on device: " + dev_path)
        return None

    parts = str(parts).splitlines()[1:]
    if len(parts) > 0:
        logger.warning("Partitions found on device: " + dev_path)
        return False

    return True


def get_device(min_size: int) -> Optional[str]:
    """Select the device to format for installation"""

    devices = list_all_devices()
    if not devices:
        logger.error("Failed to list devices")
        return None

    for dev_path in devices:
        if not is_device_valid(dev_path, min_size):
            logger.info(
                "The minimum requirements for installation were not met by device: "
                + dev_path
            )
            continue

        if not device_lacks_partitions(dev_path):
            logger.info(
                "Formatting a device that already contains partitions will result in irreversible data loss!"
                "\n\t\tExplicit permission (via interactive mode) is required to format a device with existing partitions"
            )
            continue

        return dev_path

    return None


def get_part(device_path: str, part_num: int) -> Optional[str]:
    """Get the path to a partition with a given number on a given device to format for installation"""

    parts = get(
        "lsblk",
        "--noheadings",
        "--output",
        "path",
        device_path,
        quiet=True,
    )
    if not parts:
        logger.error("Failed to get partitions from lsblk for" + device_path)
        return None

    parts = parts.splitlines()

    if part_num >= len(parts):
        logger.error(
            "The given part number ("
            + str(part_num)
            + ") is larger than the number of partitions on "
            + device_path
            + "("
            + str(len(parts) - 1)
            + ")"
        )
        return None

    return parts[part_num]


class Field:
    @staticmethod
    def default_validator(_: str) -> bool:
        return True

    @staticmethod
    def numeric_validator(value: str) -> bool:
        if not value.isnumeric():
            logger.error("The given value is not numeric: " + value)
            return False
        return True

    @staticmethod
    def boot_label_validator(value: str) -> bool:
        if not value:
            logger.error("Boot labels must contain at least one character")
            return False
        if not (value.isprintable() and value.isascii()):
            logger.error(
                "Boot labels cannot contain non-printable or non-ascii characters"
            )
            return False
        return True

    @staticmethod
    def hostname_validator(value: str) -> bool:
        if not value:
            logger.error("Hostnames must contain at least one character")
            return False
        if len(value) > 64:
            logger.error("Hostnames cannot be longer than 64 characters")
            return False
        if not (value.replace("-", "").islower() and value.replace("-", "").isalnum()):
            logger.error(
                "Hostnames may only contain lowercase letters, numbers, and hyphens"
            )
            return False
        return True

    @staticmethod
    def name_validator(value: str) -> bool:
        if not value:
            logger.error("Names must contain at least one character")
            return False
        if value == "root":
            logger.error("The name 'root' is reserved")
            return False
        if value.isnumeric():
            logger.error("Names cannot be entirely numeric")
            return False
        if value.startswith("-"):
            logger.error("Names cannot start with a hyphen")
            return False
        if len(value) > 32:
            logger.error("Names cannot be longer than 32 characters")
            return False
        if not value.replace("-", "").replace("_", "").isalnum():
            logger.error(
                "Names may only contain letters, numbers, underscores, and hyphens"
            )
            return False
        return True

    @staticmethod
    def password_validator(value: str) -> bool:
        if not value:
            logger.error("Passwords must contain at least one character")
            return False
        if not (value.isprintable() and value.isascii()):
            logger.error(
                "Passwords cannot contain non-printable or non-ascii characters"
            )
            return False
        return True

    def __init__(
        self,
        default_value: Any,
        types: Any,
        validator: Callable[[str], bool] = default_validator,
    ) -> None:
        self._value = default_value
        self._types = types
        self._validator = validator

    def get(self) -> Any:
        return self._value

    def get_str(self) -> str:
        if self._value is None:
            return ""
        return str(self._value)

    def set(self, value: Any) -> bool:
        if self._validator(str(value)):
            self._value = value
            return True
        return False


@dataclass
class Profile:
    headless: Field = Field(False, bool)
    network_install: Field = Field(False, bool)
    min_device_bytes: Field = Field(int(10e9), int, validator=Field.numeric_validator)
    device: Field = Field(None, Optional[str])
    luks_encryption: Field = Field(False, bool)
    luks_password: Field = Field(None, str, validator=Field.password_validator)
    luks_uuid: Field = Field(None, str)
    boot_label: Field = Field("MOOS", str, validator=Field.boot_label_validator)
    time_zone: Field = Field("America/Denver", str)
    hostname: Field = Field("moos", str, validator=Field.hostname_validator)
    root_password: Field = Field("root", str, validator=Field.password_validator)
    username: Field = Field("main", str, validator=Field.name_validator)
    user_password: Field = Field("main", str, validator=Field.password_validator)
    sudo_group: Field = Field("wheel", str, validator=Field.name_validator)

    def to_dict(self) -> dict:
        return {field.name: getattr(self, field.name).get() for field in fields(self)}


def dict_to_profile(profile_dict: dict) -> Profile:
    profile = Profile()
    for key, value in profile_dict.items():
        if hasattr(profile, key):
            field: Field = getattr(profile, key)
            if not field.set(value):
                logger.warning(
                    "The given value is invalid for the cooresponding field:"
                    + "\n\tfield: "
                    + key
                    + "\n\tvalue: "
                    + value
                )
            setattr(profile, key, field)
        else:
            logger.warning("Unrecognized field in profile: " + key)
    return profile


def load_packages(path: str) -> Optional[List[str]]:
    try:
        with open(path, "r") as packages_file:
            return [line.strip() for line in packages_file]
    except:
        logger.error("Failed to read the package list from " + path)
        return None


def dump_profile(profile: Profile, path: str) -> bool:
    try:
        with open(path, "w") as profile_file:
            json.dump(profile.to_dict(), profile_file, indent=4)
        return True
    except:
        logger.error("Failed to write the profile to " + path)
        return False


def load_profile(path: str) -> Optional[Profile]:
    profile = Profile()
    try:
        with open(path, "r") as profile_file:
            return dict_to_profile(json.load(profile_file))
    except:
        logger.error("Failed to read the profile from " + path)
        return None


class CursesApp:
    def _hide_cursor(self) -> None:
        curses.curs_set(0)  # Hide the cursor

    def _show_cursor(self) -> None:
        curses.curs_set(1)  # Show the cursor

    def _init_colors(self) -> None:
        curses.start_color()
        curses.init_pair(Level.normal, curses.COLOR_WHITE, curses.COLOR_BLACK)
        curses.init_pair(Level.success, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(Level.error, curses.COLOR_RED, curses.COLOR_BLACK)
        curses.init_pair(
            Level.warning,
            curses.COLOR_YELLOW,
            curses.COLOR_BLACK,
        )
        curses.init_pair(Level.info, curses.COLOR_CYAN, curses.COLOR_BLACK)
        curses.init_pair(Level.verbose, curses.COLOR_WHITE, curses.COLOR_BLACK)

    def _set_color(self, color: Level, window=None) -> None:
        if window is None:
            window = self.pad
        window.bkgdset(curses.color_pair(color))

    def refresh_pad(self, line: int) -> None:
        self.pad.refresh(
            line,
            0,
            self.line_origin,
            self.col_origin,
            self.lines - self.border_lines,
            self.cols - self.border_cols,
        )

    def __init__(self) -> None:
        # Beginning application initialization
        self.good = False

        # Ensure that the terminal is restored to its original state
        self.clean = False
        atexit.register(self.cleanup)

        # Identify the terminal type and send required setup codes (if any)
        self.screen = curses.initscr()

        # Setup colors
        self._init_colors()
        self._set_color(Level.normal, window=self.screen)

        # Edit terminal settings
        curses.noecho()  # Do not echo key presses
        curses.cbreak()  # React to keys instantly without waiting for the Enter key
        self._hide_cursor()

        # Modify curses behavior
        self.screen.keypad(True)  # Automatically interpret special key presses

        # Clear and refresh the screen and window
        self.screen.clear()
        self.screen.refresh()

        # Determine the number of usable lines and columns
        self.border_lines = 3
        self.border_cols = 10
        self.lines = curses.LINES - (self.border_lines * 2)
        self.cols = curses.COLS - (self.border_cols * 2)

        self.line_origin = int((curses.LINES - self.lines) / 2)
        self.col_origin = int((curses.COLS - self.cols) / 2)

        # Create the pad for displaying content
        self.pad = curses.newpad(
            10000,
            self.cols,
        )

        # Clear and refresh the pad
        self.pad.clear()
        self.refresh_pad(0)

        # Initialization is complete
        self.good = True

    def cleanup(self) -> None:
        self.good = False
        if self.clean == False:
            # Reset terminal settings
            self.screen.keypad(False)
            self._show_cursor()
            curses.echo()  # Echo key presses
            curses.nocbreak()  # Wait for the Enter key before receiving input
            curses.endwin()
            self.clean = True

    def show_help(self) -> None:
        self.pad.clear()
        self.pad.addstr(
            "  down:  j / DOWN_ARROW\n"
            "    up:  k / UP_ARROW\n"
            "cancel:  q\n"
            "select:  ; / ENTER"
        )
        self.refresh_pad(0)
        self.pad.getkey()

    def select(
        self,
        prompt: str,
        items: List[str],
        headings: Optional[str] = None,
        cursor_index: int = 0,
        validator: Callable[[str], bool] = Field.default_validator,
    ) -> Optional[int]:
        items_count: int = len(items)

        if items_count <= 0:
            logger.error("Not enough items given to select from")
            return None

        while True:
            try:
                self.pad.clear()

                self.pad.addstr(prompt + "\n\n")
                if headings:
                    self.pad.addstr("     " + headings + "\n")

                visible_options = self.lines - 2
                middle_index = visible_options / 2
                if cursor_index > middle_index:
                    start = int(cursor_index - middle_index)
                    end = int(start + visible_options)
                else:
                    start = 0
                    end = visible_options

                for this_index in range(start, end):
                    if this_index >= items_count:
                        break

                    item = items[this_index]
                    if type(item) is not str:
                        logger.error(
                            "The given item is not a string:"
                            "\n\ttype: " + str(type(item)) + "\n\titem: " + str(item)
                        )
                        return None

                    if cursor_index == this_index:
                        self.pad.addstr("===> ")
                    else:
                        self.pad.addstr("     ")

                    self.pad.addstr(item + "\n")

                self.pad.addstr("\n")
                logger.dump_cache(self._set_color, self.pad.addstr)

                self.refresh_pad(0)

                key = self.screen.getkey()

                if key == "j" or key == "KEY_DOWN":
                    cursor_index += 1
                elif key == "k" or key == "KEY_UP":
                    cursor_index -= 1
                elif key == "q":
                    return None
                elif key == ";" or key == "\n":
                    if validator(items[cursor_index]):
                        return cursor_index
                    else:
                        return None
                else:
                    self.show_help()
                    continue

                cursor_index = max(cursor_index, 0)
                cursor_index = min(cursor_index, len(items) - 1)
            except curses.error:
                pass

    def input(
        self,
        field: Field,
        prompt: str,
    ) -> Field:
        response = field.get_str()

        while True:
            try:
                self.pad.clear()
                self.pad.addstr(prompt + "\n\n: " + response)
                self.refresh_pad(0)

                self._show_cursor()
                key = self.screen.getkey()
                self._hide_cursor()

                if key == "\n":
                    field.set(response)
                    return field
                else:
                    if len(key) == 1:
                        response += key

                if key == "KEY_BACKSPACE":
                    response = response[:-1]

            except curses.error:
                pass

    def get_device(self, min_bytes: int) -> Optional[str]:
        devices_result = get(
            "lsblk",
            "--nodeps",
            "--output",
            "path,size,rm,ro,pttype,ptuuid",
            quiet=True,
        )
        if not devices_result:
            logger.error("Failed to get device information from lsblk")
            return None

        devices = str(devices_result).splitlines()
        if len(devices) <= 1:
            logger.error("Not enough devices listed")
            return None

        device_headings = devices[0]
        devices = devices[1:]

        def interactive_device_validator(dev_info: str) -> bool:
            dev_info_list = dev_info.split()
            if len(dev_info_list) <= 0:
                logger.error("Missing path field")
                return False

            dev_path = dev_info_list[0]

            if not is_device_valid(dev_path, min_bytes):
                logger.error(
                    "The selected device does not meet the minimum requirements for installation"
                )
                return False

            if not device_lacks_partitions(dev_path):
                selection_index = self.select(
                    "The selected device already contains partitions!\n\n"
                    "Are you sure you want to format this device?",
                    [
                        "No. Select a different device.",
                        "Yes. Permanently delete all data on " + dev_path + ".",
                    ],
                )
                return selection_index == 1

            return True

        selection_index = self.select(
            "Select the device to format for installation:",
            devices,
            headings=device_headings,
            validator=interactive_device_validator,
        )
        if selection_index is None:
            logger.error("Failed to select a device")
            return None

        device_info = devices[selection_index].split()
        if len(device_info) <= 0:
            logger.error("Missing path for device: " + devices[selection_index])
            return None

        return device_info[0]

    def get_time_zone(self) -> Optional[str]:
        timezones_str = get("timedatectl", "list-timezones", "--no-pager", quiet=True)
        if not timezones_str:
            logger.error("Failed to get the list of timezones from timedatectl")
            return None

        timezones_list = timezones_str.splitlines()

        selection_index = self.select("Select the new timezone:", timezones_list)
        if selection_index is None:
            logger.error("Failed to select a timezone")
            return None

        return timezones_list[selection_index]


def interactive_conf(profile: Profile) -> Optional[Profile]:
    # Setup the interactive GUI
    app = CursesApp()
    if not app.good:
        return None

    cursor_index: Optional[int] = 0
    error: Optional[str] = None

    class Index(IntEnum):
        headless = 0
        network_install = 1
        min_device_bytes = 2
        device = 3
        luks_encryption = 4
        luks_password = 5
        boot_label = 6
        time_zone = 7
        hostname = 8
        root_password = 9
        username = 10
        user_password = 11
        sudo_group = 12
        begin_installation = 13

    while True:
        cursor_index = app.select(
            "All SSH public keys appended to /root/.ssh/authorized_keys will be copied to the new installation.\n"
            + "If you haven't already, append your SSH public key to /root/.ssh/authorized_keys so you'll have SSH access to the new installation.\n\n"
            + "Select a field to change before installation:",
            [
                "        headless  ->  " + profile.headless.get_str(),
                " network install  ->  " + profile.network_install.get_str(),
                "min device bytes  ->  " + profile.min_device_bytes.get_str(),
                "          device  ->  " + profile.device.get_str(),
                " LUKS encryption  ->  " + profile.luks_encryption.get_str(),
                "   LUKS password  ->  " + profile.luks_password.get_str(),
                "      boot label  ->  " + profile.boot_label.get_str(),
                "       time zone  ->  " + profile.time_zone.get_str(),
                "        hostname  ->  " + profile.hostname.get_str(),
                "   root password  ->  " + profile.root_password.get_str(),
                "        username  ->  " + profile.username.get_str(),
                "   user password  ->  " + profile.user_password.get_str(),
                "      sudo group  ->  " + profile.sudo_group.get_str() + "\n",
                "Begin Installation",
            ],
            cursor_index=cursor_index,
        )
        if cursor_index is None:
            return None

        if cursor_index == int(Index.headless):
            selection_index = app.select(
                "Enable headless installation mode?",
                [
                    "No. Install a graphical environment (X server).",
                    "Yes. Do NOT install the X server or any related programs.",
                ],
            )
            if selection_index is not None:
                profile.headless.set(bool(selection_index))
        if cursor_index == int(Index.network_install):
            selection_index = app.select(
                "Enable network installation mode?\n\n"
                "Note: Check the configuration at /etc/pacman.conf before changing this setting.",
                [
                    "No. Install packages from an offline repository.",
                    "Yes. Download and install packages from remote repositories.",
                ],
            )
            if selection_index is not None:
                profile.network_install.set(bool(selection_index))
        elif cursor_index == int(Index.min_device_bytes):
            profile.min_device_bytes = app.input(
                profile.min_device_bytes,
                "Enter the minimum number of bytes for a device:",
            )
        elif cursor_index == int(Index.device):
            profile.device.set(app.get_device(profile.min_device_bytes.get()))
        elif cursor_index == int(Index.luks_encryption):
            selection_index = app.select(
                "Enable full encryption of the root partition?",
                [
                    "No. Do NOT encrypt my root partition.",
                    "Yes. Encrypt my root partition.",
                ],
            )
            if selection_index is not None:
                profile.luks_encryption.set(bool(selection_index))
        elif cursor_index == int(Index.luks_password):
            profile.luks_password = app.input(
                profile.luks_password,
                "Enter the password for the root partition:",
            )
        elif cursor_index == int(Index.boot_label):
            profile.boot_label = app.input(
                profile.boot_label, "Enter the new boot label:"
            )
        elif cursor_index == int(Index.time_zone):
            new_time_zone = app.get_time_zone()
            if new_time_zone:
                profile.time_zone.set(new_time_zone)
        elif cursor_index == int(Index.hostname):
            profile.hostname = app.input(profile.hostname, "Enter the new hostname:")
        elif cursor_index == int(Index.root_password):
            profile.root_password = app.input(
                profile.root_password,
                "Enter the new password for root:",
            )
        elif cursor_index == int(Index.username):
            profile.username = app.input(
                profile.username,
                "Enter the new name for the user:",
            )
        elif cursor_index == int(Index.user_password):
            profile.user_password = app.input(
                profile.user_password,
                "Enter the new password for the user:",
            )
        elif cursor_index == int(Index.sudo_group):
            profile.sudo_group = app.input(
                profile.sudo_group,
                "Enter the new name for the sudo group:",
            )
        elif cursor_index == int(Index.begin_installation):
            if profile.device.get() is not None:
                break
            profile.device.set(app.get_device(profile.min_device_bytes.get()))
            if profile.device.get() is not None:
                break

    # All necessary information has been collected. Installation may now begin.
    app.cleanup()

    # Attempt to clear the screen after field selection is complete.
    run("clear")  # Do nothing if this fails

    return profile


def main() -> bool:
    # Setup signal handlers.
    signal(SIGINT, lambda c, _: quit(1))
    signal(SIGTERM, lambda c, _: quit(1))

    # Ensure that this program is being run as root.
    if os.geteuid() != 0:
        logger.error("This program must be run as root")
        return False

    # Define the help message and arguments.
    arg_parser = ArgumentParser(
        prog="auto_moos",
        description="This script uses an existing MOOS installation to install MOOS on a device.",
    )
    arg_parser.add_argument(
        "-g",
        "--generate-conf",
        dest="generate_conf",
        help="generate an example package list and profile and exit",
        action="store_true",
    )
    arg_parser.add_argument(
        "-c",
        "--conf-dir",
        dest="conf_dir",
        help="set the path to the directory containing the extra packages list and profile",
        action="store",
    )
    arg_parser.add_argument(
        "-l",
        "--log-file",
        dest="log_file",
        help="set the path to the log file",
        action="store",
    )
    arg_parser.add_argument(
        "-n",
        "--non-interactive",
        dest="non_interactive",
        help="run this script without a GUI",
        action="store_true",
    )

    # Parse command line arguments.
    args: Namespace = arg_parser.parse_args()

    # Declare the default package list.
    headless_packages: List[str] = ["moos", "moos-sshd-conf", "moos-headless"]
    graphical_packages: List[str] = ["moos", "moos-sshd-conf", "moos-xorg"]

    # Declare the default profile.
    profile = Profile()

    # Determine whether this program is running in interactive mode or script mode.
    interactive: bool = not args.non_interactive

    # Set the path to the configuration directory.
    if args.conf_dir:
        conf_dir = make_absolute(args.conf_dir)
    else:
        conf_dir = home_dir + "/.auto_moos"

    # Enable writing to the log file.
    if args.log_file:
        log_file_path = make_absolute(args.log_file)
    else:
        log_file_path = home_dir + "/.auto_moos_log"
    logger.set_log_file(log_file_path)
    logger.clear_log_file()

    package_list_path = conf_dir + "/packages"
    profile_path = conf_dir + "/profile.json"

    if args.generate_conf:
        # Ensure that this operation does not overwrite existing files
        if os.path.exists(package_list_path):
            logger.error("A package list already exists at " + package_list_path)
            return False

        if os.path.exists(profile_path):
            logger.error("A profile already exists at " + profile_path)
            return False

        # Make the configuration directory if it does not already exist
        if not os.path.exists(conf_dir):
            os.makedirs(conf_dir)

        # Generate an example profile
        if not dump_profile(profile, profile_path):
            logger.error("Failed to write an example profile to " + profile_path)
            return False

        quit(0)

    # Read the package list
    extra_packages: List[str] = []
    custom_packages = load_packages(package_list_path)
    if custom_packages:
        extra_packages = custom_packages

    # Read the profile
    custom_profile = load_profile(profile_path)
    if custom_profile:
        profile = custom_profile

    # Attempt to automitically select a device.
    if profile.device.get() is None:
        profile.device.set(get_device(profile.min_device_bytes.get()))

    # If running in interactive mode, prompt the user to verify the profile.
    if interactive:
        profile_result = interactive_conf(profile)
        if not profile_result:
            logger.error("An operation failed during interactive profile configuration")
            return False
        profile = profile_result

    # If a device still hasn't been selected, cancel installation.
    if profile.device.get() is None:
        logger.error(
            "Failed to find a suitable device for installation. Manual intervention is required"
        )
        return False

    # Ensure that authorized_keys is created if installing as headless.
    if profile.headless.get() and not os.path.exists("/root/.ssh/authorized_keys"):
        logger.error(
            "Headless installation requires at least one public key in /root/.ssh/authorized_keys so it's possible to remotely login"
        )
        return False

    # Select the base package list based on the profile.
    if profile.headless.get():
        base_packages = headless_packages
    else:
        base_packages = graphical_packages

    # Add all extra packages to the list of packages to install.
    packages: List[str] = base_packages
    for pkg in extra_packages:
        if pkg not in packages:
            packages.append(pkg)

    # Setup debug utilities
    cols, lines = os.get_terminal_size()

    def sep() -> None:
        print("-" * cols)

    def section(msg: str) -> None:
        sep()
        print(msg + "...")

    if get(
        "lsblk",
        "--noheadings",
        "--output",
        "mountpoints",
        profile.device.get_str(),
    ):
        section("Unmounting all partitions on " + profile.device.get_str())
        if not run("bash", "-ec", "umount " + profile.device.get_str() + "?*"):
            logger.error(
                "Failed to unmount all partitions on " + profile.device.get_str()
            )
            return False

    section("Formatting and partitioning " + profile.device.get_str())
    boot_part_size_megs: int = 500
    boot_part_num: int = 1
    root_part_num: int = 2
    if not run(
        "bash",
        "-ec",
        "("
        "    echo g  ;"  # new GPT partition table
        "    echo n  ;"  # new EFI partition
        "    echo " + str(boot_part_num) + ";"  # EFI partition number
        "    echo    ;"  # start at the first sector
        "    echo +"
        + str(boot_part_size_megs)
        + "M;"  # reserve space for the EFI partition
        "    echo t  ;"  # change EFI partition type
        "    echo 1  ;"  # change partition type to EFI System
        "    echo n  ;"  # new root partition
        "    echo " + str(root_part_num) + ";"  # root partition number
        "    echo    ;"  # start at the end of the EFI partition
        "    echo    ;"  # reserve the rest of the device
        "    echo w  ;"  # write changes
        ") | fdisk " + profile.device.get_str(),
    ):
        logger.error("Failed to format and partition " + profile.device.get_str())
        return False

    section("Identifying the new partitions")
    boot_part = get_part(profile.device.get_str(), boot_part_num)
    if boot_part is None:
        logger.error("Failed to find the path to the boot partition")
        return False
    root_part = get_part(profile.device.get_str(), root_part_num)
    if root_part is None:
        logger.error("Failed to find the path to the root partition")
        return False

    section("Creating filesystems on " + profile.device.get_str())
    if not run("mkfs.fat", "-F", "32", boot_part):
        logger.error("Failed to create a FAT32 filesystem on " + boot_part)
        return False
    if profile.luks_encryption.get():
        if not run(
            "cryptsetup",
            "luksFormat",
            root_part,
            input=profile.luks_password.get(),
        ):
            logger.error("Failed to create a LUKS encrypted container on " + root_part)
            return False

        profile.luks_uuid.set(get("cryptsetup", "luksUUID", root_part))
        if not profile.luks_uuid.get():
            logger.error(
                "Failed to get the UUID of the LUKS encrypted container on " + root_part
            )
            return False

        if not run(
            "cryptsetup", "open", root_part, "root", input=profile.luks_password.get()
        ):
            logger.error("Failed to open the LUKS crypt on " + root_part)
            return False

        if not run("mkfs.ext4", "/dev/mapper/root"):
            logger.error(
                "Failed to create an EXT4 filesystem within the LUKS crypt on "
                + root_part
            )
            return False
    else:
        if not run("mkfs.ext4", root_part):
            logger.error("Failed to create an EXT4 filesystem on " + root_part)
            return False

    section("Mounting filesystems")
    root_mount = "/mnt"
    boot_mount = "/mnt/boot"
    if profile.luks_encryption.get():
        if not run("mount", "--mkdir", "/dev/mapper/root", root_mount):
            logger.error("Failed to mount /dev/mapper/root to " + root_mount)
            return False
    else:
        if not run("mount", "--mkdir", root_part, root_mount):
            logger.error("Failed to mount " + root_part + " to " + root_mount)
            return False
    if not run("mount", "--mkdir", boot_part, boot_mount):
        logger.error("Failed to mount " + boot_part + " to " + boot_mount)
        return False

    section("Syncing package databases")
    if profile.network_install.get():
        if not run("pacman", "-Sy", "--noconfirm", "archlinux-keyring"):
            logger.error("Failed to sync package databases")
            return False
    else:
        if not run("pacman", "-Sy"):
            logger.error("Failed to sync package databases")
            return False

    section("Installing packages with pacstrap")
    if not run("pacstrap", "-K", root_mount, *packages):
        logger.error("Failed to install essential packages")
        return False

    section("Generating fstab")
    fstab_data = get("genfstab", "-U", root_mount)
    if not fstab_data:
        logger.error("Failed to generate fstab")
        return False
    if not write(root_mount + "/etc/fstab", "w", fstab_data):
        logger.error("Failed to write to " + root_mount + "/etc/fstab")
        return False

    section("Copying this script to the root partition")
    if not copy(__file__, root_mount + "/auto_moos.py"):
        logger.error("Failed to copy this script to " + root_mount + "/root")
        return False

    section("Changing root to " + root_mount)
    if not run(
        "arch-chroot",
        root_mount,
        "python",
        "-Bc",
        "from auto_moos import logger, post_pacstrap_setup\n"
        "\n"
        "return_code = not post_pacstrap_setup(\n"
        f"    profile_dict={str(profile.to_dict())},\n"
        f"    boot_part='{boot_part}',\n"
        ")\n"
        "logger.print_cache()\n"
        "quit(return_code)\n",
    ):
        logger.error("Failed operation while root was changed to " + root_mount)
        return False

    section("Removing this script from the root partition")
    if not remove(root_mount + "/auto_moos.py"):
        logger.error("Failed to remove this script from the root partition")
        # Continue installation even if this fails

    section("Copying authorized SSH keys to the root partition")
    home_directory: str = root_mount + "/home/" + profile.username.get_str()
    ssh_directory: str = home_directory + "/.ssh"
    if not run("mkdir", "--parents", "--mode", "700", ssh_directory):
        logger.error("Failed to create the SSH directory")
        return False
    if os.path.exists("/root/.ssh/authorized_keys"):
        if not run(
            "rsync",
            "--chmod=600",
            "/root/.ssh/authorized_keys",
            ssh_directory + "/authorized_keys",
        ):
            logger.error("Failed to copy authorized SSH keys to the root partition")
            return False
    if not run(
        "arch-chroot",
        root_mount,
        "chown",
        "-R",
        "main:main",
        "/home/" + profile.username.get_str() + "/.ssh",
    ):
        logger.error("Failed to update the file ownership for authorized SSH keys")
        return False

    section("Removing this script from the root partition")
    if not remove(root_mount + "/auto_moos.py"):
        logger.error("Failed to remove this script from the root partition")
        # Continue installation even if this fails

    logger.success("Installation complete!")

    section("Copying the log file to the root home directory in the root partition")
    new_log_file_path = root_mount + "/root/.auto_moos_log"
    if not run("cp", log_file_path, new_log_file_path):
        logger.error(
            "Failed to copy the log file at "
            + log_file_path
            + " to "
            + new_log_file_path
        )

    section("Unmounting all partitions on " + profile.device.get_str())
    if not run("umount", "-R", "/mnt"):
        logger.error("Failed to unmount all partitions on " + profile.device.get_str())
    if profile.luks_encryption.get():
        if not run("cryptsetup", "close", "root"):
            logger.error("Failed to close the root partition LUKS crypt")

    return True


def post_pacstrap_setup(
    profile_dict: dict,
    boot_part: str,
) -> bool:
    profile = dict_to_profile(profile_dict)

    # Setup debug utilities
    cols, lines = os.get_terminal_size()

    def sep() -> None:
        print("-" * cols)

    def section(msg: str) -> None:
        sep()
        print(msg + "...")

    if profile.luks_encryption.get():
        section("Adding 'sd-encrypt' to the mkinitcpio HOOKS array")
        with open("/etc/mkinitcpio.conf", "r+") as file:
            file_lines = file.readlines()
            file.seek(0)
            for line in file_lines:
                if line.strip().startswith("HOOKS="):
                    file.write(
                        re.sub(r"\bfilesystems\b", r"sd-encrypt filesystems", line)
                    )
                else:
                    file.write(line)
        if not run("mkinitcpio", "-P"):
            logger.error(
                "Failed to recreate the initramfs image after adding 'sd-encrypt' to the mkinitcpio HOOKS array"
            )
            return False

    section("Installing the boot loader")
    if profile.luks_encryption.get():
        if not run(
            "auto_limine",
            boot_part,
            "--label",
            profile.boot_label.get_str(),
            "--crypt",
            profile.luks_uuid.get_str(),
        ):
            logger.error("Failed to install the boot loader (Limine)")
            return False
    else:
        if not run("auto_limine", boot_part, "--label", profile.boot_label.get_str()):
            logger.error("Failed to install the boot loader (Limine)")
            return False

    section("Setting the root password")
    if not run("chpasswd", input="root:" + profile.root_password.get_str()):
        logger.error("Failed to set the root password")
        return False

    section("Creating the sudo group")
    if not run("groupadd", "--force", profile.sudo_group.get_str()):
        logger.error("Failed to create the sudo group")
        return False

    section("Creating the user")
    if not run(
        "useradd",
        "--create-home",
        "--skel",
        "/etc/moos-skel",
        "--shell",
        "/usr/bin/bash",
        "--user-group",
        "--groups",
        profile.sudo_group.get_str(),
        profile.username.get_str(),
    ):
        logger.error("Failed to create the user")
        return False

    section("Setting the user password")
    if not run(
        "chpasswd",
        input=profile.username.get_str() + ":" + profile.user_password.get_str(),
    ):
        logger.error("Failed to set the user password")
        return False

    section("Providing root privileges to all members of the sudo group")
    if not write(
        "/etc/sudoers",
        "a",
        "\n"
        "## Allow members of group "
        + profile.sudo_group.get_str()
        + " to execute any command\n%"
        + profile.sudo_group.get_str()
        + " ALL=(ALL:ALL) ALL\n",
    ):
        logger.error(
            "Failed to provide root privileges to all members of the sudo group"
        )
        return False

    section("Setting time zone: " + profile.time_zone.get_str())
    if not run(
        "ln",
        "-sf",
        "/usr/share/zoneinfo/" + profile.time_zone.get_str(),
        "/etc/localtime",
    ):
        logger.error("Failed to set time zone: " + profile.time_zone.get_str())
        # Continue installation even if this fails

    section("Syncronizing the hardware clock with the system clock")
    if not run("hwclock", "--systohc"):
        logger.error("Failed to set the hardware clock")
        # Continue installation even if this fails

    section("Enabling NTP time synchronization")
    if not run("systemctl", "enable", "systemd-timesyncd.service"):
        logger.error("Failed to enable the systemd-timesyncd service")
        # Continue installation even if this fails

    section("Adding locales to /etc/locale.gen")
    if write("/etc/locale.gen", "a", "en_US.UTF-8 UTF-8\n"):
        section("Generating locales")
        if run("locale-gen"):
            if not write("/etc/locale.conf", "w", "LANG=en_US.UTF-8"):
                logger.error("Failed to write locale to /etc/locale.conf")
                # Continue installation even if this fails
        else:
            logger.error("Failed to generate locales")
            # Continue installation even if this fails
    else:
        logger.error("Failed to edit /etc/locale.gen, cannot generate locales")
        # Continue installation even if this fails

    section("Setting hostname")
    if not write("/etc/hostname", "w", profile.hostname.get_str()):
        logger.error("Failed to write hostname to /etc/hostname")
        # Continue installation even if this fails

    section("Enabling automatic network configuration")
    if not run("systemctl", "enable", "NetworkManager"):
        logger.error("Failed to enable the NetworkManager service")
        # Continue installation even if this fails

    section("Enabling bluetooth")
    if not run("systemctl", "enable", "bluetooth.service"):
        logger.error("Failed to enable bluetooth service")
        # Continue installation even if this fails

    section("Enabling the firewall")
    if not run("systemctl", "enable", "ufw.service"):
        logger.error("Failed to enable the ufw service")
        # Continue installation even if this fails

    section("Enabling SSH")
    if not run("systemctl", "enable", "sshd.service"):
        logger.error("Failed to enable the sshd service")
        # Continue installation even if this fails

    section("Enabling Open-VM-Tools")
    if not run("systemctl", "enable", "vmtoolsd.service"):
        logger.error("Failed to enable the vmtoolsd service for Open-VM-Tools")
        # Continue installation even if this fails
    if not run("systemctl", "enable", "vmware-vmblock-fuse.service"):
        logger.error(
            "Failed to enable the vmware-vmblock-fuse service for Open-VM-Tools"
        )
        # Continue installation even if this fails

    section("Enabling QEMU Guest Agent")
    if not run("systemctl", "enable", "qemu-guest-agent.service"):
        logger.error("Failed to enable the qemu-guest-agent service for QEMU")
        # Continue installation even if this fails

    section("Enabling VirtualBox Guest Utils")
    if not run("systemctl", "enable", "vboxservice.service"):
        logger.error("Failed to enable the vboxservice service for VirtualBox")
        # Continue installation even if this fails

    if not profile.headless.get():
        section("Enabling libvirtd")
        if not run("systemctl", "enable", "libvirtd.socket"):
            logger.error("Failed to enable the libvirtd socket for QEMU")
            # Continue installation even if this fails
        # if not run("virsh", "net-autostart", "default"):
        #     logger.error(
        #         "Failed to force the network interface for libvirt to start automatically"
        #     )
        #     # Continue installation even if this fails
        if not run("usermod", "-aG", "libvirt", profile.username.get_str()):
            logger.error("Failed add the user to the libvirt group")
            # Continue installation even if this fails

    if profile.headless.get():
        section("Enabling the hotspot")
        if not run("systemctl", "enable", "moos-hotspot.service"):
            logger.error("Failed to enable the moos-hotspot service")
            # Continue installation even if this fails

    if profile.headless.get():
        hotspot_ssid = get("cat", "/etc/moos-hotspot/ssid")
        if hotspot_ssid is not None:
            if len(hotspot_ssid) != 0:
                logger.success("WiFi hotspot SSID: " + str(hotspot_ssid))
            else:
                hostname = get("cat", "/etc/hostname")
                if hostname is not None:
                    logger.success("WiFi hotspot SSID: " + str(hostname))
                else:
                    logger.error("Failed to retrieve the hotspot SSID /etc/hostname")
                    # Continue installation even if this fails
        else:
            logger.error(
                "Failed to retrieve the hotspot SSID from /etc/moos-hotspot/ssid"
            )
            # Continue installation even if this fails

        hotspot_password = get("cat", "/etc/moos-hotspot/password")
        if hotspot_password is None:
            logger.error(
                "Failed to retrieve the password for the WiFi hotspot (Access Point)"
            )
            # Continue installation even if this fails
        else:
            logger.success("WiFi hotspot password: " + str(hotspot_password))

        ssh_port = get("head", "-c", "15", "/etc/ssh/sshd_config.d/10-secure.conf")
        if ssh_port is not None:
            logger.success("SSH " + str(ssh_port))
        else:
            logger.error("Failed to retrieve the SSH port")
            # Continue installation even if this fails

    return True


if __name__ == "__main__":
    return_code = not main()
    logger.print_cache()
    quit(return_code)
