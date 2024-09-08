#!/usr/bin/python

"""Automatically installs Arch Linux"""

from argparse import Action, ArgumentParser, Namespace
import atexit
import curses
from dataclasses import dataclass, asdict
from enum import Enum, IntEnum, auto
import json
import os
from queue import Queue, Empty
import shutil
from signal import signal, SIGINT, SIGTERM
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple


# Initialize the global log queue.
log = Queue()
log_file: Any = None


# Message reporting and processing utilities
# ----------------------------------------------------------------------------


class Message_Level(Enum):
    success = auto()
    error = auto()
    warning = auto()
    info = auto()
    verbose = auto()


@dataclass
class Message:
    raw: str
    level: Message_Level


def success(msg: str) -> None:
    log.put(Message(msg, Message_Level.success), block=False)


def error(msg: str) -> None:
    log.put(Message("  [Error] " + msg + ".", Message_Level.error), block=False)


def warning(msg: str) -> None:
    log.put(
        Message("[Warning] " + msg + ".", Message_Level.warning), block=False
    )


def info(msg: str) -> None:
    log.put(Message("   [Info] " + msg + ".", Message_Level.info), block=False)


def verbose(msg: str) -> None:
    log.put(
        Message("[Verbose] " + msg + ".", Message_Level.verbose), block=False
    )


def green(msg: str) -> str:
    return "\033[1;32m" + msg + "\033[0m"


def red(msg: str) -> str:
    return "\033[1;31m" + msg + "\033[0m"


def yellow(msg: str) -> str:
    return "\033[1;33m" + msg + "\033[0m"


def blue(msg: str) -> str:
    return "\033[1;34m" + msg + "\033[0m"


def get_next_log() -> Optional[Message]:
    try:
        msg: Message = log.get_nowait()
    except:
        return None
    if log_file is not None:
        log_file.write(msg.raw + "\n")
    return msg


# Subprocess and filesystem utilities
# ----------------------------------------------------------------------------


