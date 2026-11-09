# Canon Zoemini 2 / Ivy 2 — Bluetooth protocol

This document describes the Bluetooth protocol spoken by the Canon Zoemini 2 / Ivy 2 (model PV-223) mini photo printer, as implemented by the `zoemini` package. It covers what is needed to query the printer, change its settings and print an image. Points that are still uncertain are marked **(uncertain)** and listed in §10.

All multi-byte integers are **big-endian**. Byte offsets start at 0.

---

## 1. Transport

| Item | Value |
|---|---|
| Radio | Classic Bluetooth (BR/EDR), not BLE |
| Profile | **RFCOMM**, Serial Port Profile, **channel 1** |
| Advertised name | `Canon (XX:XX) Mini Printer`, where `XX:XX` are the last two bytes of the Bluetooth address |
| Device class | `0x100680` (imaging / printer) |
| Pairing | Secure Simple Pairing, *Just Works* (no PIN) |
| Connections | **One at a time**: the printer cannot be used by two hosts simultaneously |
| Exchange model | **Strict request / response**: send one command, wait for its response, then send the next one |
| Response delay | Typically 0.1 to 0.7 s. Use a timeout of several seconds per command |
| Disconnection | The printer closes the link by itself about **20 s after a print** has finished. Reconnect before the next operation |

### 1.1 SDP services

The printer publishes four RFCOMM services. Only the first one carries the printing protocol.

| RFCOMM channel | UUID | Service name |
|---|---|---|
| **1** | `00001101-0000-1000-8000-00805f9b34fb` (SPP) | `SerialPort` |
| 22 | `00000000-deca-fade-deca-deafdecacaff` | `Apple Inc.` |
| 23 | `e5b152ed-6b46-09e9-4678-665e9a972cbc` | `WeChat` |
| 25 | `00500056-0223-0000-0000-000000000000` | `FSK` |

The inquiry response also carries the 128-bit UUID `00000000-deca-fade-deca-deafdecacaff`. Some hosts list it byte-reversed (`ffcacade-afde-cade-defa-cade00000000`); this is not a separate service.

The official Canon app recognises a printer by its **name** (pattern `^Canon\s(.*?)\s(Mini Printer|Instant Camera)$`) and selects the protocol described here from the **SDP services**: a device that publishes only SPP is treated as a first-generation model, which uses a different protocol (not covered here). A device emulating the printer must therefore publish all four services.

---

## 2. Frame format

Commands (host → printer) and responses (printer → host) are **34-byte frames** sharing the same 8-byte header. Unused bytes are `0x00`.

| Bytes | Field | Command | Response |
|---|---|---|---|
| 0–1 | start code | `43 0F` | **`43 F0`** |
| 2–3 | device type | `00 01`; `FF FF` for START_SESSION | `00 01` |
| 4 | device code | `20`; `FF` for START_SESSION | product code of the printer (§8) |
| 5–6 | command | command code (§3) | code of the command being answered |
| 7 | access / status | `00` = read (GET), `01` = write (SET) | **error code** (§7) |
| 8–33 | payload | 26 bytes | 26 bytes |

Notes:

- Every response is exactly 34 bytes. A receiver should accumulate bytes from the RFCOMM stream and **synchronise on `43 F0`** to delimit frames.
- In the rest of this document, `p[n]` designates byte `n` of the payload, i.e. byte `8 + n` of the frame.

---

## 3. Commands

| Code | Name | Direction | Purpose |
|---|---|---|---|
| `0x0000` | START_SESSION | host → printer | open a session; returns battery and MTU |
| `0x0101` | STATUS | host → printer | battery, state, flags, print progress |
| `0x0102` | GET_INFO | host → printer | Bluetooth address, serial number, model |
| `0x0103` | SETTING | host → printer | read (GET) or write (SET) the settings |
| `0x0201`–`0x0203` | firmware update | — | not documented; **must not be sent** |
| `0x0301` | PRINT_READY | host → printer | announce an image and its size |
| `0x0302` | PRINT_RECEIVE_DATA_ACK | printer → host | sent once the whole image has been received |
| `0xFFFF` | REBOOT | host → printer | restart the printer |

Each response carries the code of the command it answers, except `0x0302`, which the printer sends on its own after the image transfer.

### 3.1 START_SESSION — `0x0000`

```
43 0F FF FF FF 00 00 00  00 … 00
```

Response:

