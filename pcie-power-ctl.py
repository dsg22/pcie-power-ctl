#!/usr/bin/env python3

import os
import os.path
import getpass
import pprint
import re
import collections
import argparse
import struct
import time
import sys
from enum import Enum

from prettytable import PrettyTable
import hwdata

COLOR_RED = "\033[0;31;40m"
COLOR_GREEN = "\033[0;32;40m"
COLOR_YELLOW = "\033[0;33;40m"
COLOR_RESET = "\033[0m"

PCI_ID_FILE="/nix/store/5h5758kspk2ir12dvwrgilmcizz617kh-pciutils-3.14.0/share/pci.ids"
SYS_PCI_BASE="/sys/devices/pci0000:00"
PCI_ROOT="0000:00"
BDF_RE = re.compile(r'^[0-9a-f]{4}\:[0-9a-f]{2}\:[0-9a-f]{2}\.[0-9a-f]$')

# List of tuples. The first entry is a relative path to a sysfs file, the second
# the field name to use in the device structure. If the given sysfs file does not exist
# for a device, the field will be set to None. Most of this isn't actually used right now.
DEVICE_ATTRIBUTES = [
  ('power/async', 'power_async'),
  ('power/autosuspend_delay_ms', 'power_autosuspend_delay_ms'),
  ('power/control', 'power_control'),
  ('power/runtime_active_kids', 'power_runtime_active_kids'),
  ('power/runtime_active_time', 'power_runtime_active_time'),
  ('power/runtime_enabled', 'power_runtime_enabled'),
  ('power/runtime_status', 'power_runtime_status'),
  ('power/runtime_suspended_time', 'power_runtime_suspended_time'),
  ('power/runtime_usage', 'power_runtime_usage'),
  ('d3cold_allowed', 'd3cold_allowed'),
  ('link/clkpm', 'link_clkpm'),
  ('link/l1_1_aspm', 'link_l1_1_aspm'),
  ('link/l1_1_pcipm', 'link_l1_1_pcipm'),
  ('link/l1_2_aspm', 'link_l1_2_aspm'),
  ('link/l1_2_pcipm', 'link_l1_2_pcipm'),
  ('link/l1_aspm', 'link_l1_aspm'),
]

class aspm_states(Enum):
  ASPM_DISABLED =   0b00
  ASPM_L0s_ONLY =   0b01
  ASPM_L1_ONLY =    0b10
  ASPM_L1_AND_L0s = 0b11
  def __str__(self):
    # Needed so we can get human-readable help messages from argparse
    return self.name