def run(
    *args,
    input: str | None = None,
    quiet: bool = True,
    env: Dict[str, str] | None = None
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


def get(*args) -> str | None:
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


def copy(src: str, dst: str) -> bool:
    try:
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    except os.error:
        return False
    return True


def remove(path: str) -> bool:
    try:
        shutil.rmtree(path)
    except os.error:
        return False
    return True


# ----------------------------------------------------------------------------


def list_all_devices() -> Optional[List[str]]:
    devices = get(
        "lsblk",
        "--noheadings",
        "--nodeps",
        "--output",
        "path",
    )
    if not devices:
        error("Failed to get device information from lsblk")
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
    )
    if not dev_info:
        error(
            "Failed to get device information from lsblk for device: "
            + dev_path
        )
        return False

    dev_info = str(dev_info).split()
    if len(dev_info) <= 1:
        error("Not enough fields given by lsblk for device: " + dev_path)
        return False

    if dev_path != dev_info[0]:
        error(
            "Wrong device given by lsblk."
            + "\nExpected: "
            + dev_path
            + "\n   Given:"
            + dev_info[0]
        )
        return False

    dev_size = dev_info[1]

    if int(dev_size) < min_dev_bytes:
        error(
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
    parts = get("lsblk", "--noheadings", "--output", "path", dev_path)
    if not parts:
        error("Failed to use lsblk to list partitions on device: " + dev_path)
        return None

    parts = str(parts).splitlines()[1:]
    if len(parts) > 0:
        warning("Partitions found on device: " + dev_path)
        return False

    return True


def get_device(min_size: int) -> Optional[str]:
    """Select the device to format for installation"""

    devices = list_all_devices()
    if not devices:
        error("Failed to list devices")
        return None

    for dev_path in devices:
        if not is_device_valid(dev_path, min_size):
            info(
                "The minimum requirements for installation were not met by device: "
                + dev_path
            )
            continue

        if not device_lacks_partitions(dev_path):
            info(
                "Formatting a device that already contains partitions will result in irreversible data loss!"
                "\n\t\tExplicit permission (via interactive mode) is required to format a device with existing partitions"
            )
            continue

        return dev_path

    return None


def is_uefi_bootable() -> bool:
    """Determine whether this system is UEFI bootable or not"""

    return os.path.exists("/sys/firmware/efi/fw_platform_size")


class Field:
    @staticmethod
    def default_validator(_: str) -> bool:
        return True

    @staticmethod
    def numeric_validator(value: str) -> bool:
        if not value.isnumeric():
            error("The given value is not numeric: " + value)
            return False
        return True

    @staticmethod
    def boot_label_validator(value: str) -> bool:
        if not value:
            error("Boot labels must contain at least one character")
            return False
        if not (value.isprintable() and value.isascii()):
            error(
                "Boot labels cannot contain non-printable or non-ascii characters"
            )
            return False
        return True

    @staticmethod
    def hostname_validator(value: str) -> bool:
        if not value:
            error("Hostnames must contain at least one character")
            return False
        if len(value) > 64:
            error("Hostnames cannot be longer than 64 characters")
            return False
        if not (
            value.replace("-", "").islower()
            and value.replace("-", "").isalnum()
        ):
            error(
                "Hostnames may only contain lowercase letters, numbers, and hyphens"
            )
            return False
        return True

    @staticmethod
    def name_validator(value: str) -> bool:
        if not value:
            error("Names must contain at least one character")
            return False
        if value.isnumeric():
            error("Names cannot be entirely numeric")
            return False
        if value.startswith("-"):
            error("Names cannot start with a hyphen")
            return False
        if len(value) > 32:
            error("Names cannot be longer than 32 characters")
            return False
        if not value.replace("-", "").replace("_", "").isalnum():
            error(
                "Names may only contain letters, numbers, underscores, and hyphens"
            )
            return False
        return True

    @staticmethod
    def password_validator(value: str) -> bool:
        if not value:
            error("Passwords must contain at least one character")
            return False
        if not (value.isprintable() and value.isascii()):
            error(
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
    network_install = Field(True, bool)
    min_device_bytes = Field(int(10e9), int, validator=Field.numeric_validator)
    device = Field(None, Optional[str])
    boot_label = Field("Arch Linux", str, validator=Field.boot_label_validator)
    time_zone = Field("America/Denver", str)
    hostname = Field("arch", str, validator=Field.hostname_validator)
    root_password = Field("root", str, validator=Field.password_validator)
    username = Field("main", str, validator=Field.name_validator)
    user_password = Field("main", str, validator=Field.password_validator)
    sudo_group = Field("wheel", str, validator=Field.name_validator)


def dump_packages(packages: List[str], path: str) -> bool:
    try:
        with open(path, "w") as packages_file:
            packages_file.write("\n".join(packages))
        return True
    except:
        error("Failed to write the package list to " + path)
        return False


def load_packages(path: str) -> Optional[List[str]]:
    try:
        with open(path, "r") as packages_file:
            return [line.strip() for line in packages_file]
    except:
        error("Failed to read the package list from " + path)
        return None


def dump_profile(profile: Profile, path: str) -> bool:
    json_profile = {}
    key: str
    value: Field
    for key, value in asdict(profile).items():
        json_profile[key] = value.get()
    try:
        with open(path, "w") as profile_file:
            json.dump(json_profile, profile_file, indent=4)
        return True
    except:
        error("Failed to write the profile to " + path)
        return False


def load_profile(path: str) -> Optional[Profile]:
    profile = Profile()
    try:
        with open(path, "r") as profile_file:
            json_profile = json.load(profile_file)
            for key, value in json_profile.items():
                if hasattr(profile, key):
                    field: Field = getattr(profile, key)
                    if not field.set(value):
                        warning(
                            "The given value is invalid for the cooresponding field:"
                            + "\n\tfield: "
                            + key
                            + "\n\tvalue: "
                            + value
                        )
                    setattr(profile, key, field)
                else:
                    warning("Unrecognized field in profile: " + key)
        return profile
    except:
        error("Failed to read the profile from " + path)
        return None


class CursesApp:
    def _hide_cursor(self) -> None:
        curses.curs_set(0)  # Hide the cursor

    def _show_cursor(self) -> None:
        curses.curs_set(1)  # Show the cursor

    class ColorIndex(IntEnum):
        normal = auto()
        highlight = auto()
        success = auto()
        error = auto()
        warning = auto()
        info = auto()
        verbose = auto()

    def _init_colors(self) -> None:
        curses.start_color()
        curses.init_pair(
            CursesApp.ColorIndex.normal, curses.COLOR_WHITE, curses.COLOR_BLACK
        )
        curses.init_pair(
            CursesApp.ColorIndex.highlight,
            curses.COLOR_BLACK,
            curses.COLOR_WHITE,
        )
        curses.init_pair(
            CursesApp.ColorIndex.success, curses.COLOR_GREEN, curses.COLOR_BLACK
        )
        curses.init_pair(
            CursesApp.ColorIndex.error, curses.COLOR_RED, curses.COLOR_BLACK
        )
        curses.init_pair(
            CursesApp.ColorIndex.warning,
            curses.COLOR_YELLOW,
            curses.COLOR_BLACK,
        )
        curses.init_pair(
            CursesApp.ColorIndex.info, curses.COLOR_CYAN, curses.COLOR_BLACK
        )
        curses.init_pair(
            CursesApp.ColorIndex.verbose, curses.COLOR_WHITE, curses.COLOR_BLACK
        )

    def _set_color(self, color: ColorIndex, window=None) -> None:
        if not window:
            window = self.win
        window.bkgdset(curses.color_pair(color))

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
        self._set_color(CursesApp.ColorIndex.normal, window=self.screen)

        # Edit terminal settings
        curses.noecho()  # Do not echo key presses
        curses.cbreak()  # React to keys instantly without waiting for the Enter key
        self._hide_cursor()

        # Modify curses behavior
        self.screen.keypad(True)  # Automatically interpret special key presses

        # Clear and refresh the screen and window
        self.screen.clear()
        self.screen.refresh()

        # The minimum number of lines and columns necessary for this program to function
        self.min_lines = 12
        self.min_cols = 44
        self.max_border_lines = 3
        self.max_border_cols = 10
        if curses.LINES < self.min_lines or curses.COLS < self.min_cols:
            self.cleanup()
            error("Min dim: " + str(self.min_lines) + "x" + str(self.min_cols))
            return

        # Get the size and position of the window
        if curses.LINES > ((self.max_border_lines * 2) + self.min_lines):
            self.lines = curses.LINES - (self.max_border_lines * 2)
        else:
            self.lines = self.min_lines

        if curses.COLS > ((self.max_border_cols * 2) + self.min_cols):
            self.cols = curses.COLS - (self.max_border_cols * 2)
        else:
            self.cols = self.min_cols

        self.line_origin = int((curses.LINES - self.lines) / 2)
        self.col_origin = int((curses.COLS - self.cols) / 2)

        # Create the border and window
        self.border = curses.newwin(
            self.lines, self.cols, self.line_origin, self.col_origin
        )
        self.win = curses.newwin(
            self.lines - 2,
            self.cols - 4,
            self.line_origin + 1,
            self.col_origin + 2,
        )

        # Clear and refresh the border and window
        self.border.clear()
        self.border.border()
        self.border.refresh()
        self.win.clear()
        self.win.refresh()

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
        self.win.clear()
        self.win.addstr(
            "  down:  j / DOWN_ARROW\n"
            "    up:  k / UP_ARROW\n"
            "cancel:  q\n"
            "select:  ; / ENTER"
        )
        self.win.refresh()
        self.win.getkey()

    def select(
        self,
        prompt: str,
        items: List[str],
        headings: Optional[str] = None,
        cursor_index: int = 0,
        validator: Callable[[str], bool] = Field.default_validator,
    ) -> Optional[int]:
        if len(items) <= 0:
            error("Not enough items given to select from")
            return None

        while True:
            try:
                self.win.clear()

                self.win.addstr(prompt + "\n\n")
                if headings:
                    self.win.addstr("     " + headings + "\n")

                for this_index in range(len(items)):
                    item = items[this_index]
                    if type(item) is not str:
                        error(
                            "The given item is not a string:"
                            "\n\ttype: "
                            + str(type(item))
                            + "\n\titem: "
                            + str(item)
                        )
                        return None

                    if cursor_index == this_index:
                        self.win.addstr("===> ")
                    else:
                        self.win.addstr("     ")

                    self.win.addstr(item + "\n")

                self.win.addstr("\n")
                while not log.empty():
                    msg: Optional[Message] = get_next_log()
                    if msg is None:
                        break
                    if msg.level == Message_Level.success:
                        self._set_color(CursesApp.ColorIndex.success)
                    elif msg.level == Message_Level.error:
                        self._set_color(CursesApp.ColorIndex.error)
                    elif msg.level == Message_Level.warning:
                        self._set_color(CursesApp.ColorIndex.warning)
                    elif msg.level == Message_Level.info:
                        self._set_color(CursesApp.ColorIndex.info)
                    elif msg.level == Message_Level.verbose:
                        self._set_color(CursesApp.ColorIndex.verbose)
                    self.win.addstr(msg.raw + "\n")
                    self._set_color(CursesApp.ColorIndex.normal)

                self.win.refresh()

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
                self.win.clear()
                self.win.addstr(prompt + "\n\n: " + response)
                self.win.refresh()

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
        devices = get(
            "lsblk", "--nodeps", "--output", "path,size,rm,ro,pttype,ptuuid"
        )
        if not devices:
            error("Failed to get device information from lsblk")
            return None

        devices = str(devices).splitlines()
        if len(devices) <= 1:
            error("Not enough devices listed")
            return None

        device_headings = devices[0]
        devices = devices[1:]

        def interactive_device_validator(dev_info: str) -> bool:
            dev_info_list = dev_info.split()
            if len(dev_info_list) <= 0:
                error("Missing path field")
                return False

            dev_path = dev_info_list[0]

            if not is_device_valid(dev_path, min_bytes):
                error(
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
            error("Failed to select a device")
            return None

        device_info = devices[selection_index].split()
        if len(device_info) <= 0:
            error("Missing path for device: " + devices[selection_index])
            return None

        return device_info[0]

    def get_time_zone(self) -> Optional[str]:
        timezones_str = get("timedatectl", "list-timezones", "--no-pager")
        if not timezones_str:
            error("Failed to get the list of timezones from timedatectl")
            return None

        timezones_list = timezones_str.splitlines()

        selection_index = self.select(
            "Select the new timezone:", timezones_list
        )
        if selection_index is None:
            error("Failed to select a timezone")
            return None

        return timezones_list[selection_index]


def interactive_conf(profile: Profile) -> Optional[Profile]:
    # Setup the interactive GUI
    app = CursesApp()
    if not app.good:
        return None

    cursor_index: Optional[int] = 0
    error: Optional[str] = None

    while True:
        cursor_index = app.select(
            "Select a field to change before installation:",
            [
                " network install  ->  " + profile.network_install.get_str(),
                "min device bytes  ->  " + profile.min_device_bytes.get_str(),
                "          device  ->  " + profile.device.get_str(),
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

        if cursor_index == 0:  # network install
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
        elif cursor_index == 1:  # min device bytes
            profile.min_device_bytes = app.input(
                profile.min_device_bytes,
                "Enter the minimum number of bytes for a device:",
            )
        elif cursor_index == 2:  # device
            profile.device.set(app.get_device(profile.min_device_bytes.get()))
        elif cursor_index == 3:  # boot label
            profile.boot_label = app.input(
                profile.boot_label, "Enter the new boot label:"
            )
        elif cursor_index == 4:  # time zone
            new_time_zone = app.get_time_zone()
            if new_time_zone:
                profile.time_zone.set(new_time_zone)
        elif cursor_index == 5:  # hostname
            profile.hostname = app.input(
                profile.hostname, "Enter the new hostname:"
            )
        elif cursor_index == 6:  # root password
            profile.root_password = app.input(
                profile.root_password,
                "Enter the new password for root:",
            )
        elif cursor_index == 7:  # username
            profile.username = app.input(
                profile.username,
                "Enter the new name for the user:",
            )
        elif cursor_index == 8:  # user password
            profile.user_password = app.input(
                profile.user_password,
                "Enter the new password for the user:",
            )
        elif cursor_index == 9:  # sudo group
            profile.sudo_group = app.input(
                profile.sudo_group,
                "Enter the new name for the sudo group:",
            )
        elif cursor_index == 10:  # Begin Installation
            if profile.device.get() is not None:
                break
            profile.device.set(app.get_device(profile.min_device_bytes.get()))
            if profile.device.get() is not None:
                break

    # All necessary information has been collected. Installation may now begin.
    app.cleanup()

    # Attempt to clear the screen after field selection is complete.
    run("clear", quiet=False)  # Do nothing if this fails

    return profile


if __name__ == "__main__":
    # Setup signal handlers.
    signal(SIGINT, lambda c, _: quit(1))
    signal(SIGTERM, lambda c, _: quit(1))

    # Define the help message and arguments.
    arg_parser = ArgumentParser(
        prog="auto_arch",
        description="This script uses an existing Arch Linux installation to install Arch Linux on a device.",
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
        help="set the path to the directory containing the package list and profile",
        action="store",
    )
    arg_parser.add_argument(
        "-l",
        "--log-file",
        dest="log_file",
        help="write logs to a given file",
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
    packages: List[str] = [
        "base",
        "base-devel",
        "linux",
        "linux-firmware",
        "bash",
        "bash-completion",
        "man-db",
        "man-pages",
        "cgs-auto-limine",
        "less",
        "curl",
        "git",
        "python",
        "ufw",
        "nano",
        "vim",
        "networkmanager",
        "alsa-utils",
        "bluez",
        "bluez-utils",
        "pulseaudio",
        "pulseaudio-alsa",
        "pulseaudio-bluetooth",
    ]

    # Declare the default profile.
    profile = Profile()

    # Determine whether this program is running in interactive mode or script mode.
    interactive: bool = not args.non_interactive

    # Define paths.
    this_dir = os.path.dirname(__file__)
    home_dir = os.path.expanduser("~")

    # Ensure that the path to the configuration directory is absolute.
    if args.conf_dir:
        if os.path.isabs(args.conf_dir):
            conf_dir = args.conf_dir
        else:
            conf_dir = home_dir + args.conf_dir
    else:
        conf_dir = home_dir + "/.auto_arch"

    # Create the log file (if enabled)
    if args.log_file:
        if os.path.isabs(args.log_file):
            log_file_path = args.log_file
        else:
            log_file_path = home_dir + "/" + args.log_file
        try:
            log_file = open(log_file_path, "w")
        except:
            error("Failed to create the log file")

    package_list_name = "packages"
    profile_name = "profile.json"
    conf_dir = args.conf_dir if args.conf_dir else home_dir + "/.auto_arch"
    package_list_path = conf_dir + "/" + package_list_name
    profile_path = conf_dir + "/" + profile_name

    if args.generate_conf:
        # Ensure that this operation does not overwrite existing files
        if os.path.exists(package_list_path):
            error(
                "A package list already exists at the default location: "
                + package_list_path
            )
            quit(1)

        if os.path.exists(profile_path):
            error(
                "A profile already exists at the default location: "
                + profile_path
            )
            quit(1)

        # Make the configuration directory if it does not already exist
        if not os.path.exists(conf_dir):
            os.makedirs(conf_dir)

        # Generate example packages
        if not dump_packages(packages, package_list_path):
            error(
                "Failed to write an example package list to "
                + package_list_path
            )
            quit(1)

        if not dump_profile(profile, profile_path):
            error("Failed to write an example profile to " + profile_path)
            quit(1)

        quit(0)

    # Read the package list
    custom_packages = load_packages(package_list_path)
    if custom_packages:
        packages = custom_packages

    # Read the profile
    custom_profile = load_profile(profile_path)
    if custom_profile:
        profile = custom_profile

    # Attempt to automitically select a device.
    if profile.device.get() is None:
        profile.device.set(get_device(profile.min_device_bytes.get()))

    # If running in interactive mode, prompt the user to verify the profile.
    if interactive:
        profile = interactive_conf(profile)
        if not profile:
            error(
                "An operation failed during interactive profile configuration"
            )
            quit(1)

    # If a device still hasn't been selected, cancel installation.
    if profile.device.get() is None:
        error(
            "Failed to find a suitable device for installation. Manual intervention is required"
        )
        quit(1)

    # Setup debug utilities
    cols, lines = os.get_terminal_size()

    def sep() -> None:
        print("-" * cols)

    def section(msg: str) -> None:
        sep()
        print(msg + "...")

    section("Identifying supported boot modes")
    uefi = is_uefi_bootable()
    if uefi:
        print("This system is UEFI bootable")
    else:
        print("This system is BIOS bootable")

    if get(
        "lsblk",
        "--noheadings",
        "--output",
        "mountpoints",
        profile.device.get_str(),
    ):
        section("Unmounting all partitions on " + profile.device.get_str())
        if not run("bash", "-ec", "umount " + profile.device.get_str() + "?*"):
            error(
                "Failed to unmount all partitions on "
                + profile.device.get_str()
            )
            quit(1)

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
        error("Failed to format and partition " + profile.device.get_str())
        quit(1)

    section("Creating filesystems on " + profile.device.get_str())
    boot_part = profile.device.get_str() + str(boot_part_num)
    root_part = profile.device.get_str() + str(root_part_num)
    if not run("mkfs.fat", "-F", "32", boot_part):
        error("Failed to create a FAT32 filesystem on " + boot_part)
        quit(1)
    if not run("mkfs.ext4", root_part):
        error("Failed to create an EXT4 filesystem on " + root_part)
        quit(1)

    section("Mounting filesystems")
    root_mount = "/mnt"
    boot_mount = "/mnt/boot"
    if not run("mount", "--mkdir", root_part, root_mount):
        error("Failed to mount " + root_part + " to " + root_mount)
        quit(1)
    if not run("mount", "--mkdir", boot_part, boot_mount):
        error("Failed to mount " + boot_part + " to " + boot_mount)
        quit(1)

    section("Syncing package databases")
    if profile.network_install.get():
        if not run(
            "pacman", "-Sy", "--noconfirm", "archlinux-keyring", quiet=False
        ):
            error("Failed to sync package databases")
            quit(1)
    else:
        if not run("pacman", "-Sy", quiet=False):
            error("Failed to sync package databases")
            quit(1)

    section("Installing packages with pacstrap")
    if not run("pacstrap", "-K", root_mount, *packages, quiet=False):
        error("Failed to install essential packages")
        quit(1)

    section("Generating fstab")
    fstab_data = get("genfstab", "-U", root_mount)
    if not fstab_data:
        error("Failed to generate fstab")
        quit(1)
    if not write(root_mount + "/etc/fstab", "w", fstab_data):
        error("Failed to write to " + root_mount + "/etc/fstab")
        quit(1)

    section("Copying this script to the root partition")
    if not copy(__file__, root_mount + "/root/auto_arch.py"):
        error("Failed to copy this script to " + root_mount + "/root")
        quit(1)

    section("Changing root to " + root_mount)
    if not run(
        "arch-chroot",
        root_mount,
        "python",
        "-Bc",
        "from auto_arch import post_pacstrap_setup\n"
        "\n"
        "quit(\n"
        "    not post_pacstrap_setup(\n"
        "        boot_part=" + boot_part + ",\n"
        "        profile=" + str(profile) + ",\n"
        "    )\n"
        ")",
    ):
        error("Failed operation while root was changed to " + root_mount)
        quit(1)

    section("Removing this script from the root partition")
    remove(root_mount + "/root/auto_arch.py")  # Do nothing if this fails

    section("Unmounting all partitions on " + profile.device.get_str())
    if not run("bash", "-ec", "umount " + profile.device.get_str() + "?*"):
        error("Failed to unmount all partitions on " + profile.device.get_str())
        quit(1)

    sep()
    print(green("Installation complete!"))

    quit(0)


def post_pacstrap_setup(
    profile: Profile,
    boot_part: str,
) -> bool:
    section("Installing the boot loader")
    if not run(
        "auto_limine", boot_part, "--label", profile.boot_label.get_str()
    ):
        error("Failed to install the boot loader (Limine)")
        return False

    section("Setting the root password")
    if not run("chpasswd", input="root:" + profile.root_password.get_str()):
        error("Failed to set the root password")
        # Continue installation even if this fails

    section("Creating the sudo group")
    if run("groupadd", "--force", profile.sudo_group.get_str()):
        section("Creating the user")
        if run(
            "useradd",
            "--create-home",
            "--user-group",
            "--groups",
            profile.sudo_group.get_str(),
            profile.username.get_str(),
        ):
            section("Setting the user password")
            if not run(
                "chpasswd",
                input=profile.username.get_str()
                + ":"
                + profile.user_password.get_str(),
            ):
                error("Failed to set the user password")
                # Continue installation even if this fails
        else:
            error("Failed to create the user")
            # Continue installation even if this fails

        section("Providing root privileges to all members of the sudo group")
        if not write(
            "/etc/sudoers",
            "a",
            "\n"
            "## Allow members of group "
            + profile.sudo_group.get_str()
            + " to execute any command\n"
            + profile.sudo_group.get_str()
            + " ALL=(ALL:ALL) ALL\n",
        ):
            error(
                "Failed to provide root privileges to all members of the sudo group"
            )
            # Continue installation even if this fails
    else:
        error("Failed to create the sudo group")
        # Continue installation even if this fails

    section("Setting time zone: " + profile.time_zone.get_str())
    if not run(
        "ln",
        "-sf",
        "/usr/share/zoneinfo/" + profile.time_zone.get_str(),
        "/etc/localtime",
    ):
        error("Failed to set time zone: " + profile.time_zone.get_str())
        # Continue installation even if this fails

    section("Syncronizing the hardware clock with the system clock")
    if not run("hwclock", "--systohc"):
        error("Failed to set the hardware clock")
        # Continue installation even if this fails

    section("Syncronizing the hardware clock with the system clock")
    if not run("hwclock", "--systohc"):
        error("Failed to set the hardware clock")
        # Continue installation even if this fails

    section("Enabling NTP time synchronization")
    if not run("systemctl", "enable", "systemd-timesyncd.service"):
        error("Failed to enable the systemd-timesyncd service")
        # Continue installation even if this fails

    section("Adding locales to /etc/locale.gen")
    if write("/etc/locale.gen", "a", "en_US.UTF-8 UTF-8"):
        section("Generating locales")
        if run("locale-gen"):
            if not write("/etc/locale.conf", "w", "LANG=en_US.UTF-8"):
                error("Failed to write locale to /etc/locale.conf")
                # Continue installation even if this fails
        else:
            error("Failed to generate locales")
            # Continue installation even if this fails
    else:
        error("Failed to edit /etc/locale.gen, cannot generate locales")
        # Continue installation even if this fails

    section("Setting hostname")
    if not write("/etc/hostname", "w", profile.hostname.get_str()):
        error("Failed to write hostname to /etc/hostname")
        # Continue installation even if this fails

    section("Enabling automatic network configuration")
    if not run("systemctl", "enable", "NetworkManager"):
        error("Failed to enable the NetworkManager service")
        # Continue installation even if this fails

    section("Enabling bluetooth")
    if not run("systemctl", "enable", "bluetooth.service"):
        error("Failed to enable bluetooth service")
        # Continue installation even if this fails

    section("Enabling the firewall")
    if not run("systemctl", "enable", "ufw.service"):
        error("Failed to enable the ufw service")
        # Continue installation even if this fails

    section("Enabling ssh")
    if not run("systemctl", "enable", "sshd.service"):
        error("Failed to enable the sshd service")
        # Continue installation even if this fails

    # section("Enabling libvirtd")
    # if not run("systemctl", "enable", "libvirtd.service"):
    #     error("Failed to enable the libvirtd service")
    #     # Continue installation even if this fails

    return True
