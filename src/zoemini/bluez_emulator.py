"""BlueZ side of the emulator: the computer presents itself as a Zoemini 2.

- adapter name `Canon (XX:XX) Mini Printer` (the official app discovers
  printers by name: `^Canon\\s(.*?)\\s(Mini Printer|Instant Camera)$`);
- the four SDP services of the real printer, including SPP (serial port) on RFCOMM channel 1;
- pairing agent without input that accepts everything (for the duration of the tests).

Dependency: dbus-fast (`pip install '.[emulator]'`). The adapter's original
name is restored on exit.
"""
import asyncio
import logging
import os
import socket
import threading
from pathlib import Path

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_method

from .emulator import FakeZoemini

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
AGENT_PATH = "/zoemini/agent"

# SDP services published by the real Zoemini 2 (protocol.md, transport).
# The app uses them to recognise the device: without 00500056-0223 ("PV 0223"),
# it treats it as an older-generation model.
SERVICES = [
    # (UUID, RFCOMM channel, name)
    (SPP_UUID, 1, "SerialPort"),
    ("00000000-deca-fade-deca-deafdecacaff", 22, "Apple Inc."),
    ("e5b152ed-6b46-09e9-4678-665e9a972cbc", 23, "WeChat"),
    ("00500056-0223-0000-0000-000000000000", 25, "FSK"),
]


class Agent(ServiceInterface):
    """NoInputNoOutput pairing agent: accepts every request."""

    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @dbus_method()
    def Release(self):
        pass

    @dbus_method()
    def RequestPinCode(self, device: "o") -> "s":
        log.info("PIN code requested by %s: 0000", device)
        return "0000"

    @dbus_method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        log.info("PIN code %s for %s", pincode, device)

    @dbus_method()
    def RequestPasskey(self, device: "o") -> "u":
        return 0

    @dbus_method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        pass

    @dbus_method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        log.info("pairing accepted: %s (code %06d)", device, passkey)

    @dbus_method()
    def RequestAuthorization(self, device: "o"):
        log.info("authorisation granted: %s", device)

    @dbus_method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        log.info("service %s authorised for %s", uuid, device)

    @dbus_method()
    def Cancel(self):
        log.info("pairing cancelled")


class SppProfile(ServiceInterface):
    """Server RFCOMM profile: BlueZ hands us each incoming connection."""

    def __init__(self, out_dir: Path, mac: str, loop: asyncio.AbstractEventLoop, label: str,
                 serial: str):
        super().__init__("org.bluez.Profile1")
        self.out_dir, self.mac, self.loop, self.label = out_dir, mac, loop, label
        self.serial = serial
        self.sockets: list[socket.socket] = []

    @dbus_method()
    def Release(self):
        pass

    @dbus_method()
    def NewConnection(self, device: "o", fd: "h", properties: "a{sv}"):
        sock = socket.socket(fileno=os.dup(fd))
        os.close(fd)
        sock.setblocking(True)          # BlueZ hands over a non-blocking descriptor
        log.info("connection from %s on service %s", device, self.label)
        fake = FakeZoemini(self.out_dir, mac=self.mac, serial=self.serial)
        self.sockets.append(sock)
        # Daemon thread: does not prevent the program from exiting (Ctrl+C)
        threading.Thread(target=fake.serve, args=(sock,), daemon=True).start()

    def close_all(self):
        for sock in self.sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    @dbus_method()
    def RequestDisconnection(self, device: "o"):
        log.info("disconnection requested by %s", device)


async def _interface(bus, path, name):
    intro = await bus.introspect(BLUEZ, path)
    return bus.get_proxy_object(BLUEZ, path, intro).get_interface(name)


async def run(out_dir: Path, adapter: str = "hci0", discoverable_s: int = 300,
              serial: str = "EMFEMUL00001") -> None:
    loop = asyncio.get_running_loop()
    bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
    path = f"/org/bluez/{adapter}"
    props = await _interface(bus, path, "org.freedesktop.DBus.Properties")
    address = (await props.call_get("org.bluez.Adapter1", "Address")).value
    old_alias = (await props.call_get("org.bluez.Adapter1", "Alias")).value
    name = f"Canon ({address[-5:]}) Mini Printer"

    bus.export(AGENT_PATH, Agent())
    agents = await _interface(bus, "/org/bluez", "org.bluez.AgentManager1")
    profiles = await _interface(bus, "/org/bluez", "org.bluez.ProfileManager1")
    await agents.call_register_agent(AGENT_PATH, "NoInputNoOutput")
    await agents.call_request_default_agent(AGENT_PATH)
    profile_paths, profile_objs = [], []
    for i, (uuid, channel, label) in enumerate(SERVICES):
        ppath = f"/zoemini/profile{i}"
        obj = SppProfile(out_dir, address, loop, f"{label} (channel {channel})", serial)
        profile_objs.append(obj)
        bus.export(ppath, obj)
        await profiles.call_register_profile(ppath, uuid, {
            "Name": Variant("s", label),
            "Role": Variant("s", "server"),
            "Channel": Variant("q", channel),
            "RequireAuthentication": Variant("b", False),
            "RequireAuthorization": Variant("b", False),
        })
        profile_paths.append(ppath)
    for key, sig, value in (("Powered", "b", True), ("Alias", "s", name),
                            ("Pairable", "b", True),
                            ("DiscoverableTimeout", "u", discoverable_s),
                            ("Discoverable", "b", True)):
        await props.call_set("org.bluez.Adapter1", key, Variant(sig, value))

    print(f"Emulator running: \"{name}\" ({address}), discoverable {discoverable_s} s, serial {serial}.")
    print(f"Received JPEGs go to: {out_dir.resolve()}   — Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        for obj in profile_objs:
            obj.close_all()
        await props.call_set("org.bluez.Adapter1", "Discoverable", Variant("b", False))
        await props.call_set("org.bluez.Adapter1", "Alias", Variant("s", old_alias))
        for ppath in profile_paths:
            await profiles.call_unregister_profile(ppath)
        await agents.call_unregister_agent(AGENT_PATH)
        bus.disconnect()
        print(f"Stopped; original name \"{old_alias}\" restored.")


def main(out_dir: Path, adapter: str = "hci0", discoverable_s: int = 300,
         serial: str = "EMFEMUL00001") -> int:
    try:
        asyncio.run(run(out_dir, adapter, discoverable_s, serial))
    except KeyboardInterrupt:
        pass
    return 0