class PciDevice:
  def __init__(self, bdf, sysfs_path, parent):

    # Bus-Domain-Function, the [xxxx:]xx:xx.x h addresses used to identify
    # stuff on the PCI bus.
    self.bdf = self.__name = bdf
    self.sysfs_path = sysfs_path
    self.update_config_space()
    self.vendorid = self.sysfs_get('vendor')
    self.deviceid = self.sysfs_get('device')
    self.extra_attributes = {}
    self.parent = parent
    self.hwdata = hwdata.PCI()
    self.update_device_info()

    # Discover downstream devices and hubs
    self.children = []
    self.scan_bus()

  def sysfs_get(self, filename, binary=False):
    """
    Returns the content of the sysfs file under the device's path. The
    optional binary argument gives a bytes output.
    If the file does not exist or is not writable, we return None.
    """
    path = os.path.join(self.sysfs_path, filename)
    if binary:
      mode = 'rb'
    else:
      mode = 'r'
    try:
      with open(path, mode) as f:
        return(f.read())
    except OSError:
      return(None)

  def update_device_info(self):
    for (filename, attrname) in DEVICE_ATTRIBUTES:
      self.extra_attributes[attrname] = self.sysfs_get(filename)

    if self.vendorid:
      self.vendorid = self.vendorid.lstrip('0x').strip()
      self.vendor  = self.hwdata.get_vendor(self.vendorid)
    else:
      self.vendor = ""

    if self.deviceid:
      self.deviceid = self.deviceid.lstrip('0x').strip()
      self.device  = self.hwdata.get_device(self.vendorid, self.deviceid)
    else:
      self.device = ""

    self.power_state = self.sysfs_get('power_state')
    self.update_aspm_status()

  def scan_bus(self):
    """
    Recursively scan child buses, devices and functions. PciDevice objects
    will be allocated and populated for them.
    """
    # Find the IDs of our immediate child nodes
    for filename in os.listdir(self.sysfs_path):
      if BDF_RE.match(filename):
        # Valid BDF, construct a PciDevice for it. It will continue the scan.
        self.children.append(PciDevice(filename, os.path.join(self.sysfs_path, filename), self.bdf))

  def walk_bus(self):
    """
    Generator that yields every BDF downstream of us. Includes this object.
    """
    yield self
    for child in self.children:
      yield from child.walk_bus()

  def update_config_space(self):
    """
    Read the PCI configuration space and update our cached copy.
    """
    self.config_space = self.sysfs_get('config', binary=True)

  def write_config(self, offset, data):
    """
    Write the given bytes-like object data into the device's PCI configuration
    space at the given offset.
    """
    with open(os.path.join(self.sysfs_path, 'config'), 'r+b') as f:
      f.seek(offset)
      print("{}: Writing to device, offset={} data={}".format(
        self.bdf,
        hex(offset),
        data.hex()
      ))
      f.write(data)


  def get_link_aspm_config_offsets(self):
    """
    Parse PCIe configuration registers to find the byte offsets to the PCIe
    link capabilities and link status registers. The link capabilities
    register is where information on supported ASPM modes is. The link status
    register holds information about currently active modes, and writing
    to it can change the operational mode.
    Returns (link_capabilities_offset, link_status_offset), or None if no
    link capabilities register was found.
    """
    if len(self.config_space) < 256:
      raise ValueError("Configuration space must be at least 256 bytes")

    # Get capabilities pointer (for type 0 header, offset 0x34)
    cap_ptr = self.config_space[0x34]

    # Keep track of addresses we've hit, just in case a device has registers
    # that send us in a loop.
    visited = set()
    while cap_ptr and cap_ptr not in visited:
      if cap_ptr + 2 > len(self.config_space):
        break

      visited.add(cap_ptr)
      cap_id = self.config_space[cap_ptr]
      next_ptr = self.config_space[cap_ptr + 1]

      if cap_id == 0x10:  # PCI Express Capability ID
        pcie_cap_offset = cap_ptr

        # Link Capabilities is at offset 0x0C (4 bytes)
        link_cap_offset = pcie_cap_offset + 0x0C
        link_ctrl_offset = pcie_cap_offset + 0x10

        if link_ctrl_offset + 2 > len(self.config_space):
          break

        return (link_cap_offset, link_ctrl_offset)
      cap_ptr = next_ptr

    # No PCI Express Capabilities structure found.
    return None

  def update_aspm_status(self):
    """
    Reads information from PCI config space registers to determine supported
    and current ASPM modes. Updates our cached copy.
    """
    if self.config_space is None:
      self.aspm_capabilities = None
      self.aspm_link_status = None
      return None
    self.aspm_capabilities = self.get_aspm_capabilities()
    self.aspm_link_status = self.get_aspm_link_status()

  def get_aspm_capabilities(self):
    """
    Returns an aspm_states Enum with the reported ASPM capabilities.
    """
    try:
      (link_cap_offset, link_ctrl_offset) = self.get_link_aspm_config_offsets()
    except (NameError, TypeError):
      # No PCIe capability structure found.
      return(None)
    link_cap = struct.unpack_from("<I", self.config_space, link_cap_offset)[0]

    # ASPM Support (bits 11:10 of Link Capabilities)
    return(aspm_states((link_cap >> 10) & 0x3))

  def get_aspm_link_status(self):
    try:
      (link_cap_offset, link_ctrl_offset) = self.get_link_aspm_config_offsets()
    except (NameError, TypeError):
      # No PCIe capability structure found.
      return(None)

    link_ctrl = struct.unpack_from("<H", self.config_space, link_ctrl_offset)[0]

    # ASPM Enabled (bits 1:0 of Link Control)
    return(aspm_states(link_ctrl & 0x3))

  def set_aspm_link_status(self, new_link_status):
    """
    Writes the given settings to the ASPM configuration in the link status
    register. It's up to the device whether it abides, especially if the
    mode being set is not advertised in the link capabilities.
    """
    try:
      (link_cap_offset, link_ctrl_offset) = self.get_link_aspm_config_offsets()
    except (NameError, TypeError):
      # No PCIe capability structure found.
      print("{}: Error: Can't set new link status {} because the PCIe capabilities structure was not found.".format(
        self.bdf,
        new_link_status.name
      ))
      return(None)

    old_link_status = self.aspm_link_status.value
    old_link_ctrl = struct.unpack_from("<H", self.config_space, link_ctrl_offset)[0]
    new_link_ctrl = old_link_ctrl | new_link_status.value
    new_link_ctrl_packed = struct.pack("<H", new_link_ctrl)

    self.write_config(link_ctrl_offset, new_link_ctrl_packed)

    # Wait a little time for the device to respond.
    time.sleep(5)

    # Update our view of the device to see if it accepted the new status.
    self.update_config_space()
    self.update_aspm_status()

    if self.aspm_link_status.value == new_link_status.value:
      print("Success, the device is now in ASPM mode {}".format(new_link_status.name))
    else:
      print("The device did not accept the new ASPM mode. We requested {} but after write it is in {}.".format(new_link_status.name, self.aspm_link_status.name))

