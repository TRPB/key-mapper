#!/usr/bin/python3
# -*- coding: utf-8 -*-
# key-mapper - GUI for device specific keyboard mappings
# Copyright (C) 2021 sezanzeb <proxima@sezanzeb.de>
#
# This file is part of key-mapper.
#
# key-mapper is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# key-mapper is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with key-mapper.  If not, see <https://www.gnu.org/licenses/>.


"""Starts injecting keycodes based on the configuration.

https://github.com/LEW21/pydbus/tree/cc407c8b1d25b7e28a6d661a29f9e661b1c9b964/examples/clientserver  # noqa pylint: disable=line-too-long
"""


import os
import sys
import json
import time
import atexit

import evdev
from pydbus import SystemBus
import gi
gi.require_version('GLib', '2.0')
from gi.repository import GLib

from keymapper.logger import logger, is_debug
from keymapper.injection.injector import Injector, UNKNOWN
from keymapper.mapping import Mapping
from keymapper.config import config
from keymapper.state import system_mapping, XMODMAP_FILENAME, xmodmap_to_dict
from keymapper.getdevices import get_devices, refresh_devices


BUS_NAME = 'keymapper.Control'


def path_to_device_name(path):
    """Find the name of the get_devices group this path belongs to.

    The group name is commonly referred to as "device".

    Parameters
    ----------
    path : str
    """
    if not path.startswith('/dev/input/'):
        # already the name
        return path

    devices = get_devices()
    for device in devices:
        for candidate_path in devices[device]['paths']:
            if path == candidate_path:
                return device

    logger.debug('Device path %s is not managed by key-mapper', path)
    return None


class AutoloadHistory:
    """Contains the autoloading history and constraints."""
    def __init__(self):
        """Construct this with an empty history."""
        # mapping of device -> (timestamp, preset)
        self._autoload_history = {}

    def remember(self, device, preset):
        """Remember when this preset was autoloaded for the device."""
        self._autoload_history[device] = (time.time(), preset)

    def forget(self, device):
        """The injection was stopped or started by hand."""
        if device in self._autoload_history:
            del self._autoload_history[device]

    def may_autoload(self, device, preset):
        """Check if this autoload would be redundant.

        This is needed because udev triggers multiple times per hardware
        device, and because it should be possible to stop the injection
        by unplugging the device if the preset goes wrong or if key-mapper
        has some bug that prevents the computer from being controlled.

        For that unplug and reconnect the device twice within a 15 seconds
        timeframe which will then not ask for autoloading again. Wait 3
        seconds between replugging.
        """
        if device not in self._autoload_history:
            return True

        if self._autoload_history[device][1] != preset:
            return True

        # bluetooth devices go to standby mode after some time. After a
        # certain time of being disconnected it should be legit to autoload
        # again. It takes 2.5 seconds for me when quickly replugging my usb
        # mouse until the daemon is asked to autoload again. Redundant calls
        # by udev to autoload for the device seem to happen within 0.2
        # seconds in my case.
        now = time.time()
        threshold = 15  # seconds
        if self._autoload_history[device][0] < now - threshold:
            return True

        return False


