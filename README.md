# Kodi TCP Bridge

A Kodi service add-on that listens on a TCP socket and speaks plain
CR-delimited ASCII, so any control system can drive Kodi without parsing JSON. A ready-to-use Crestron SIMPL+ module is included in the [`Crestron/`](Crestron/) folder.

Default port: **9091** (Kodi's own JSON-RPC socket stays on 9090, untouched).

Every message in both directions is uppercase ASCII terminated by a single
carriage return (`\r`). Inbound also accepts LF or CRLF.

## Install

1. Copy the zip to the Kodi box.
2. Kodi → Add-ons → Install from zip file → select the zip.
3. Kodi → Add-ons → My add-ons → Services → Kodi TCP Bridge →
   Configure to change the port, bind address, or feedback rate.

The service starts automatically at Kodi startup. Nothing needs to be enabled
under Settings → Services → Control; this add-on does not use Kodi's own
remote-control interfaces.

## Settings

Kodi → Add-ons → My add-ons → Services → Kodi TCP Bridge → Configure.

| Setting | Default | Range | Effect |
|---|---|---|---|
| Progress update interval while playing | `1` s | 1–10 s | How often `TIME` and `PROGRESS` are recomputed and sent while something is playing |
| Heartbeat interval | `0` (off) | 0–120 s | `HEARTBEAT` every N seconds |
| Notify on screen when a client connects | on | | Toast naming the client IP |
| Bind address | `0.0.0.0` | | Interface to listen on |
| Listen port | `9091` | 1024–65535 | |

Raise the update interval to keep a busy processor quiet: at `10` you get one
`TIME`/`PROGRESS` pair every ten seconds instead of one a second. It only paces
those two. Play, pause, stop, seek, track change, volume and mute are still
sent the instant Kodi reports them, whatever the interval is set to, so
transport feedback never feels sluggish.

All settings apply immediately — no Kodi restart, and existing connections stay
up unless the address or port changed.

## Commands (Crestron → Kodi)

### Navigation
| Command | Action |
|---|---|
| `UP` `DOWN` `LEFT` `RIGHT` | D-pad |
| `PAGEUP` / `PAGEDOWN` | Page up / page down (aliases `PGUP` / `PGDN`) |
| `SELECT` | OK / Enter — opens the player OSD during fullscreen video |
| `BACK` | Back |
| `MENU` | Context menu |
| `INFO` | Info |
| `OSD` | Player OSD |
| `HOME` | Jump to Home |
| `MOVIES` `TVSHOWS` `MUSIC` | Jump to library section |
| `FULLSCREEN` | Return to fullscreen video |
| `0`-`9` | Numeric keypad, one digit per press |
| `NUM=7` / `NUM=1234` | Same, addressed by name; multi-digit sends each key in turn |

### Transport
| Command | Action |
|---|---|
| `PLAY` / `PAUSE` | **Discrete** play and pause |
| `PLAYPAUSE` | Toggle |
| `STOP` | Stop |
| `FF` / `RW` | Step scan speed up/down |
| `NEXT` / `PREV` | Next / previous item |
| `SKIPFWD` / `SKIPBACK` | Small seek |
| `BIGFWD` / `BIGBACK` | Large seek |
| `SEEK=90` | Seek to 90 seconds |
| `SEEK=01:23:45` | Seek to absolute time |
| `SEEK=50%` | Seek to percentage |
| `REPEAT=OFF\|ONE\|ALL` | Set repeat (no argument = cycle) |
| `SHUFFLE=0\|1` | Set shuffle (no argument = toggle) |

### Audio and misc
| Command | Action |
|---|---|
| `VOLUP` / `VOLDOWN` | Volume step |
| `VOL=45` | Absolute volume, 0–100 |
| `MUTE` / `MUTE=1` / `MUTE=0` | Toggle or discrete mute |
| `SUBTITLES` / `AUDIOTRACK` | Cycle subtitle / audio track |
| `QUERY` (or `STATUS`) | Resend the full state |
| `PING` | Replies `PONG` |
| `EXEC=<builtin>` | Run any Kodi builtin, e.g. `EXEC=ActivateWindow(Weather)` |

## Feedback (Kodi → Crestron)

Sent unsolicited whenever a value changes — no polling required. The full set
is also sent on connect (followed by `READY`) and on `QUERY`.

| String | Notes |
|---|---|
| `STATE=PLAYING\|PAUSED\|STOPPED` | Drive your transport feedback from this |
| `SPEED=1` | Negative while rewinding, >1 while scanning |
| `MEDIATYPE=video\|audio\|picture` | Empty when stopped |
| `TITLE=...` | Episode or track title |
| `SUBTITLE=...` | Show + S01E02, movie year, or artist - album |
| `THUMBNAIL=...` | HTTP URL to cover art or video thumbnail |
| `TIME=00:12:34` | Elapsed |
| `DURATION=01:47:00` | Total |
| `PROGRESS=0..65535` | Ready to drive an analog gauge directly |
| `VOLUME=0..100` | |
| `MUTE=0\|1` | |
| `HEARTBEAT` | Only if the heartbeat interval is set |
| `ERR=NOPLAYER` | Transport command arrived with nothing playing |
| `ERR=<COMMAND>` | Unrecognised or malformed command |

`TITLE`, `SUBTITLE`, and `THUMBNAIL` have any CR/LF stripped, so a Serial Gather on `\r`
can never be desynchronised by metadata.

## Crestron Module

A pre-built SIMPL+ module (`KodiTCPBridgeFeedback.usp` / `KodiTCPBridgeFeedback.ush`) is provided in the [`Crestron/`](Crestron/) folder to parse feedback automatically without needing complex SIMPL logic:

- **Socket Wiring**: Connect the Crestron **TCP/IP Client** `rx$` directly to the module's `From_Device$` buffer input (internal buffering reassembles split TCP packets automatically, so no Serial Gather is required).
- **Digitals**: `Is_Online`, `Is_Playing`, `Is_Paused`, `Is_Stopped`, `Is_Scanning`, `Is_Muted`, `No_Player_Error`.
- **Analogs**: `Progress` (0–65535, ready for gauge joins), `Volume` (0–100).
- **Serials**: `Title$`, `Subtitle$`, `Thumbnail$`, `Elapsed$`, `Duration$`, `Media_Type$`, `Last_Error$`.
- **Link Watchdog**: `Link_Timeout_Seconds` parameter tracks heartbeat / traffic to drive `Is_Online` automatically.

## Notes

- **There is no authentication.** Anything that can reach the port can control
  Kodi. Keep it on a trusted VLAN, or set the bind address to the specific
  interface facing your control subnet.
- If the socket cannot be opened at startup (Kodi booting before the network
  is up, or the port already in use), the service retries every 5 seconds
  until it succeeds. The failure is logged once at error level; subsequent
  attempts are debug-level, so enable Kodi debug logging to watch them.
- Up to 8 simultaneous clients; every one of them receives all feedback.
- Command names are case-insensitive on the wire. Arguments keep the case
  you send, since `EXEC=` carries paths and plugin ids that Kodi matches
  case-sensitively.
- Kodi 19 (Matrix) or newer, since it targets Python 3.

## Changelog

### v1.0.0 (2026-08-23)
- Initial release.

