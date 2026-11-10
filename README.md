# zoemini

Print on a **Canon Zoemini 2** mini photo printer over Bluetooth from Linux, without the official app, and **emulate** the printer for testing.

The Zoemini 2 is sold as the **Canon Ivy 2** in the United States and as the **Canon iNSPiC PV-223** in Japan; all three are the same device (model PV-223), printing 2 × 3" photos on ZINK paper. The code should therefore work with any of them; it has been tested with a Zoemini 2.

The project contains:

- `zoemini`, a command-line tool and Python package that queries the printer, changes its settings and prints images;
- a **CUPS** filter, backend and PPD, so that the printer can be used as an ordinary print queue (`lp`, applications' print dialogs);
- an **emulator** that makes a Linux computer appear as a Zoemini 2, so that the official Canon Mini Print app (or this package) can "print" to it; every received image is saved as a JPEG;
- [`protocol.md`](protocol.md), a description of the Bluetooth protocol.

This project started from [**ivy2**](https://github.com/dtgreene/ivy2) by dtgreene, a Python library for the Canon Ivy 2. `zoemini` is an independent implementation, written from scratch, that fixes several points of the protocol and of the image format (battery level, image geometry, end-of-print tracking).

Not affiliated with or endorsed by Canon. "Canon", "Zoemini", "Ivy" and "iNSPiC" are trademarks of their owners.

**Requirements**: Linux with BlueZ, a Bluetooth adapter, Python ≥ 3.11 and Pillow. The emulator also needs `dbus-fast`; the CUPS queue needs CUPS and `pdftoppm` (poppler-utils).

Tested with the procedures below and no changes:

| Machine | Bluetooth adapter | OS | Architecture | BlueZ | Python | Tested |
|---|---|---|---|---|---|---|
| Raspberry Pi Zero 2 W | built-in | Raspberry Pi OS Lite 64-bit (Debian 13) | aarch64 | 5.82 | 3.13.5 | printing, emulator |
| Raspberry Pi 4 | built-in | Raspberry Pi OS Lite 64-bit (Debian 13) | aarch64 | 5.82 | 3.13.5 | printing, emulator |
| PC | Intel BE200 (USB `8087:0036`) | Debian GNU/Linux 13 (trixie) | x86_64 | 5.82 | 3.13.5 | printing, emulator, CUPS 2.4.10 |
| Raspberry Pi 5 | built-in | Raspberry Pi OS 64-bit (Debian 13) | aarch64 | 5.82 | 3.13.5 | printing, CUPS 2.4.10 shared on the network (clients: Windows 11, Android) |

| File | Role |
|---|---|
| `src/zoemini/protocol.py` | frames, reassembly, decoding (no I/O) |
| `src/zoemini/image.py` | image preparation to the printer's format |
| `src/zoemini/transport.py` | RFCOMM socket (standard library) |
| `src/zoemini/printer.py` | session, status, settings, print sequence and progress |
| `src/zoemini/cli.py` | `zoemini` command |
| `src/zoemini/cups_filter.py` | CUPS filter: PDF or image → printer JPEGs |
| `src/zoemini/cups_backend.py` | CUPS backend: `zoemini://` URIs, sends the JPEGs over Bluetooth |
| `src/zoemini/emulator.py` | emulated printer, independent of Bluetooth |
| `src/zoemini/bluez_emulator.py` | publication of the emulator through BlueZ (D-Bus) |
| `cups/zoemini2.ppd` | PPD of the print queue |
| `cups/zoemini.types` | MIME type produced by the filter |
| `protocol.md` | protocol description |

---

## 1. Printing

### 1.1 Installation

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install .
zoemini --help
```

No root access and no group membership are needed to print.

### 1.2 Bluetooth

Make sure the Bluetooth adapter is unblocked and powered (on Raspberry Pi OS Lite it is blocked by default):

```bash
sudo rfkill unblock bluetooth
bluetoothctl power on
```

Switch the printer on, **close the Canon app** on any phone (the printer accepts one connection at a time), then find and pair the printer once:

```bash
bluetoothctl --timeout 15 scan on | grep -i "mini printer"     # Canon (XX:XX) Mini Printer
bluetoothctl --agent NoInputNoOutput pair <address>
bluetoothctl trust <address>
```

Pairing is *Just Works*: there is no PIN to enter.

### 1.3 Usage

The printer's address is given with `-a/--address` or the `ZOEMINI_ADDRESS` variable:

```bash
export ZOEMINI_ADDRESS=<address>

zoemini status                       # model, firmware, battery, paper, state
zoemini test-pattern                 # print a test pattern
zoemini print photo.jpg              # fill the 2×3 print (centred crop)
zoemini print photo.jpg --fit        # whole image, with white bands
zoemini set --auto-off 5             # auto power-off delay: 3, 5 or 10 min
```

Example of `zoemini status`:

```
Model         PV223 (product code 0x21)
Serial no.    …
MAC           …
Firmware      1.4.13    TMD 0.0.24
Photos        16
Auto off      10 min
Battery       100 %
State         READY, error NO_ERROR
Flags         none
Ready         yes
```

Options of `print` and `test-pattern`:

| Option | Effect |
|---|---|
| `--fit` | (`print` only) keep the whole image, add white bands instead of cropping |
| `--no-rotate` | (`print` only) do not rotate landscape images (rotated to portrait by default) |
| `--raw` | no tone curve and no sharpening |
| `--dry-run` | prepare the image without printing |
| `--save-jpeg FILE` | save the JPEG sent to the printer |
| `--preview FILE.png` | save an upright preview at the print's proportions |

General options: `-v/--verbose` shows the exchanged frames; `--channel` sets the RFCOMM channel (1 by default).

```bash
zoemini print photo.jpg --dry-run --preview preview.png    # check the framing without printing
```

![Test pattern printed on a Zoemini 2](test-pattern_zoemini2.jpg)
*Test pattern printed with `zoemini test-pattern`.*


### 1.4 Behaviour

- Before printing, `zoemini` checks the printer's state and refuses to print if the cover is open, the paper is missing or wrong, or the printer is busy. If the blue calibration sheet is being fed (new paper pack), it waits for the end of the calibration.
- Progress is displayed during the transfer (about 10 s) and the print (about 50 s).
- About 20 s after a print, the printer closes the Bluetooth link: this is normal. Each `zoemini` command opens its own connection.
- By default, images get a mild tone curve (shadows lifted according to the image's brightness) and sharpening, to compensate for the ZINK paper. `--raw` disables it.
- Exit codes: `0` success, `1` printing refused or failed, `2` connection error.

### 1.5 Python API

```python
from PIL import Image
from zoemini import image
from zoemini.printer import Printer

jpeg = image.prepare(Image.open("photo.jpg"))          # mode="fit", enhanced=False, …
with Printer("<address>") as p:
    print(p.status().power.battery, "%")
    p.print_jpeg(jpeg, progress=lambda stage, pct: print(stage, pct))
```

---

## 2. CUPS print queue

The printer can be set up as a CUPS queue: printing then works from `lp` and from applications' print dialogs, and, if the queue is shared, from other computers on the network.

- The **filter** converts the job (PDF, JPEG, PNG, TIFF or GIF; other formats are converted to PDF by CUPS first) into one printer JPEG per page and per copy. PDF pages are rasterised with `pdftoppm`.
- The **backend** sends the JPEGs to the printer over Bluetooth. If the printer is switched off, out of range, out of paper or has its cover open, the job **waits** and is retried every 10 s: switch the printer on (or load paper) and printing starts by itself. The reason is shown in the queue's state.

### 2.1 Installation

CUPS runs filters and backends as the `lp` user, which usually cannot read a home directory: install the package in a system-wide venv. CUPS and `pdftoppm` must be installed (`sudo apt install cups poppler-utils` on Debian). From this folder:

```bash
sudo python3 -m venv /opt/zoemini
sudo /opt/zoemini/bin/pip install .
sudo install -m 755 /opt/zoemini/bin/zoemini-cups-backend /usr/lib/cups/backend/zoemini
sudo install -m 755 /opt/zoemini/bin/zoemini-cups-filter /usr/lib/cups/filter/zoemini
sudo install -m 644 cups/zoemini.types /usr/share/cups/mime/
sudo systemctl restart cups
cupstestppd cups/zoemini2.ppd                # must print PASS
```

The printer must be paired first (§1.2). No root access is needed at print time: the backend runs as `lp`.

To update the package later, run `sudo /opt/zoemini/bin/pip install --force-reinstall --no-deps .`; the scripts in `/usr/lib/cups/` do not need to be copied again.

### 2.2 Creating the queue

```bash
sudo lpadmin -p Zoemini -E -v zoemini://XX-XX-XX-XX-XX-XX -P cups/zoemini2.ppd -D "Canon Zoemini 2"
```

The URI is the printer's Bluetooth address with `-` separators. `sudo /usr/lib/cups/backend/zoemini` (or `sudo lpinfo -v`; on Debian, `lpinfo` is in `/usr/sbin`) lists the paired Zoemini printers with their URI. CUPS 2.4 warns that printer drivers are deprecated: this is expected, PPD-based queues still work.

URI options, added as a query string (`zoemini://XX-XX-XX-XX-XX-XX?wait=600`):

| Option | Effect |
|---|---|
| `wait=SECONDS` | stop waiting for the printer after this time and let CUPS retry the job later (0, the default: wait as long as needed) |
| `channel=N` | RFCOMM channel (1 by default) |

### 2.3 Printing

```bash
lp -d Zoemini photo.jpg
lp -d Zoemini -o ZoeminiScaling=Fit document.pdf
lpstat -l -p Zoemini                         # state, progress, waiting reason
```

Queue options (`lp -o`, or the defaults with `lpadmin -p Zoemini -o ZoeminiScaling-default=Fit`):

| Option | Values | Effect |
|---|---|---|
| `ZoeminiScaling` | `Fill` (default), `Fit` | fill the print with a centred crop, or keep the whole page with white bands |
| `ZoeminiEnhance` | `On` (default), `Off` | tone curve and sharpening, as in §1.4; `Off` is the raw rendering |

- The only page size is 2 × 3". Pages of another size are scaled; landscape pages are rotated to portrait.
- Every page of a PDF gives one print, in order; copies repeat the whole document. `page-ranges` and `number-up` are not supported.
- A print cannot be stopped once its transfer has started: cancelling a job takes effect after the current print.

### 2.4 Sharing on the network

CUPS can share the queue and announce it on the local network (mDNS / DNS-SD, with Avahi). Computers and phones then find it without any driver: the queue is announced as Mopria- and AirPrint-compatible, and CUPS converts what they send (PDF, PWG raster, Apple raster…) before the zoemini filter.

On the computer that holds the queue:

```bash
sudo apt install cups-filters avahi-daemon avahi-utils
sudo cupsctl --share-printers
sudo lpadmin -p Zoemini -o printer-is-shared=true
sudo systemctl restart cups
avahi-browse -rt _ipp._tcp | grep -A3 -i zoemini       # "Canon Zoemini 2 @ <hostname>"
```

`cups-filters` provides the conversions of the formats sent by Windows and phones; `avahi-utils` is only needed for the `avahi-browse` check.

**Windows 10 / 11**: Settings → Bluetooth & devices → Printers & scanners → Add device. The printer appears as `Canon Zoemini 2 @ <hostname>`; otherwise, "Add manually" → "Select a shared printer by name": `http://<host address>:631/printers/Zoemini`. Windows uses its built-in IPP driver. As the queue has a single page size, the print dialog shows no paper size choice. In the Photos app, the list of sizes (full page, 4 × 6", …) is the size of the photo on the page: choose "Full page".

**Android**: Settings → search for "Printing" → turn on the **Default Print Service**; the printer appears after a few seconds. From an app (e.g. Google Photos: ⋮ → Print), choose the printer. Android does not list the queue's 2 × 3" size (no standard IPP name exists for it, so CUPS publishes it as `custom_2x3in_2x3in`) and offers its own list instead: choose **10 × 15** (4 × 6"), which has the same 2:3 ratio and prints identically. A4 or Letter pages are cropped (Fill) or get white bands (Fit).

iOS (AirPrint) has not been tested.

### 2.5 Troubleshooting

| Symptom | Remedy |
|---|---|
| The job stays at "Waiting for the printer" | the printer is off, out of range, or connected to a phone: switch it on, close the Canon app |
| The job stays at "Pairing lost: pair the printer again" | the printer has forgotten this computer's pairing (it may keep only a few pairings, and pairing it with another device can drop this one). Pair it again: `bluetoothctl remove <address>`, then pair and trust as in §1.2. The job waits meanwhile and starts by itself |
| "Printer not ready: out of paper" / "cover open" | load paper or close the cover; the job resumes by itself |
| The job fails with "Bluetooth unavailable" | the adapter is off or blocked: `sudo rfkill unblock bluetooth`, `bluetoothctl power on` |
| Nothing happens and no message | `sudo tail -50 /var/log/cups/error_log`; more detail with `sudo cupsctl --debug-logging` |

Manual test of the backend, without CUPS:

```bash
DEVICE_URI=zoemini://XX-XX-XX-XX-XX-XX /opt/zoemini/bin/zoemini-cups-backend 1 user title 1 "" photo.jpg
```

Debug logging, once the problem is solved, is turned off with `sudo cupsctl --no-debug-logging`.

### 2.6 Uninstalling

Remove the queue (pending jobs are cancelled first):

```bash
cancel -a Zoemini
sudo lpadmin -x Zoemini
```

To also remove the filter, the backend and the package (only once no other queue uses the `zoemini://` backend):

```bash
sudo rm /usr/lib/cups/backend/zoemini /usr/lib/cups/filter/zoemini /usr/share/cups/mime/zoemini.types
sudo rm -rf /opt/zoemini
sudo systemctl restart cups
sudo lpinfo -v | grep zoemini                # must print nothing
```

`/etc/cups/printers.conf.O` may still mention the queue: it is the backup CUPS makes of `printers.conf` before each change, and it is overwritten at the next one. The same goes for any `*.O` file in `/etc/cups/ppd/`.

If the queue was shared (§2.4) and no other printer needs to be: `sudo cupsctl --no-share-printers`. To forget the printer on the Bluetooth side as well: `bluetoothctl remove <address>`. The packages installed with `apt` (CUPS, poppler-utils, cups-filters, Avahi) are left in place.

---

## 3. Emulator

The emulator makes the Linux computer present itself as a Zoemini 2: same Bluetooth name format, same services, same responses. The Canon Mini Print app can add it, connect to it and print to it; each image it sends is saved as a JPEG, exactly as the printer would receive it. It can also be used to test `zoemini` itself without paper.

The emulator does not print anything and refuses firmware update commands.

### 3.1 Installation

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install '.[emulator]'          # adds dbus-fast
```

### 3.2 BlueZ configuration

The app pairs twice and needs pairing settings that are weaker than BlueZ's defaults. In `/etc/bluetooth/main.conf`, section `[General]`:

```ini
JustWorksRepairing = always
Class = 0x000680
```

- `JustWorksRepairing = always` is required: otherwise BlueZ refuses the app's second pairing.
- `Class = 0x000680` (imaging / printer) is optional.

**Secure Connections** must also be off, otherwise the phone gives up pairing. With BlueZ 5.82, the `SecureConnections = off` key of `main.conf` is not enough; use `btmgmt` after the service has restarted:

```bash
sudo systemctl restart bluetooth
sudo btmgmt sc off
btmgmt info | grep "current settings"       # must not contain secure-conn
```

`btmgmt sc off` is lost at every restart of the Bluetooth service or of the computer.

These settings are only for the emulator: printing (§1, §2) works with BlueZ's default configuration.

### 3.3 Running

1. **Switch the real printer off.**
2. Start the emulator:
   ```bash
   zoemini emulate --out received
   ```
   ```
   Emulator running: "Canon (XX:XX) Mini Printer" (…), discoverable 300 s, serial EMFEMUL00001.
   ```
3. On the phone, within the discoverable period: open the Mini Print app, add the printer `Canon (XX:XX) Mini Printer` (the last two bytes of the computer's Bluetooth address), accept the pairing request, and print a photo.
4. The received images are written to `received/app_<date>_<time>.jpg`.
5. Stop with **Ctrl+C**: the adapter's original name is restored.

Options:

| Option | Effect |
|---|---|
| `--out DIR` | folder for the received JPEGs (`captures/emulator` by default) |
| `--visible SECONDS` | discoverable duration (300 by default) |
| `--adapter NAME` | Bluetooth adapter (`hci0` by default) |
| `--color A…F` | announced body colour, shown as the printer's icon in the app: A black/pink/rose gold, B pearl white, C blue, D rose gold/white, E pearl white/silver, F navy/white (default) |
| `--serial TEXT` | full serial number (13 characters at most; overrides `--color`) |
| `-v` | show every exchanged frame |

The received JPEGs are 640 × 1608 pixels, rotated by 180° and with non-square pixels (see `protocol.md` §5). An upright preview can be obtained with `zoemini.image.preview(jpeg_bytes)`.

### 3.4 After use

While it runs, the emulator makes the computer discoverable and **accepts any pairing**, with weakened security settings. Use it only for the duration of the tests, then restore the normal configuration:

```bash
# remove JustWorksRepairing = always (and Class) from /etc/bluetooth/main.conf, then:
sudo systemctl restart bluetooth
sudo btmgmt sc on
```

### 3.5 Troubleshooting

| Symptom | Remedy |
|---|---|
| The phone gives up pairing | Secure Connections still on: `sudo btmgmt sc off` |
| BlueZ refuses the second pairing | `JustWorksRepairing = always` missing from `main.conf` |
| The app no longer finds the emulator, while Android lists it | restart the phone |
| An old `Canon (XX:XX) Mini Printer` entry on the phone | remove it in Android's Bluetooth settings and pair again |
| The emulator was stopped abruptly and the computer keeps the Canon name | `bluetoothctl system-alias "$(hostname)"` |

---

## Protocol

See [`protocol.md`](protocol.md): transport, frame format, commands, print sequence, image format, error codes, and remaining open questions.

## License

MIT, see `LICENSE`.