class Daemon:
    """Starts injecting keycodes based on the configuration.

    Can be talked to either over dbus or by instantiating it.

    The Daemon may not have any knowledge about the logged in user, so it
    can't read any config files. It has to be told what to do and will
    continue to do so afterwards, but it can't decide to start injecting
    on its own.
    """

    # https://dbus.freedesktop.org/doc/dbus-specification.html#type-system
    dbus = f"""
        <node>
            <interface name='{BUS_NAME}'>
                <method name='stop_injecting'>
                    <arg type='s' name='device' direction='in'/>
                </method>
                <method name='get_state'>
                    <arg type='s' name='device' direction='in'/>
                    <arg type='i' name='response' direction='out'/>
                </method>
                <method name='start_injecting'>
                    <arg type='s' name='device' direction='in'/>
                    <arg type='s' name='preset' direction='in'/>
                    <arg type='b' name='response' direction='out'/>
                </method>
                <method name='stop_all'>
                </method>
                <method name='set_config_dir'>
                    <arg type='s' name='config_dir' direction='in'/>
                </method>
                <method name='autoload'>
                </method>
                <method name='autoload_single'>
                    <arg type='s' name='device_path' direction='in'/>
                </method>
                <method name='hello'>
                    <arg type='s' name='out' direction='in'/>
                    <arg type='s' name='response' direction='out'/>
                </method>
            </interface>
        </node>
    """

    def __init__(self):
        """Constructs the daemon."""
        logger.debug('Creating daemon')
        self.injectors = {}
        self.config_dir = None

        self.autoload_history = AutoloadHistory()
        self.refreshed_devices_at = 0

        atexit.register(self.stop_all)

    @classmethod
    def connect(cls, fallback=True):
        """Get an interface to start and stop injecting keystrokes.

        Parameters
        ----------
        fallback : bool
            If true, returns an instance of the daemon instead if it cannot
            connect
        """
        try:
            bus = SystemBus()
            interface = bus.get(BUS_NAME)
            logger.info('Connected to the service')
        except GLib.GError as error:
            if not fallback:
                logger.error('Service not running? %s', error)
                return None

            logger.info('Starting the service')
            # Blocks until pkexec is done asking for the password.
            # Runs via key-mapper-control so that auth_admin_keep works
            # for all pkexec calls of the gui
            debug = ' -d' if is_debug() else ''
            cmd = f'pkexec key-mapper-control --command start-daemon {debug}'

            # using pkexec will also cause the service to continue running in
            # the background after the gui has been closed, which will keep
            # the injections ongoing

            logger.debug('Running `%s`', cmd)
            os.system(cmd)
            time.sleep(0.2)

            # try a few times if the service was just started
            for attempt in range(3):
                try:
                    interface = bus.get(BUS_NAME)
                    break
                except GLib.GError as error:
                    logger.debug(
                        'Attempt %d to connect to the service failed: "%s"',
                        attempt + 1, error
                    )
                time.sleep(0.2)
            else:
                logger.error('Failed to connect to the service')
                sys.exit(1)

        return interface

    def publish(self):
        """Make the dbus interface available."""
        bus = SystemBus()
        try:
            bus.publish(BUS_NAME, self)
        except RuntimeError as error:
            logger.error('Is the service is already running? %s', str(error))
            sys.exit(1)

    def run(self):
        """Start the daemons loop. Blocks until the daemon stops."""
        loop = GLib.MainLoop()
        logger.debug('Running daemon')
        loop.run()

    def refresh_devices(self, device=None):
        """Keep the devices up to date."""
        now = time.time()
        if now - 10 > self.refreshed_devices_at:
            logger.debug('Refreshing because last info is too old')
            refresh_devices()
            self.refreshed_devices_at = now
            return

        if device is not None:
            if device.startswith('/dev/input/'):
                for group in get_devices().values():
                    if device in group['paths']:
                        break
                else:
                    logger.debug('Refreshing because path unknown')
                    refresh_devices()
                    self.refreshed_devices_at = now
                    return
            else:
                if device not in get_devices():
                    logger.debug('Refreshing because name unknown')
                    refresh_devices()
                    self.refreshed_devices_at = now
                    return

    def stop_injecting(self, device):
        """Stop injecting the mapping for a single device."""
        if self.injectors.get(device) is None:
            logger.debug(
                'Tried to stop injector, but none is running for device "%s"',
                device
            )
            return

        self.injectors[device].stop_injecting()
        self.autoload_history.forget(device)

    def get_state(self, device):
        """Get the injectors state."""
        injector = self.injectors.get(device)
        return injector.get_state() if injector else UNKNOWN

    def set_config_dir(self, config_dir):
        """All future operations will use this config dir.

        Existing injections (possibly of the previous user) will be kept
        alive, call stop_all to stop them.

        Parameters
        ----------
        config_dir : string
            This path contains config.json, xmodmap and the
            presets directory
        """
        config_path = os.path.join(config_dir, 'config.json')
        if not os.path.exists(config_path):
            logger.error('"%s" does not exist', config_path)
            return

        self.config_dir = config_dir
        config.load_config(config_path)

    def _autoload(self, device):
        """Check if autoloading is a good idea, and if so do it.

        Parameters
        ----------
        device : str
            Device name. Expects a key that is present in get_devices().
            Can also be a path starting with /dev/input/
        """
        self.refresh_devices(device)

        device = path_to_device_name(device)
        if device not in get_devices():
            # even after refresh_devices, the device is not in
            # get_devices(), so it's either not relevant for key-mapper,
            # or not connected yet
            return

        preset = config.get(['autoload', device], log_unknown=False)

        if preset is None:
            # no autoloading is configured for this device
            return

        if not isinstance(preset, str):
            # might be broken due to a previous bug
            config.remove(['autoload', device])
            config.save_config()
            return

        logger.info('Autoloading "%s"', device)

        if not self.autoload_history.may_autoload(device, preset):
            logger.info(
                'Not autoloading the same preset "%s" again for device "%s"',
                preset, device
            )
            return

        self.start_injecting(device, preset)
        self.autoload_history.remember(device, preset)

    def autoload_single(self, device):
        """Inject the configured autoload preset for the device.

        If the preset is already being injected, it won't autoload it again.

        Parameters
        ----------
        device : str
            The name of the device as indexed in get_devices()
        """
        if device.startswith('/dev/input/'):
            # this is only here to avoid confusing console output,
            # block invalid requests before any logs are written.
            # Those requests are rejected later anyway.
            try:
                name = evdev.InputDevice(device).name
                if 'key-mapper' in name:
                    return
            except OSError:
                return

        logger.info('Request to autoload for "%s"', device)

        if self.config_dir is None:
            logger.error(
                'Tried to autoload %s without configuring the daemon first '
                'via set_config_dir.',
                device
            )
            return

        self._autoload(device)

    def autoload(self):
        """Load all autoloaded presets for the current config_dir.

        If the preset is already being injected, it won't autoload it again.
        """
        if self.config_dir is None:
            logger.error(
                'Tried to autoload without configuring the daemon first '
                'via set_config_dir.'
            )
            return

        autoload_presets = list(config.iterate_autoload_presets())

        logger.info('Autoloading for all devices')

        if len(autoload_presets) == 0:
            logger.error('No presets configured to autoload')
            return

        for device, _ in autoload_presets:
            self._autoload(device)

    def start_injecting(self, device, preset):
        """Start injecting the preset for the device.

        Returns True on success. If an injection is already ongoing for
        the specified device it will stop it automatically first.

        Parameters
        ----------
        device : string
            The name of the device
        preset : string
            The name of the preset
        """
        self.refresh_devices(device)

        device = path_to_device_name(device)

        if self.config_dir is None:
            logger.error(
                'Tried to start an injection without configuring the daemon '
                'first via set_config_dir.'
            )
            return False

        if device not in get_devices():
            logger.error('Could not find device "%s"', device)
            return False

        preset_path = os.path.join(
            self.config_dir,
            'presets',
            device,
            f'{preset}.json'
        )

        mapping = Mapping()
        try:
            mapping.load(preset_path)
        except FileNotFoundError as error:
            logger.error(str(error))
            return False

        if self.injectors.get(device) is not None:
            self.stop_injecting(device)

        # Path to a dump of the xkb mappings, to provide more human
        # readable keys in the correct keyboard layout to the service.
        # The service cannot use `xmodmap -pke` because it's running via
        # systemd.
        xmodmap_path = os.path.join(self.config_dir, XMODMAP_FILENAME)
        try:
            with open(xmodmap_path, 'r') as file:
                # do this for each injection to make sure it is up to
                # date when the system layout changes.
                logger.debug('Using keycodes from "%s"', xmodmap_path)
                system_mapping.update(xmodmap_to_dict(file.read()))
                # the service now has process wide knowledge of xmodmap
                # keys of the users session
        except FileNotFoundError:
            logger.error('Could not find "%s"', xmodmap_path)

        try:
            injector = Injector(device, mapping)
            injector.start()
            self.injectors[device] = injector
        except OSError:
            # I think this will never happen, probably leftover from
            # some earlier version
            return False

        return True

    def stop_all(self):
        """Stop all injections."""
        logger.info('Stopping all injections')
        for device in list(self.injectors.keys()):
            self.stop_injecting(device)

    def hello(self, out):
        """Used for tests."""
        logger.info('Received "%s" from client', out)
        return out