def main():
  parser = argparse.ArgumentParser(description="Manages PCIe power states.")

  parser.add_argument('-b', '--best', action='store_true', help='Change ASPM params for all devices to best supported')
  parser.add_argument('-s', '--set' , help='Try to apply the specified ASPM state to a device.', type=lambda state: aspm_states[state], choices=list(aspm_states))
  parser.add_argument('-d', '--device' , help='Device to operate on, for actions that require them. Use a full address including domain, such as those printed by this program or `lspci -D`.')
  args = parser.parse_args()

  if getpass.getuser() != 'root':
    print("This script must be run as root.")
    sys.exit(1)

  if args.set and not args.device:
    print("Device must be specified for --set")
    sys.exit(1)

  root = PciDevice(PCI_ROOT, SYS_PCI_BASE, None)
  table = PrettyTable()
  #table.field_names = ["Device ID", 'Vendor', 'Product', "Power state", 'ASPM supported', 'ASPM before changes', 'ASPM status']
  table.field_names = ["Device ID", 'Vendor', 'Product', "Power state", 'ASPM supported', 'ASPM status']

  old_status = {}
  for dev in root.walk_bus():
    # Save original state so we can compare later
    old_status[dev.bdf] = dev.aspm_link_status

    if args.set and args.device == dev.bdf:
      dev.set_aspm_link_status(args.set)

    if args.best and dev.aspm_capabilities != dev.aspm_link_status:
      dev.set_aspm_link_status(dev.aspm_capabilities)

    color = COLOR_RESET
    if old_status[dev.bdf] != dev.aspm_link_status:
      # The ASPM status of this device changed
      if args.set and args.device == dev.bdf:
        if dev.aspm_link_status == args.set:
          # We were asked to set a status on a specific device and it succeeded.
          color = COLOR_GREEN
        else:
          # We were asked to set a status on a specific device, the status changed but not to the state we asked for.
          color = COLOR_YELLOW
      else:
        # We were asked to set the best supported state
        if dev.aspm_capabilities == dev.aspm_link_status:
          # Device is in best state
          color = COLOR_GREEN
        else:
          # Device status changed but not to best supported state
          color = COLOR_YELLOW

      aspm_link_status = "{}{}{}".format(color, dev.aspm_link_status, COLOR_RESET)
    else:
      aspm_link_status = dev.aspm_link_status

    aspm_capabilities = dev.aspm_capabilities

    row = [
      dev.bdf,
      dev.vendor or dev.vendorid,
      dev.device or dev.deviceid,
      dev.power_state,
      aspm_capabilities,
      aspm_link_status
   ]
    table.add_row(row)
  print(table)


if __name__ == '__main__':
  main()

