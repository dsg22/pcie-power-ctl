pcie-power-ctl
==============

This tool lets you view and configure the power status of PCI(e) devices. For
now, you can view Dx states and view and configure ASPM link state.

WARNING: Configuring link state is done at a very low level, by writing
directly into device control registers. Don't mess with it if you don't know
what you're doing. If your computer explodes and kills your goldfish, don't
blame me. This tool comes with ABSOLUTELY NO WARRANTY OF ANY KIND!

Requirements
------------
* `python-hwdata` - for looking up PCI IDs and translating them into names
* `python-prettytable` - for formatting the device list output

Usage
-----

When run without arguments, the tool will just display a table of all the PCI
devices on the system, along with their configured and supported power states.

To modify, you can use the `-b` option to set every device into  the lowest
power state it says it supports. Alternatively, you can use the `-s` and `-d`
parameters to set a specific ASPM link state for a device. It is sometimes
possible to set a device into a lower power ASPM state than its configuration
descriptor says it can support. YMMV.

If a device shows `None` for the ASPM fields, it means there is no PCI Express
capability structure in the devices configuration space. Trying to set ASPM
states there won't work, because there is no register for the tool to write
configuration to.

If a device shows `ASPM_DISABLED` for the `ASPM supported` field, that means
it does have a PCI Express capability structure that says it can't work with
ASPM modes enabled. That may or may not be true. I've had success with enabling
ASPM for those kinds of devices in some cases.

Once the tool has finished a write, it will print out the device table, showing
colors for changed values depending on if they changed to the requested value
or to some other value (like going to L1 instead of L0s). Keep in mind that
the kernel, system firmware or the device itself may switch power states back
even if it shows green after the change. Try running the tool without arguments
after 30 seconds or so and see if the power state sticks.

Also, it's important to understand that ASPM applies to the link. You can't
have a device in ASPM mode when the host controller is not. And if devices
are behind bridges, there may be more than one device sharing a bus. In that
case all those devices need to be configured for the desired ASPM level or
it won't work. You need to look at your system PCI topology (for example
using `lspci -t -v`) to see which peripherals connect where.

And finally, if you're going to mess around with this, be methodical. Measure
your power usage over time to be sure your changes are actually helping. Just
because you have more devices showing `ASPM_L1_AND_L0s` in the output does not
necessarily mean you've gained anything. I've seen devices actually use *more*
power after a change, in some cases because they stop going into D3hot/D3cold
state.

A note about ACPI and ASPM
--------------------------

It is possible for the system firmware to signal to the OS that it's not
allowed to change ASPM settings. Linux will obey this without hacks.

If you see a message like this in your kernel log, then your system is affected:
```
ACPI FADT declares the system doesn't support PCIe ASPM, so disable it
```

In theory this should mean that your system firmware will handle that
managing ASPM. And it there is probably a reason why the integrator set that
flag. Maybe some peripherals get flaky in certain power modes or you get
random crashes. Or maybe they just quick-fixed a Windows driver bug with the
sledgehammer approach.

If you want to try disobeying the firmware, read this kernel documentation
on how to dump your ACPI tables, decompile, patch and inject the changes:
[Upgrading ACPI tables via initrd](]https://docs.kernel.org/admin-guide/acpi/initrd_table_override.html)

The flag will be in the FACP table. In the decompiled output of facp.dsl, look
for a line like this:
```
          PCIe ASPM Not Supported (V4) : 1
```

Just toggle that to 0 and follow the docs for the rest. Just remember that
the firmware initrd you create **needs** to be at the start of the initrd,
and it can't be compressed.

You might also consider setting the `pcie_aspm=force` kernel command line to
ensure the kernel uses ASPM.