| Payload | Field | Decoding |
|---|---|---|
| p[0] | unknown | `0x01` on all units seen **(uncertain: protocol version?)** |
| p[1–2] | power word | same layout as in STATUS (§3.2) |
| p[3–4] | **MTU** | uint16: maximum size of an image chunk (§4.2). The Zoemini 2 returns `0xFFFF` |

### 3.2 STATUS — `0x0101`

```
43 0F 00 01 20 01 01 00  00 … 00
```

Response:

| Payload | Field | Decoding |
|---|---|---|
| p[0–1] | power word `w` (uint16) | **bits 0–6**: battery level in % (`w & 0x7F`); **bit 7**: 1 = on mains power / charging, 0 = on battery; **bits 8–15** (= p[0]): battery voltage, raw unit **(uncertain)** |
| p[2] | error code | §7 |
| p[3] | queue status | §3.2.1 |
| p[4–5] | queue flags (uint16) | §3.2.2 |
| p[6] | print progress | int8, in %: 25, 50, 75, 100 during a print; stays at 100 once back to READY |

#### 3.2.1 Queue status

| Value | Name | Meaning |
|---|---|---|
| `0x00` | READY | idle; if a print was in progress, it is finished |
| `0x01` | PRINT_PREPARATION | preparing a print; can also appear briefly **after** printing, before READY |
| `0x04` | PRINT_PRINTING | printing |
| `0x10` | PAPER_JAM | paper jam |
| `0xF0` | TRANSIENT_ERROR | transient error (see error code p[2]) |
| `0xF1` | RECOVERABLE_ERROR | recoverable error (see error code p[2]) |

#### 3.2.2 Queue flags

| Bit | Meaning |
|---|---|
| 0 | cover open |
| 1 | out of paper |
| 2 | wrong paper |
| 3 | calibration sheet (blue *Smart Sheet*) being read |
| 4 | calibration sheet error |
| 5–15 | unused **(uncertain)** |

These conditions are reported through the flags only: an open cover or a missing paper does **not** set the error code p[2] while the printer is idle. A host should check the flags before announcing a print.

When paper is loaded with a new pack, the printer first feeds the blue calibration sheet on its own; bit 3 stays set for about 35 s. Do not start a print while it is set.

### 3.3 GET_INFO — `0x0102`

```
43 0F 00 01 20 01 02 00  00 … 00
```

Response:

| Location | Field | Decoding |
|---|---|---|
| frame byte 4 | product code | §8 |
| p[0–5] | Bluetooth address | 6 bytes, most significant first |
| p[6] | `n` | length of the serial number |
| p[7 … 6+n] | serial number | UTF-8 string, 12 characters on the units seen. Its 3rd character encodes the body colour (§8.1) |
| p[7+n] | `m` | length of the model string |
| p[8+n … 7+n+m] | model | UTF-8 string: `PV223` |

Both strings share the 26-byte payload: `n + m ≤ 18`.

### 3.4 SETTING — `0x0103`

Read:

```
43 0F 00 01 20 01 03 00  00 … 00
```

Write (auto power-off delay):

```
43 0F 00 01 20 01 03 01  MM 00 … 00
```

`MM` = delay in minutes. Supported values: **3, 5, 10**. The rest of the payload is zero.

Response to a read **or** a write (a write returns the full settings with the new value already applied):

| Payload | Field | Decoding |
|---|---|---|
| p[0] | auto power-off delay | minutes |
| p[1–3] | firmware version | three bytes `a b c`, displayed `a.b.c` (e.g. `01 04 0D` → 1.4.13) |
| p[4] | unknown | `0x05` on the units seen **(uncertain)** |
| p[5] | TMD version | displayed `0.0.x` (e.g. `0x18` → 0.0.24) |
| p[6–7] | photos printed | uint16; calibration sheets are not counted |
| p[8] | colour ID | `0` on the units seen; the body colour is given by the serial number instead (§8.1) |
| p[9–10] | unknown | uint16, increases slowly with use **(uncertain: calibration counter?)** |

The auto power-off delay is stored by the printer and kept across power cycles. No separate commit command is needed.

### 3.5 PRINT_READY — `0x0301`

```
43 0F 00 01 20 03 01 00  S3 S2 S1 S0 01 01 00 … 00
```

| Payload | Field |
|---|---|
| p[0–3] | size of the JPEG that follows, in bytes (uint32) |
| p[4] | `0x01` **(uncertain: number of copies?)** |
| p[5] | `0x01` **(uncertain: print mode?)** |

