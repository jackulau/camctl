# camctl

Control your camera from the command line or from an AI assistant, with no vendor app. camctl is a CLI and an
[MCP](https://modelcontextprotocol.io) server that changes settings, applies presets, shoots, records, runs live
view, acts as a Bluetooth remote, and offloads cards to Google Drive with md5 verification.

It was built and verified on a **Sony A7 V (ILCE-7M5)**. Canon, Nikon, Fujifilm, Panasonic and OM System are
supported through libgphoto2 and published Bluetooth protocols, but are **untested on hardware**. Reports and fixes
from owners are welcome.

| Transport | What works |
|---|---|
| **USB-C (PTP)** | Every setting the camera exposes: presets, dump/load/diff config, capture, record, live view |
| **Wi-Fi (PTP/IP)** | Same as USB via `--port ptpip:<camera IP>` (Sony: PC Remote Cnct Method: Wi-Fi) |
| **Bluetooth LE** | Remote only: shutter, AF, record, bulb, zoom, focus, C1 (varies by brand). *No settings.* |
| **Card → Google Drive** | md5-verified resumable offload, dedup, sorted by year and type |

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- macOS is the main platform (it handles macOS's `ptpcamerad` grabbing the camera, and Bluetooth bond repair via
  `blueutil`). USB and Bluetooth control should also work on Linux; Windows is untested.
- libgphoto2 comes bundled with the `gphoto2` Python wheels on macOS and Linux. On macOS the raw Sony client
  (`config explain`) uses the libusb bundled with them; on Linux install libusb-1.0 from your package manager.
- Optional: [blueutil](https://github.com/toy/blueutil) (`brew install blueutil`) for stale Bluetooth bond repair;
  the Google Cloud CLI or a Google OAuth client for Drive offload.

## Install

```sh
git clone https://github.com/jackulau/camctl.git && cd camctl
uv sync
uv run camctl doctor --fix          # checks USB/BLE/Drive; frees the camera from macOS ptpcamerad
```

Use as an MCP server (Claude Code shown; any MCP client works):

```sh
claude mcp add camctl -- uv --directory /path/to/camctl run camctl-mcp
```

## Support matrix

**USB / Wi-Fi PTP.** The vendor is detected automatically. Presets use canonical keys, and each vendor profile
translates them.

| Brand | Status | Camera-menu setup for USB control |
|---|---|---|
| Sony | **verified** on A7 V fw 1.01 | MENU → Network → Transfer/Remote → PC Remote Function → PC Remote: On, Cnct Method: USB |
| Canon (EOS) | libgphoto2, untested on hardware | nothing to set; turn Wi-Fi/Bluetooth off (they block USB control) |
| Nikon | libgphoto2, untested on hardware | SETUP → USB → MTP/PTP; Z bodies: Airplane mode on |
| Fujifilm | libgphoto2, untested on hardware | SET UP → CONNECTION SETTING → PC CONNECTION MODE → USB TETHER SHOOTING AUTO |
| Panasonic LUMIX | libgphoto2, untested on hardware | SETUP → IN/OUT → USB Mode → PC(Tether) |
| OM System / Olympus | libgphoto2, untested on hardware | choose RAW/Control on the USB prompt (or USB Mode → RAW/Control) |

Settings a brand doesn't have over USB are reported as `unsupported`. That is information, not a failure: apply and
set still exit 0. Dial-owned or menu-only settings become on-camera checklist items. Locked settings say why (mode
dial, white balance not on Color Temp, movie-only setting, Fujifilm dials not on A, ...).

**Bluetooth LE remote** (`*` verified on hardware, `?` experimental):

| Brand | Commands | Source |
|---|---|---|
| Sony | shoot\*, record\*, autofocus, focus, zoom, bulb, AF-ON, C1, with shutter/focus/record feedback | verified on A7 V |
| Canon | shoot, autofocus, record, zoom? | Furble-confirmed protocol (BR-E1) |
| Fujifilm | shoot, autofocus, bulb? | Furble-confirmed protocol |
| Nikon | shoot, record?, bulb? | Furble-confirmed protocol (ML-L7) |
| GoPro | shoot, record (shutter feedback) | Open GoPro spec |
| Panasonic LUMIX | shoot?, record? | experimental |
| OM System | none | recognised in scans, unsupported (no public protocol) |

`camctl ble brands` prints this table from the code.

## Everyday use

```sh
camctl doctor                       # everything at once; exit 1 on problems; --ble-scan, --json
camctl status                       # vendor, model, battery, settings, card warnings, unsupported keys
camctl preset list
camctl preset apply photo           # stills: M, RAW+JPEG, 1/250, f/2.8, Auto ISO, AF-C
camctl preset apply cinematic-24    # video for grading: 4K24 10-bit 4:2:2, 1/50, 5600K
camctl set shutter 1/125 iso 800
camctl config dump                  # full backup → ~/.config/camctl/backups/
camctl config load <file> --dry-run
camctl config explain --filter movie   # Sony: decode every property the camera reports
camctl shoot -o captures/
camctl offload --dry-run            # card → Drive, md5 verified, sorted by year/type
camctl drive organize               # dry run: sort an existing archive; --apply to do it
camctl calc shutter 24              # 180° rule → 1/50
```

Built-in presets: `photo`, `photo-action`, `photo-group`, `cinematic-24`, `slowmo-60`, `easy-video`,
`event-ceremony`, `outdoor-cinematic`, `talking-head`, `low-light`, `best-photo`, `best-cine-24`,
`best-slowmo-60`, `best-slowmo-120`. Every apply backs up first and prints an on-camera checklist for what USB
can't set. Save your own with `camctl preset save NAME`.

`--json` works on every command (before or after the subcommand). Prompts and progress go to stderr.

### `camctl doctor`

Read-only. It checks three things:

- **USB:** whether a camera is connected, its vendor, model, firmware and battery, how many settings are writable,
  which are locked and why, which the brand doesn't support, and card warnings. With no camera, it runs the generic
  checks instead (data-capable cable, quit Image Capture/Photos/Imaging Edge, `--fix` stops ptpcamerad) and prints
  every vendor's USB setup.
- **Bluetooth:** remembered cameras, the daemon, and blueutil. `--ble-scan` also listens for cameras.
- **Drive:** whether auth works.

It exits 0 when everything is fine and 1 on problems (no camera, camera won't open, card full, Bluetooth scan
failing). Low battery, a nearly full card and missing Drive auth are warnings.

### Bluetooth

```sh
camctl ble scan                     # every brand: brand, supported, pairing mode (--no-all-brands, --only canon)
camctl ble pair                     # camera in pairing mode; press OK on the camera; verifies it survives reconnect
camctl ble shoot [--no-af] [--hold S] [--burst S]
camctl ble record [start|stop|toggle]
camctl ble bulb 30 | focus in|out | zoom tele|wide [--seconds S --speed N] | af-on | autofocus | c1
camctl ble status | devices | default ADDRESS | forget ADDRESS | brands
camctl ble unpair ADDRESS [--clear-bond --allow-bt-restart]
camctl ble serve                    # foreground daemon holding the link open
```

`--address` and `--brand` pick a camera. The default is the remembered default camera, else the best scan hit.

`ble shoot` reports `released`:

- `true`: the camera confirmed the shot.
- `false`: the camera reported it did *not* fire. Exits 1 with a hint such as "focus did not lock".
- unknown: the brand has no shutter feedback. Exits 0 and prints a hint.

`ble record` prints the camera's state, or a hint when the brand doesn't report it.

**Daemon:** `camctl ble serve` keeps one camera connected on `~/.config/camctl/ble.sock`, reconnecting with
backoff and sending GoPro keep-alives. Every other `ble` command and the MCP Bluetooth tools use it automatically,
which makes shots near-instant. `--no-daemon` connects directly.

**Stale bond repair:** if the camera forgot this Mac but macOS still holds the bond (CBError 14 "Peer removed
pairing information"), pairing fails. `camctl ble pair --allow-bt-restart` clears the bond with blueutil by
power-cycling Bluetooth. That briefly disconnects your other Bluetooth devices, so it only happens with the flag.
Without the flag, the error prints the exact command, or the System Settings → Bluetooth → Forget This Device step.

## MCP tools

- **Camera:** `camera_status` (vendor, every setting's value, writability, choices and locked reason, unsupported
  keys, card), `camera_doctor`, `set_settings`, presets, config dump/load/diff, `capture`, `record`,
  `liveview_jpeg`.
- **Bluetooth:** `ble_scan`, `ble_pair` (`allow_bt_restart` defaults to false), `ble_shoot`, `ble_record`,
  `ble_command` (status/bulb/autofocus/zoom/focus/af_on/c1), `ble_devices`, `ble_brands`.
- **Drive:** `offload`, `drive_organize`.
- **Other:** calculators, and the `camctl://guide` resource that teaches the client how to use the rest.

Bluetooth results carry the camera's prompts in `prompts`. The server never writes to stdout.

## Configuration

Everything lives in `~/.config/camctl/`. None of it is required.

| File | Purpose |
|---|---|
| `presets/*.yaml` | your own presets (`camctl preset save`) |
| `backups/` | config dumps, written before every preset apply |
| `ble.json` | remembered Bluetooth cameras |
| `organize.yaml` | Drive archive folder names (see below) |
| `sony_props.json` | optional property-name table for `config explain` (see below) |
| `drive-credentials.json`, `drive-token.json` | Google OAuth client and token |

Environment overrides: `CAMCTL_DRIVE_AUTH` (`gcloud` or `oauth`), `CAMCTL_DRIVE_TOKEN`,
`CAMCTL_DRIVE_CREDENTIALS`, `CAMCTL_ORGANIZE_CONFIG`, `CAMCTL_SONY_PROPS`.

**Property names.** `camctl config explain` decodes every property a Sony body reports, using camctl's own labels
for the settings it knows and hex codes for the rest. To see a name for every code, supply your own
`sony_props.json` mapping (`{"d157": {"name": "...", "values": {"1": "..."}}}`). No such table ships with camctl.

## Google Drive offload

**Auth.** camctl uses the active gcloud account if it has Drive scope (`gcloud auth login
--enable-gdrive-access`). Otherwise, put a Desktop OAuth client JSON (Drive API enabled) at
`~/.config/camctl/drive-credentials.json` and run `camctl drive login`. Offload never deletes from the card. It
prints `safe_to_format_card` only when every file is md5-verified on Drive.

**Layout.** `camctl offload` uploads into one archive folder, "Card Archive", already sorted. md5 dedup spans the
whole archive.

```text
<YYYY>/Photos, <YYYY>/Videos           phone and other photos/videos
<YYYY>/Camera/RAW|JPEG|Videos          camera DCIM/1xxXXXXX stills, Sony PRIVATE/M4ROOT clips with their XML
Documents/…                            card folders you choose to keep whole (subpaths kept)
Music/…                                audio, plus card folders you route there
Other/<card path>                      everything else
```

The year comes from EXIF, then a date in the file name, then the clip's XML, then the file's mtime.
`--layout dated` keeps the older `<date>/<card>/<path>` layout instead.

Choose which card folders are kept whole, and the music folder name, in `~/.config/camctl/organize.yaml`:

```yaml
keep_folders: [Documents, Projects]   # card top-level folders kept whole (case-insensitive)
music_folder: Music
music_sources: [Music, Audiobooks]    # card folders whose whole contents go to music_folder
```

Changing these on an existing archive changes what counts as already organized, so run a dry run first.

**Organizing an existing archive.** `camctl drive organize` sorts folders that older offloads left behind
(`<card>-<date>/…`, `<date>/<card>/…`) into this layout, and merges an older camera-only archive into it. Safety
rules:

- It is a dry run unless you pass `--apply`.
- Applying only moves files; nothing is re-uploaded.
- Duplicate copies (same md5) and emptied folders go to the Drive trash, recoverable for 30 days.
- Afterwards it re-checks that every file is present exactly once, and logs to
  `~/.config/camctl/offload-logs/organize-*.jsonl`.
- Re-running continues where it stopped, and it refuses to apply while an offload is running.

## Sony A7 V notes

**One-time menu setup.** These can't be set over USB on firmware 1.01:

1. **Log Shooting: On (Flexible ISO), Color Gamut: S-Gamut3.Cine.** The A7 V has no S-Log picture profiles, and
   the Log property codes aren't advertised over USB: writes are acknowledged, then ignored.
2. **Different Set for Still/Mv: tick Aperture, Shutter, ISO, Exp. Comp, WB.** Otherwise photo and movie share
   exposure and white balance, so a video preset overwrites your photo settings.
3. **Mode dial → M.** Remote exposure-mode changes last only for the session; once unplugged, the dial rules.

**Findings (verified on hardware):**

- **Remote exposure mode:** `500e` is read-only until priority key `d25a` = 1 (PC Remote). After that, writing a
  raw `500e` code works (M = 1, Movie (M) = 491603). The named gphoto2 `expprogram` widget fails with "Bad
  parameters". Closing the session reverts to the dial, and exposure widgets unlock about 0.5 s after a mode change.
- **Context locks:** `colortemperature` stays read-only until white balance is Color Temp. Movie SteadyShot
  (`d0da`) is read-only outside Movie mode. `status` and `doctor` say so instead of blaming the mode dial.
- **libgphoto2 2.5.34 quirks:**
  - A full `get_config()` right after a write can segfault, so camctl reopens the session and re-asserts
    `d25a`/`500e`.
  - libgphoto2 drops Sony's 0xE000-range properties, so `sonyptp.py` is a raw pyusb Sony client used by
    `config explain`: Connect 1/2 → GetExtDeviceInfo 0x12C → Connect 3, GetAllExtDevicePropInfo 0x9209,
    SetExtDevicePropValue 0x9205.
- **Capture** fails with `[-1] Unspecified error` when AF can't lock (lens cap on, too dark). Tethered captures
  download a 1616×1080 preview; the full RAW+JPEG is written to the card.
- **Card access:** the card isn't visible in PC Remote mode. To offload, switch USB Connection to Mass Storage. A
  USB 2.0 cable caps reads at about 27 MB/s, so use a USB 3 cable or a card reader.
- **Bluetooth:**
  - Service `8000FF00-FF00-FFFF-FFFF-FFFFFFFFFFFF`: write FF01, notify FF02.
  - Commands: half-press `0106/0107`, full press `0108/0109`, record `010E/010F`, AF-ON `0114/0115`, C1
    `0120/0121`, zoom `0244–0247` + step, focus `026A–026D` + step.
  - Status notifications: `02 3F|A0|D5 00|20`.
  - Pair once (MENU → Network → Bluetooth → Pairing, while `camctl ble pair` runs) and press OK on the camera.
    Otherwise the camera discards the bond at disconnect.

**Other vendors.** Their profiles handle these quirks, taken from libgphoto2 sources and untested on hardware:

- Canon: capturetarget → memory card; movie via live view plus `movierecordtarget`.
- Nikon: live view and movie only on P/A/S/M, with the reason read from `liveviewprohibit`/`movieprohibit`.
- Fujifilm: `prioritymode` → USB before exposure writes.
- Panasonic: only Creative Video P/A/S/M over USB.

## Development

```sh
uv sync
uv run pytest                       # ~640 tests; no camera, Bluetooth or network needed
uv run python scripts/live_check.py --capture   # hardware smoke test, camera on USB (writes one still)
```

The test suite fakes everything at the transport layer (gphoto2, bleak, blueutil, HTTP). A test that reaches real
hardware or the network fails instead.

## Credits

Non-Sony Bluetooth protocols come from [Furble](https://github.com/gkoh/furble) (MIT), thank you, and from GoPro's
[Open GoPro](https://gopro.github.io/OpenGoPro) spec (Apache-2.0). USB control is built on
[libgphoto2](http://www.gphoto.org/) through [python-gphoto2](https://github.com/jim-easterbrook/python-gphoto2).

## License

[MIT](LICENSE).

camctl is an independent project and is not affiliated with or endorsed by Sony, Canon, Nikon, Fujifilm,
Panasonic, OM Digital Solutions or GoPro. All product names and trademarks belong to their owners. Changing
camera settings and deleting or moving files is at your own risk. Keep backups.
