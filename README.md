# loxklipper

[![Klipper](https://img.shields.io/badge/Klipper-extras%20module-ff6600.svg)](#)
[![Moonraker](https://img.shields.io/badge/Moonraker-auto--update-2b8a3e.svg)](#)
[![Loxone](https://img.shields.io/badge/Loxone-Miniserver-69c350.svg)](#)
[![Python](https://img.shields.io/badge/python-3.7%2B-3776ab.svg?logo=python&logoColor=white)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Donate](https://img.shields.io/badge/Donate-PayPal-00457C.svg?logo=paypal)](https://www.paypal.com/donate/?hosted_button_id=6CDEVZGJWTNQQ)

A Klipper extras module that lets a 3D printer switch things in a **Loxone** home automation system straight from G-code. Define one `[loxone ...]` block per thing you want to switch, then put `LOXONE NAME=<target>` in a macro or in your slicer's start/end G-code. The module assembles the Miniserver URL, sends the request with HTTP Basic authentication, reports the reply in the console and optionally waits a configured number of seconds before the next G-code line runs.

Nothing about it is tied to the printer: it is a plain HTTP call to the Miniserver's web service, so anything you can wire behind a Virtual Text Input in Loxone Config is fair game — lights, a TV, an extractor fan, a notification. The request runs in a worker thread so it never stalls Klipper's event loop, and a Miniserver that is switched off or unreachable does not abort your print unless you ask it to.

## Features

- **Several targets** — one `[loxone <name>]` section per thing to switch, all reachable through a single `LOXONE NAME=<name>` command
- **Assembles the URL for you** — IP, Virtual Text Input name and text go in as separate options; no hand-built URLs to get wrong
- **Basic authentication** — user and password are base64-encoded into the `Authorization` header, done once at startup
- **Non-blocking** — the HTTP call runs in a worker thread while Klipper's reactor keeps servicing the MCU
- **Optional wait** — `wait_time` holds the G-code stream for N seconds after the call, overridable per call with `WAIT=`
- **Override per call** — `VALUE=` sends a different text through the same target, so on and off need one section, not two
- **Chooses how to fail** — `on_error: log` (default), `pause` the print, or `abort` it
- **Retries** — `retries: N` re-sends after a network hiccup
- **Test without the printer** — `tools/loxone_send.py` fires the same config section from a shell
- **Auto-updates** — ships a Moonraker `[update_manager]` block; the installer adds it for you
- **No dependencies** — Klipper's Python environment has no `requests`, so this uses only the standard library

## Requirements

- Klipper on Python 3.7 or newer (any current installation)
- A Loxone Miniserver reachable from the printer host
- A Loxone user allowed to use the web service
- Moonraker, if you want auto-updates — optional, the module works without it

## Setting up the Loxone side

The endpoint this module uses pushes a **text** into the Miniserver, so it needs a **Virtueller Texteingang** (Virtual Text Input). A plain virtual input will not do — that one takes a numeric value and the URL below would not reach it.

![Loxone Config: virtual text input, Impuls bei, Schalter, relay](docs/loxone-config-example.jpg)

A ready-made project with exactly this wiring is in [`docs/example.Loxone`](docs/example.Loxone) — open it in Loxone Config to look around.

1. In **Loxone Config**, go to **Peripherie → Virtuelle Eingänge** and create a **Virtueller Texteingang**. In the example it is called **`api`**.
2. That name is what goes into **`vi_name`**, character for character — it becomes part of the URL. Keep it short and free of spaces and umlauts and you never have to think about percent-encoding.
3. Drag in an **Impuls bei** block and connect the text input's **VI** output to its **T** input. Set the block's text to the value you intend to send — **`sw1`** in the example. That value is what goes into **`text_send`**. The block emits a pulse on **P** whenever the arriving text matches it.
4. Wire **P** into whatever you want to trigger. The example feeds the **Tg** (toggle) input of a **Schalter**, whose output drives relay **Q1**.
5. Save the program to the Miniserver.

The resulting request is

```
POST http://<loxone_ip>/dev/sps/io/<vi_name>/<text_send>
Authorization: Basic <base64 of user:password>
```

so the example project answers to `http://192.168.1.10/dev/sps/io/api/sw1`, which in `printer.cfg` is `vi_name: api` and `text_send: sw1`.

Two things about this wiring are worth knowing before you copy it:

- **`Tg` is a toggle, so sending `sw1` twice switches on and then off again.** Convenient for a test, awkward for a print — if a print aborts between the two calls, the relay stays on. For a deterministic on and off, use two texts (`sw1_on`, `sw1_off`), give each its own **Impuls bei** block, and wire them into the Schalter's **On** and **Off** inputs instead of **Tg**. One `[loxone ...]` section still covers both, because `VALUE=` overrides the text per call.
- **One text input can drive any number of these.** Hang several **Impuls bei** blocks off the same **VI** output, each matching a different text, and one `[loxone ...]` section plus `VALUE=` reaches all of them.

## Installation

```bash
cd ~
git clone https://github.com/sorglos-it/loxklipper.git
cd loxklipper
bash install.sh
```

The installer links `extras/loxone.py` into `~/klipper/klippy/extras/`, appends the Moonraker update block to `moonraker.conf` (keeping a `.loxklipper.bak` beside it), and restarts Klipper if it can do so without asking for a password. It is safe to run again and never prompts, because Moonraker re-runs it unattended after every update.

If Klipper does not live in `~/klipper`, point the installer at it:

```bash
KLIPPER_PATH=/opt/klipper bash install.sh
```

Clone rather than copying the folder from a Windows machine — a copy carries CRLF line endings along and `install.sh` then dies with `/bin/bash^M: bad interpreter`. Already hit it? `sed -i 's/\r$//' install.sh`.

Removing it again:

```bash
bash ~/loxklipper/uninstall.sh
```

## Configuration

One section per thing you want to switch. The section name is what `LOXONE NAME=` refers to.

```ini
[loxone printer_light]
loxone_ip: 192.168.1.10
user: klipper
password: changeme
vi_name: api            # name of the Virtual Text Input
text_send: sw1          # text the "Impuls bei" block matches
wait_time: 0
```

| Option | Default | Meaning |
|---|---|---|
| `loxone_ip` | *required* | Miniserver host or IP. A non-standard port goes here as `192.168.1.10:8080`. Aliases: `loxoneIP`, `host` |
| `user` | *required* | Loxone user allowed to use the web service. Alias: `username` |
| `password` | *required* | That user's password, in plain text — read the caveats. Alias: `passwort` |
| `vi_name` | *required* | Name of the Virtual Text Input in Loxone Config. Alias: `viName` |
| `text_send` | *required* | Text pushed into that input; must match what the **Impuls bei** block behind it expects. Alias: `textSend` |
| `wait_time` | `0` | Seconds to hold the G-code stream **after** the call. Alias: `waittime` |
| `protocol` | `http` | `http` or `https` |
| `verify_certificate` | `True` | HTTPS only. `False` skips certificate checking for a self-signed Miniserver certificate |
| `method` | `POST` | HTTP method |
| `timeout` | `5` | Seconds allowed per attempt |
| `retries` | `0` | Extra attempts after a failure, one second apart |
| `on_error` | `log` | `log` reports and carries on, `pause` runs `PAUSE`, `abort` raises a G-code error |

Option names are case-insensitive and the camelCase spellings above are accepted, because Klipper lower-cases every option name before a module sees it. `loxoneIP` and `loxone_ip` are the same option; pick one style and stay with it.

[`docs/example-printer.cfg`](docs/example-printer.cfg) has a fuller set including macro examples.

## Commands

| Command | Effect |
|---|---|
| `LOXONE NAME=<target>` | Send that target's configured text, then wait its `wait_time` |
| `LOXONE NAME=<target> VALUE=<text>` | Send a different text through the same target |
| `LOXONE NAME=<target> WAIT=<seconds>` | Override `wait_time` for this call; `WAIT=0` skips it |
| `LOXONE_LIST` | List the configured targets with the URL each one would call |

In a macro:

```ini
[gcode_macro PRINT_START]
gcode:
    LOXONE NAME=printer_light
    LOXONE NAME=workshop_fan

[gcode_macro PRINT_END]
gcode:
    LOXONE NAME=workshop_fan VALUE=WS-FAN-OFF
    LOXONE NAME=printer_light VALUE=light_off
```

## Testing without the printer

`tools/loxone_send.py` reads the same `[loxone ...]` sections and fires one from a shell, so you can check credentials and the Virtual Text Input name before restarting Klipper:

```bash
python3 ~/loxklipper/tools/loxone_send.py ~/printer_data/config/printer.cfg
python3 ~/loxklipper/tools/loxone_send.py ~/printer_data/config/printer.cfg printer_light --dry-run
python3 ~/loxklipper/tools/loxone_send.py ~/printer_data/config/printer.cfg printer_light
```

It needs nothing but Python 3 — no Klipper, no Moonraker — and it explains the common HTTP failures (401 credentials, 404 unknown text input, 405 wrong method) instead of just printing the code.

## Auto-updates

The installer adds this to `moonraker.conf`; see [`docs/example-moonraker.conf`](docs/example-moonraker.conf).

```ini
[update_manager loxklipper]
type: git_repo
path: ~/loxklipper
origin: https://github.com/sorglos-it/loxklipper.git
primary_branch: main
managed_services: klipper
install_script: install.sh
```

Restart Moonraker once after it appears, then loxklipper shows up alongside Klipper and Moonraker in Mainsail's or Fluidd's update panel.

## How it works

`[loxone <name>]` is a Klipper config prefix, so each section builds one `LoxoneTarget`. The first one created also registers the shared `LOXONE` and `LOXONE_LIST` commands and puts a dispatcher on the printer object; every later section registers itself with that dispatcher. This is why ten targets still give you one command rather than ten.

When `LOXONE` runs, the target percent-encodes `vi_name` and the text into the path, hands the request to a worker thread, and then polls that thread through `reactor.pause()` in 50 ms slices. That detail is the whole point of the design: a blocking socket call in Klipper's main thread stops MCU communication for the length of the timeout and can end in a `Timer too close` shutdown mid-print. The same `reactor.pause()` loop implements `wait_time`, in one-second slices, and breaks out if the printer shuts down underneath it.

Failures land in `_handle_failure`, which logs to `klippy.log`, echoes to the console with a `!!` prefix, and then does whatever `on_error` says.

## Notes & caveats

- **Your Loxone password sits in `printer.cfg` in plain text.** Mainsail and Fluidd show that file in their config editor, Moonraker serves it over the network, and it ends up in every config backup and in `git` if you version your config. Base64 is encoding, not encryption — anyone who reads the file has the password. Create a **dedicated Loxone user** with only the rights this needs, and do not reuse an admin password.
- **`wait_time` stops the toolhead with a hot nozzle.** The wait blocks the G-code stream, so the move queue drains and the printer sits still for the duration. A `wait_time: 120` in the middle of a print will leave a blob or a burnt patch. Use it in start/end G-code, keep it short mid-print, or park and retract first — `docs/example-printer.cfg` has a macro that does. `WAIT=0` skips the wait for a single call.
- **The wait happens when the line is parsed, not when the toolhead gets there.** Klipper reads ahead of the motion queue, so a `LOXONE` call in the middle of a print fires slightly before the nozzle reaches the matching point in the model. For "switch the light on at layer 30" that is irrelevant; for anything that must be frame-accurate it is not.
- **`M112` still works during a wait, `CANCEL_PRINT` queues behind it.** Emergency stop is handled out of band and breaks the wait loop through the shutdown check. A normal cancel is an ordinary G-code command and waits its turn, so it takes effect after the wait expires.
- **POST is the default because it was asked for; Loxone documents these endpoints as GET.** POST works against current firmware. If your Miniserver answers `405 Method Not Allowed`, set `method: GET` — nothing else changes.
- **`on_error: log` means a dead Miniserver does not stop your print.** That is the default on purpose. If the switching matters more than the print, use `abort`; `pause` needs `[pause_resume]` configured, and says so in the console instead of failing silently if it is missing.
- **`verify_certificate: False` turns off TLS verification completely** for that target — not just the hostname check. It is the right setting for a Miniserver with a self-signed certificate on your own LAN and the wrong one over the open internet.
- **A wrong text is not an error.** The Miniserver accepts any text into the input and answers `200`; only the **Impuls bei** block decides whether anything happens. So a typo in `text_send` looks like a success in the Klipper console and switches nothing. `LOXONE_LIST` prints the exact URL each target calls — compare it against the block in Loxone Config.
- **Spaces and umlauts in `vi_name` or `text_send` are percent-encoded, `/` is not.** A slash is left alone so you can address deeper paths on purpose. If a name genuinely contains a slash, this module will not encode it for you.
- **Klipper's Python environment has no `requests`.** Only what ships with Python is available, so this uses `urllib.request`: no connection pooling, no HTTP/2, one fresh connection per call. At the rate G-code fires these, that costs nothing.
- **The symlink shows up as an untracked file in Klipper's git repository.** Every Klipper plugin installed this way does this. It does not block Klipper updates, but `git status` in `~/klipper` will mention `klippy/extras/loxone.py`.
- **Target names are compared case-insensitively** and a duplicate is refused at startup, so `[loxone tv]` and `[loxone TV]` cannot both exist. A name containing spaces works but then needs quoting at the command line, which is a good reason not to use one.
- **The installer will not run as root.** Installing as root leaves a symlink and a `moonraker.conf` that the Klipper user cannot manage.

## Development

The module is a single file, `extras/loxone.py`, symlinked into `klippy/extras/`. Editing it in the clone and restarting Klipper is the whole edit-test loop:

```bash
sudo systemctl restart klipper
tail -f ~/printer_data/logs/klippy.log
```

Every request is logged there as `loxklipper: POST <url> (target '<name>')`; the `Authorization` header is never logged. For faster iteration, `tools/loxone_send.py` exercises the same URL assembly and authentication without restarting anything.

## Support this project ❤️

If this saved you time, you can support further development:

[![Donate with PayPal](https://www.paypalobjects.com/en_US/i/btn/btn_donate_LG.gif)](https://www.paypal.com/donate/?hosted_button_id=6CDEVZGJWTNQQ)

**[➡️ Donate via PayPal](https://www.paypal.com/donate/?hosted_button_id=6CDEVZGJWTNQQ)**

## License

This project is licensed under the [MIT License](LICENSE) — © 2026 Thomas Weirich.