The response's byte 7 is the error code (§7): a non-zero value means the print is refused. Its payload has the same layout as the STATUS response.

### 3.6 PRINT_RECEIVE_DATA_ACK — `0x0302`

Sent by the printer, unsolicited, right after the last byte of the image. Byte 7 is the error code (`0x00` = image accepted); the payload has the same layout as the STATUS response. This frame acknowledges **reception**, not the end of printing.

### 3.7 REBOOT — `0xFFFF`

```
43 0F 00 01 20 FF FF 01  01 00 … 00
```

---

## 4. Print sequence

```
host                                       printer
 │── RFCOMM connection, channel 1 ────────────▶│
 │── START_SESSION ───────────────────────────▶│
 │◀───────────────────── response (MTU, battery)│
 │── STATUS ──────────────────────────────────▶│  check flags and state
 │◀──────────────────────────────── response ──│
 │── PRINT_READY (JPEG size) ─────────────────▶│
 │◀──────────────────── response (error code) ─│
 │── JPEG bytes, raw, in chunks ──────────────▶│  no header, no per-chunk ack
 │◀─────────────── PRINT_RECEIVE_DATA_ACK ─────│  image received
 │── STATUS (every 2–5 s) ────────────────────▶│
 │◀───── response: queue status, progress ─────│  … until READY
 │            (link closed by the printer ≈ 20 s later)
```

### 4.1 Before printing

Check the STATUS response: queue status READY, error code `0x00`, and flags 0–4 all clear. Wait while bit 3 (calibration) is set.

Reading the settings or the device information before printing is not required.

### 4.2 Image transfer

- Starts immediately after a successful PRINT_READY response.
- The JPEG is sent **as is**, with no framing, as consecutive chunks of at most **MTU** bytes (from START_SESSION). Without a known MTU, use 650-byte chunks.
- Chunks are written back to back, with no delay and without waiting for any acknowledgement; flow control is left to RFCOMM.
- With the `0xFFFF` MTU of the Zoemini 2, the whole JPEG may be written at once. `zoemini` uses 4,096-byte chunks to report progress.
- Throughput is about 100 KB/s: a 1 MB image takes about 10 s.
- The printer answers with PRINT_RECEIVE_DATA_ACK (`0x0302`) a fraction of a second after the last byte.

### 4.3 Following the print

The printer sends no end-of-print message. Poll STATUS every 2 to 5 seconds:

| Queue status | Action |
|---|---|
| PRINT_PREPARATION, PRINT_PRINTING | in progress; p[6] gives the progress |
| READY (after a print has started) | **finished** |
| PAPER_JAM | failed |
| TRANSIENT_ERROR, RECOVERABLE_ERROR | error, see p[2] |

A print takes about 50 s after the end of the transfer. If the link drops after the progress has reached 100 %, the print can be considered finished.

---

## 5. Image format

The printer expects a JPEG prepared to its exact geometry. It does no scaling, rotation or colour processing of its own.

### 5.1 Geometry

| Parameter | Value |
|---|---|
| JPEG size | **640 × 1608** pixels (width × height), portrait |
| Content area | **634 × 1596** pixels, centred: 3 px margin left and right, 6 px top and bottom |
| Pixel aspect | **non-square**: the vertical resolution is 1.675 × the horizontal one (1608 × 2 / (640 × 3)) |
| Print size | 2 × 3" (ZINK paper), about 320 dpi across × 536 dpi along |
| Orientation | the image is **rotated by 180°** in the JPEG |

To fill the print, crop the source image to a physical ratio of 634 : (1596 / 1.675), i.e. about 2 : 3, then resize it **without keeping proportions** to 634 × 1596.

### 5.2 Edge filling

After the 180° rotation, the image is shifted and its edges are filled, to obtain a bleed at the top and bottom of the print:

1. remember content rows 6 (first) and 1601 (last);
2. shift the whole image 4 rows down;
3. fill rows 0–9 with the remembered first row;
4. fill rows 1604–1607 with the remembered last row.

The 3 white columns on each side stay white.

### 5.3 Encoding

| Property | Value |
|---|---|
| Type | baseline JPEG (not progressive), JFIF |
| Colour | RGB, no alpha, no ICC profile, no EXIF |
| Chroma subsampling | **4:4:4** (none) |
| Quality | 90 |

JPEGs of more than 1 MB are accepted.

### 5.4 Recipe

1. crop the source to about 2 : 3 (physical ratio of the content area), centred;
2. resize to 634 × 1596, ignoring proportions;
3. paste on a white 640 × 1608 canvas at (3, 6);
4. rotate by 180°;
5. fill the edges (§5.2);
6. encode as baseline JPEG, quality 90, 4:4:4.

ZINK paper strongly compresses shadows and highlights: an unprocessed image prints with blocked shadows. A tone curve that lifts shadows and midtones, and light sharpening, give results closer to the official app. This is a rendering choice, not part of the protocol.

---

## 6. Typical frames

```
START_SESSION   43 0F FF FF FF 00 00 00  00 …
  response      43 F0 00 01 21 00 00 00  01 82 64 FF FF 00 …     battery 100 %, MTU 0xFFFF
STATUS          43 0F 00 01 20 01 01 00  00 …
  response      43 F0 00 01 21 01 01 00  81 E4 00 00 00 00 00 …  mains, 100 %, READY, no flag
SETTING (read)  43 0F 00 01 20 01 03 00  00 …
  response      43 F0 00 01 21 01 03 00  03 01 04 0D 05 18 00 0B 00 00 02 …
                                         3 min, firmware 1.4.13, TMD 0.0.24, 11 photos
PRINT_READY     43 0F 00 01 20 03 01 00  00 11 AA 0C 01 01 00 …  JPEG of 1,157,644 bytes
```

---

## 7. Error codes

Used in byte 7 of the responses and in p[2] of STATUS.

| Code | Name | Phase |
|---|---|---|
| `0x00` | NO_ERROR | — |
| `0x11` | BUSY | before printing |
| `0x12` | HIGH_TEMPERATURE | before printing |
| `0x13` | LOW_TEMPERATURE | before printing |
| `0x14` | BATTERY_TOO_LOW | before printing |
| `0x15` | OPEN_COVER | before printing |
| `0x16` | PAPER_EMPTY | before printing |
| `0x17` | WRONG_SMART_SHEET | before printing |
| `0x21` | PRINT_DATA_ERROR | while printing |
| `0x22` | PRINT_OPEN_COVER | while printing |
| `0x23` | PRINT_PAPER_EMPTY | while printing |
| `0x24` | PRINT_WRONG_SMART_SHEET | while printing |
| `0x25` | CAMERA_WRITE_ERROR | camera-printers only |

`0x1X` codes are conditions that prevent a print from starting; `0x2X` codes are errors during printing. A paper jam has no error code: it is reported by queue status `0x10`.

---

## 8. Device identification

The product code is byte 4 of every response.

| Product code | Device |
|---|---|
| `0x20`, `0x21`, `0x22` | PV-223 family (Ivy 2 / Zoemini 2). **The Zoemini 2 answers `0x21`**. `0x20` and `0x22` are other revisions of the same family **(which commercial models: uncertain)** |

All three codes use the same protocol and the same image format. The GET_INFO model string is `PV223`.

The same protocol family is shared with Canon's camera-printers (Zoemini S2 / Ivy Cliq 2), which explains error code `0x25`; these devices are not covered here.

### 8.1 Body colour

The 3rd character of the serial number gives the body colour (used by the official app to choose the printer's icon):

| Letter | Colour |
|---|---|
| A | black / pink / rose gold |
| B | pearl white |
| C | blue |
| D | rose gold and white |
| E | pearl white and silver |
| F | navy and white |

---

## 9. Firmware update

Commands `0x0201` to `0x0203` update the printer's firmware. They are not documented here and must **never** be sent: a wrong sequence could make the printer unusable. The `zoemini` emulator refuses them.

---

## 10. Open questions

- START_SESSION p[0] (`0x01`): meaning unknown.
- STATUS p[0]: assumed to be the raw battery voltage (decreases with use), unit unknown.
- PRINT_READY p[4] and p[5] (`01 01`): meaning unknown; other values untested.
- SETTING p[4] and p[9–10]: meaning unknown. Setting bytes after p[0] in a write: unused.
- Queue flags bits 5–15: never seen set.
- Maximum accepted JPEG size; acceptance of a progressive JPEG, of other dimensions, or of an ICC profile: untested. A rejected image is expected to return `PRINT_DATA_ERROR` (`0x21`).
- Several prints in the same session, without reconnecting: untested.
- MTU `0xFFFF`: presumably means "no limit".
- Behaviour on low battery, and when the cover is opened during a print: untested.
