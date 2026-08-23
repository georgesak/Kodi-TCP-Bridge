# -*- coding: utf-8 -*-
"""Kodi TCP Bridge - a CR-delimited ASCII control socket for Kodi."""

import json
import select
import socket
import time
from urllib.parse import quote, unquote

import xbmc
import xbmcaddon
import xbmcgui

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')
ADDON_NAME = ADDON.getAddonInfo('name') or 'Kodi TCP Bridge'

EOL = b'\r'
MAX_LINE = 512          # drop a client that floods us without a terminator
MAX_CLIENTS = 8
MIN_INTERVAL = 1        # seconds between TIME/PROGRESS refreshes while playing
MAX_INTERVAL = 10
SELECT_TIMEOUT = 0.2
RETRY_INTERVAL = 5      # seconds between bind attempts if the socket won't open


def log(msg, level=xbmc.LOGINFO):
    xbmc.log('[%s] %s' % (ADDON_ID, msg), level)


# ---------------------------------------------------------------- JSON-RPC --

def jsonrpc(method, **params):
    """Call Kodi's in-process JSON-RPC. Returns the result dict, or None."""
    request = {'jsonrpc': '2.0', 'id': 1, 'method': method}
    if params:
        request['params'] = params
    try:
        raw = xbmc.executeJSONRPC(json.dumps(request))
        reply = json.loads(raw)
    except Exception as exc:                                # noqa: BLE001
        log('JSON-RPC %s failed: %s' % (method, exc), xbmc.LOGWARNING)
        return None
    if 'error' in reply:
        log('JSON-RPC %s error: %s' % (method, reply['error']), xbmc.LOGWARNING)
        return None
    return reply.get('result')


def active_player():
    """Return (playerid, type) of the first active player, or (None, None)."""
    players = jsonrpc('Player.GetActivePlayers') or []
    if players:
        return players[0].get('playerid'), players[0].get('type')
    return None, None


# ----------------------------------------------------------------- helpers --

def clean(value):
    """Make a value safe to put in a CR-delimited serial string."""
    if value is None:
        return ''
    text = value if isinstance(value, str) else str(value)
    return text.replace('\r', ' ').replace('\n', ' ').strip()


def hms(seconds):
    seconds = int(max(0, seconds))
    return '%02d:%02d:%02d' % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def secs(time_dict):
    if not isinstance(time_dict, dict):
        return 0
    return (time_dict.get('hours', 0) * 3600
            + time_dict.get('minutes', 0) * 60
            + time_dict.get('seconds', 0))


def parse_seek(arg):
    """Accept SEEK=90 (seconds), SEEK=01:23:45, or SEEK=50% (percentage)."""
    if arg.endswith('%'):
        try:
            return {'percentage': max(0.0, min(100.0, float(arg[:-1])))}
        except ValueError:
            return None
    if ':' in arg:
        parts = arg.split(':')
        try:
            parts = [int(p) for p in parts]
        except ValueError:
            return None
        while len(parts) < 3:
            parts.insert(0, 0)
        return {'time': {'hours': parts[0], 'minutes': parts[1], 'seconds': parts[2]}}
    try:
        total = int(arg)
    except ValueError:
        return None
    return {'time': {'hours': total // 3600,
                     'minutes': (total % 3600) // 60,
                     'seconds': total % 60}}


def get_kodi_base_url():
    """Return http://host:port for Kodi's built-in webserver."""
    ip = xbmc.getIPAddress()
    if not ip or ip in ('127.0.0.1', '0.0.0.0'):
        try:
            host_setting = xbmcaddon.Addon().getSetting('host')
            if host_setting and host_setting not in ('0.0.0.0', '127.0.0.1', 'localhost'):
                ip = host_setting
        except Exception:
            pass
    if not ip or ip == '0.0.0.0':
        ip = '127.0.0.1'

    port = 8080
    try:
        port_setting = jsonrpc('Settings.GetSettingValue', setting='services.webserverport')
        if port_setting and isinstance(port_setting, dict) and 'value' in port_setting:
            port = int(port_setting['value'])
    except Exception:
        pass

    return 'http://%s:%d' % (ip, port)


def get_thumbnail_url(art_path):
    """Convert an internal Kodi art path or external URL into an HTTP URL."""
    if not art_path:
        return ''

    # Direct HTTP/HTTPS URLs
    if art_path.startswith(('http://', 'https://')):
        return clean(art_path)

    # Kodi image:// wrapper
    if art_path.startswith('image://'):
        inner = unquote(art_path[8:].rstrip('/'))
        if inner.startswith(('http://', 'https://')):
            return clean(inner)
        base_url = get_kodi_base_url()
        return clean('%s/image/%s' % (base_url.rstrip('/'), quote(art_path, safe='')))

    # Any other local/VFS path
    base_url = get_kodi_base_url()
    return clean('%s/image/%s' % (base_url.rstrip('/'), quote(art_path, safe='')))


def get_item_artwork(item):
    """Pick the most appropriate artwork path from an item dictionary."""
    if not isinstance(item, dict):
        return ''

    art = item.get('art') or {}
    itype = item.get('type', '')

    raw_thumb = ''
    if isinstance(art, dict):
        if itype == 'episode':
            raw_thumb = (art.get('thumb') or art.get('season.poster') or
                         art.get('tvshow.poster') or art.get('poster') or '')
        elif itype == 'movie':
            raw_thumb = art.get('poster') or art.get('thumb') or ''
        elif itype in ('song', 'musicvideo'):
            raw_thumb = (art.get('album.thumb') or art.get('thumb') or
                         art.get('poster') or '')
        else:
            raw_thumb = art.get('thumb') or art.get('poster') or ''

        if not raw_thumb:
            for k in ('thumb', 'poster', 'icon', 'fanart', 'banner'):
                if art.get(k):
                    raw_thumb = art[k]
                    break

    if not raw_thumb:
        raw_thumb = item.get('thumbnail') or ''

    return raw_thumb


# ---------------------------------------------------------------- commands --

# Remote buttons that pass through Kodi's keymap engine (remote.xml).
REMOTE_BUTTONS = {
    'UP': 'up',
    'DOWN': 'down',
    'LEFT': 'left',
    'RIGHT': 'right',
    'SELECT': 'select',
    'BACK': 'back',
    'MENU': 'menu',
    'INFO': 'info',
    'OSD': 'osd',
    'PAGEUP': 'pageplus',
    'PAGEDOWN': 'pageminus',
    'PGUP': 'pageplus',
    'PGDN': 'pageminus',
    'SUBTITLES': 'subtitle',
    'AUDIOTRACK': 'language',
}

# Commands that map straight onto a Kodi builtin window activation.
BUILTINS = {
    'HOME': 'ActivateWindow(Home)',
    'FULLSCREEN': 'ActivateWindow(FullscreenVideo)',
}

# Library jumps, as (window, path). Movies and TV shows are the same Kodi
# window at different paths, and ActivateWindow refuses to navigate a window
# that is already active - it just reloads it where it stands. See jump().
LIBRARY = {
    'MOVIES': ('Videos', 'videodb://movies/titles/'),
    'TVSHOWS': ('Videos', 'videodb://tvshows/titles/'),
    'MUSIC': ('Music', 'musicdb://'),
}


def jump(window, path):
    """Open a library location, from inside that window or anywhere else."""
    if xbmc.getCondVisibility('Window.IsActive(%s)' % window):
        # Already there: ReplaceWindow is the only one that will move.
        builtin = 'ReplaceWindow'
    else:
        # Arriving from elsewhere: ActivateWindow keeps BACK working normally,
        # since it stacks rather than dropping the window we came from.
        builtin = 'ActivateWindow'
    xbmc.executebuiltin('%s(%s,%s,return)' % (builtin, window, path))


DIGITS = '0123456789'


def do_command(name, arg):
    """Run one command. Returns an optional immediate reply string."""
    if name in REMOTE_BUTTONS:
        jsonrpc('Input.ButtonEvent', button=REMOTE_BUTTONS[name], keymap='R1')
        return None

    if name in BUILTINS:
        xbmc.executebuiltin(BUILTINS[name])
        return None

    if name in LIBRARY:
        jump(*LIBRARY[name])
        return None

    if name == 'PING':
        return 'PONG'

    # NUM=7, NUM=1234, or a bare digit string straight off a keypad. Every
    # digit is sent through the keymap engine as its own key press.
    if name == 'NUM' or (name and all(char in DIGITS for char in name)):
        digits = arg if name == 'NUM' else name
        if not digits or not all(char in DIGITS for char in digits):
            return 'ERR=NUM'
        for digit in digits:
            jsonrpc('Input.ButtonEvent', button=digit, keymap='R1')
        return None

    if name == 'EXEC' and arg:
        xbmc.executebuiltin(arg)
        return None

    # --- volume: valid with or without an active player -------------------
    if name == 'VOLUP':
        jsonrpc('Application.SetVolume', volume='increment')
        return None
    if name == 'VOLDOWN':
        jsonrpc('Application.SetVolume', volume='decrement')
        return None
    if name == 'VOL' and arg:
        try:
            jsonrpc('Application.SetVolume', volume=max(0, min(100, int(arg))))
        except ValueError:
            return 'ERR=VOL'
        return None
    if name == 'MUTE':
        if arg.upper() in ('1', 'ON', 'TRUE'):
            jsonrpc('Application.SetMute', mute=True)
        elif arg.upper() in ('0', 'OFF', 'FALSE'):
            jsonrpc('Application.SetMute', mute=False)
        else:
            jsonrpc('Application.SetMute', mute='toggle')
        return None

    # --- transport: everything below needs a player -----------------------
    pid, _ptype = active_player()
    if pid is None:
        if name in ('PLAY', 'PAUSE', 'PLAYPAUSE', 'STOP', 'FF', 'RW',
                    'NEXT', 'PREV', 'SKIPFWD', 'SKIPBACK', 'BIGFWD',
                    'BIGBACK', 'SEEK', 'REPEAT', 'SHUFFLE'):
            return 'ERR=NOPLAYER'
        return 'ERR=%s' % name

    if name == 'PLAY':
        jsonrpc('Player.PlayPause', playerid=pid, play=True)
    elif name == 'PAUSE':
        jsonrpc('Player.PlayPause', playerid=pid, play=False)
    elif name == 'PLAYPAUSE':
        jsonrpc('Player.PlayPause', playerid=pid, play='toggle')
    elif name == 'STOP':
        jsonrpc('Player.Stop', playerid=pid)
    elif name == 'FF':
        jsonrpc('Player.SetSpeed', playerid=pid, speed='increment')
    elif name == 'RW':
        jsonrpc('Player.SetSpeed', playerid=pid, speed='decrement')
    elif name == 'NEXT':
        jsonrpc('Player.GoTo', playerid=pid, to='next')
    elif name == 'PREV':
        jsonrpc('Player.GoTo', playerid=pid, to='previous')
    elif name == 'SKIPFWD':
        jsonrpc('Player.Seek', playerid=pid, value={'step': 'smallforward'})
    elif name == 'SKIPBACK':
        jsonrpc('Player.Seek', playerid=pid, value={'step': 'smallbackward'})
    elif name == 'BIGFWD':
        jsonrpc('Player.Seek', playerid=pid, value={'step': 'bigforward'})
    elif name == 'BIGBACK':
        jsonrpc('Player.Seek', playerid=pid, value={'step': 'bigbackward'})
    elif name == 'SEEK':
        value = parse_seek(arg)
        if value is None:
            return 'ERR=SEEK'
        jsonrpc('Player.Seek', playerid=pid, value=value)
    elif name == 'REPEAT':
        jsonrpc('Player.SetRepeat', playerid=pid,
                repeat=(arg.lower() if arg.upper() in ('OFF', 'ONE', 'ALL')
                        else 'cycle'))
    elif name == 'SHUFFLE':
        jsonrpc('Player.SetShuffle', playerid=pid,
                shuffle=(arg != '0') if arg in ('0', '1') else 'toggle')
    else:
        return 'ERR=%s' % name
    return None


# ------------------------------------------------------------------- state --

def read_state():
    """Snapshot everything we report, as a flat dict of key -> string."""
    state = {}

    app = jsonrpc('Application.GetProperties', properties=['volume', 'muted']) or {}
    state['VOLUME'] = str(app.get('volume', 0))
    state['MUTE'] = '1' if app.get('muted') else '0'

    pid, ptype = active_player()
    if pid is None:
        state.update({'STATE': 'STOPPED', 'MEDIATYPE': '', 'TITLE': '',
                      'SUBTITLE': '', 'THUMBNAIL': '', 'TIME': '00:00:00',
                      'DURATION': '00:00:00', 'PROGRESS': '0', 'SPEED': '0'})
        return state

    props = jsonrpc('Player.GetProperties', playerid=pid,
                    properties=['speed', 'time', 'totaltime', 'percentage']) or {}
    speed = props.get('speed', 0)
    state['SPEED'] = str(speed)
    state['STATE'] = 'PLAYING' if speed else 'PAUSED'
    state['MEDIATYPE'] = clean(ptype)
    state['TIME'] = hms(secs(props.get('time')))
    state['DURATION'] = hms(secs(props.get('totaltime')))
    state['PROGRESS'] = str(int(props.get('percentage', 0) / 100.0 * 65535))

    item = (jsonrpc('Player.GetItem', playerid=pid,
                    properties=['title', 'showtitle', 'season', 'episode',
                                'artist', 'album', 'year', 'art', 'thumbnail']) or {}).get('item', {})
    title = clean(item.get('title')) or clean(item.get('label'))
    subtitle = ''
    itype = item.get('type', '')

    if itype == 'episode':
        show = clean(item.get('showtitle'))
        season, episode = item.get('season', -1), item.get('episode', -1)
        if season >= 0 and episode >= 0:
            subtitle = ('%s  S%02dE%02d' % (show, season, episode)).strip()
        else:
            subtitle = show
    elif itype == 'movie':
        year = item.get('year')
        subtitle = str(year) if year else ''
    elif itype in ('song', 'musicvideo'):
        artist = item.get('artist') or []
        if isinstance(artist, list):
            artist = ', '.join(artist)
        album = clean(item.get('album'))
        subtitle = ' - '.join([p for p in (clean(artist), album) if p])

    raw_thumb = get_item_artwork(item)
    thumb_url = get_thumbnail_url(raw_thumb)

    state['TITLE'] = title
    state['SUBTITLE'] = clean(subtitle)
    state['THUMBNAIL'] = clean(thumb_url)
    return state


# ------------------------------------------------------------------ bridge --

class Bridge(object):
    """Owns the listening socket and every connected client."""

    def __init__(self):
        self.listener = None
        self.clients = {}          # socket -> bytearray receive buffer
        self.last = {}             # last values broadcast, for delta sending
        self.failures = 0          # consecutive failed binds, for log throttling
        self.notify = True         # show a toast when a client connects

    def toast(self, message, icon=None):
        if not self.notify:
            return
        try:
            xbmcgui.Dialog().notification(
                ADDON_NAME, message,
                icon or xbmcgui.NOTIFICATION_INFO, 4000, False)
        except Exception as exc:                            # noqa: BLE001
            # A toast is never worth taking the bridge down for.
            log('notification failed: %s' % exc, xbmc.LOGWARNING)

    # -- lifecycle --------------------------------------------------------
    def start(self, host, port):
        self.stop()
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen(MAX_CLIENTS)
            listener.setblocking(False)
        except Exception as exc:                            # noqa: BLE001
            # The usual cause is Kodi starting before the NIC has an address,
            # so complain once and then keep quiet while we retry.
            self.failures += 1
            level = xbmc.LOGERROR if self.failures == 1 else xbmc.LOGDEBUG
            log('cannot listen on %s:%s - %s (attempt %d, retrying every %ds)'
                % (host, port, exc, self.failures, RETRY_INTERVAL), level)
            return False
        self.listener = listener
        if self.failures:
            log('listening on %s:%s after %d failed attempt(s)'
                % (host, port, self.failures))
        else:
            log('listening on %s:%s' % (host, port))
        self.failures = 0
        return True

    def stop(self):
        for sock in list(self.clients):
            self.drop(sock)
        if self.listener:
            try:
                self.listener.close()
            except Exception:                               # noqa: BLE001
                pass
            self.listener = None
        self.last = {}

    def drop(self, sock):
        self.clients.pop(sock, None)
        try:
            sock.close()
        except Exception:                                   # noqa: BLE001
            pass

    # -- io ---------------------------------------------------------------
    def send(self, sock, line):
        try:
            sock.sendall(line.encode('utf-8', 'replace') + EOL)
        except Exception:                                   # noqa: BLE001
            self.drop(sock)

    def broadcast(self, line):
        for sock in list(self.clients):
            self.send(sock, line)

    def accept(self):
        try:
            sock, addr = self.listener.accept()
        except Exception:                                   # noqa: BLE001
            return
        if len(self.clients) >= MAX_CLIENTS:
            log('refusing %s, client limit reached' % (addr,), xbmc.LOGWARNING)
            self.toast('Refused %s - %d client limit reached'
                       % (addr[0], MAX_CLIENTS), xbmcgui.NOTIFICATION_WARNING)
            try:
                sock.close()
            except Exception:                               # noqa: BLE001
                pass
            return
        sock.setblocking(False)
        self.clients[sock] = bytearray()
        log('client connected from %s' % (addr,))
        self.toast('Client connected: %s' % addr[0])
        # A control system needs the whole picture the moment it links up.
        self.last = read_state()
        for key, value in self.last.items():
            self.send(sock, '%s=%s' % (key, value))
        self.send(sock, 'READY')

    def receive(self, sock):
        try:
            chunk = sock.recv(1024)
        except Exception:                                   # noqa: BLE001
            self.drop(sock)
            return
        if not chunk:
            log('client disconnected')
            self.drop(sock)
            return

        buf = self.clients.get(sock)
        if buf is None:
            return
        buf.extend(chunk)

        while True:
            index = -1
            for i, byte in enumerate(buf):
                if byte in (13, 10):            # CR or LF, either terminates
                    index = i
                    break
            if index < 0:
                if len(buf) > MAX_LINE:
                    log('line too long, dropping client', xbmc.LOGWARNING)
                    self.drop(sock)
                return
            line = bytes(buf[:index]).decode('utf-8', 'replace')
            del buf[:index + 1]
            self.dispatch(sock, line)
            if sock not in self.clients:        # dispatch may have dropped it
                return

    def dispatch(self, sock, line):
        command = line.strip()
        if not command:
            return
        name, _, arg = command.partition('=')
        # Command names are case-insensitive, arguments are not: EXEC carries
        # paths and plugin ids that Kodi matches case-sensitively.
        name, arg = name.strip().upper(), arg.strip()

        if name in ('QUERY', 'STATUS'):
            self.last = read_state()
            for key, value in self.last.items():
                self.send(sock, '%s=%s' % (key, value))
            return

        try:
            reply = do_command(name, arg)
        except Exception as exc:                            # noqa: BLE001
            log('command %r failed: %s' % (command, exc), xbmc.LOGERROR)
            reply = 'ERR=%s' % name
        if reply:
            self.send(sock, reply)

    # -- feedback ---------------------------------------------------------
    def publish(self, force=False):
        """Broadcast whatever changed since last time."""
        if not self.clients:
            return
        state = read_state()
        for key, value in state.items():
            if force or self.last.get(key) != value:
                self.broadcast('%s=%s' % (key, value))
        self.last = state

    def sockets(self):
        socks = list(self.clients)
        if self.listener:
            socks.append(self.listener)
        return socks


# ---------------------------------------------------------------- monitors --

class BridgeMonitor(xbmc.Monitor):
    def __init__(self):
        super(BridgeMonitor, self).__init__()
        self.dirty = True
        self.settings_changed = False

    def onNotification(self, sender, method, data):
        if method in ('Application.OnVolumeChanged', 'Player.OnSeek',
                      'Player.OnSpeedChanged', 'Playlist.OnClear'):
            self.dirty = True

    def onSettingsChanged(self):
        self.settings_changed = True


class BridgePlayer(xbmc.Player):
    """Kodi calls these from its own thread, so we only ever set a flag."""

    def __init__(self, monitor):
        super(BridgePlayer, self).__init__()
        self.monitor = monitor

    def onAVStarted(self):
        self.monitor.dirty = True

    def onPlayBackStarted(self):
        self.monitor.dirty = True

    def onAVChange(self):
        self.monitor.dirty = True

    def onPlayBackPaused(self):
        self.monitor.dirty = True

    def onPlayBackResumed(self):
        self.monitor.dirty = True

    def onPlayBackStopped(self):
        self.monitor.dirty = True

    def onPlayBackEnded(self):
        self.monitor.dirty = True

    def onPlayBackSeek(self, time_ms, seek_offset):
        self.monitor.dirty = True


# ------------------------------------------------------------------- main --

def read_settings():
    # A fresh Addon object every time. The one built at import can keep serving
    # the values it was constructed with, which would pin the update interval
    # to whatever it was when Kodi started.
    addon = xbmcaddon.Addon()

    def get_int(key, fallback):
        try:
            return int(addon.getSetting(key))
        except (ValueError, TypeError):
            return fallback

    def get_bool(key, fallback):
        raw = (addon.getSetting(key) or '').strip().lower()
        if raw in ('true', '1'):
            return True
        if raw in ('false', '0'):
            return False
        return fallback

    return {
        'host': addon.getSetting('host') or '0.0.0.0',
        'port': get_int('port', 9091),
        # Clamped to the slider's range in case settings.xml was hand-edited.
        'interval': min(MAX_INTERVAL,
                        max(MIN_INTERVAL, get_int('interval', MIN_INTERVAL))),
        'heartbeat': max(0, get_int('heartbeat', 0)),
        'notify': get_bool('notify', True),
    }


def main():
    monitor = BridgeMonitor()
    player = BridgePlayer(monitor)                          # noqa: F841
    bridge = Bridge()

    config = read_settings()
    bridge.notify = config['notify']
    log('progress updates every %ds while playing, heartbeat %s'
        % (config['interval'],
           ('%ds' % config['heartbeat']) if config['heartbeat'] else 'off'))
    bridge.start(config['host'], config['port'])

    next_tick = 0.0
    next_retry = time.time() + RETRY_INTERVAL
    next_beat = time.time() + config['heartbeat'] if config['heartbeat'] else 0.0

    while not monitor.abortRequested():
        if monitor.settings_changed:
            monitor.settings_changed = False
            new_config = read_settings()
            if (new_config['host'], new_config['port']) != (config['host'], config['port']):
                log('rebinding after settings change')
                bridge.failures = 0
                bridge.start(new_config['host'], new_config['port'])
                next_retry = time.time() + RETRY_INTERVAL
            if new_config['interval'] != config['interval']:
                log('progress update interval now %ds' % new_config['interval'])
            config = new_config
            bridge.notify = config['notify']
            # Re-arm from the moment of the change, so a new interval applies
            # now rather than after the tick that is already pending.
            next_tick = time.time() + config['interval']
            next_beat = time.time() + config['heartbeat'] if config['heartbeat'] else 0.0

        # Kodi can start before the network is up, so keep trying to bind.
        if bridge.listener is None and time.time() >= next_retry:
            bridge.start(config['host'], config['port'])
            next_retry = time.time() + RETRY_INTERVAL

        watched = bridge.sockets()
        if not watched:
            # Nothing to select on yet; idle without burning the CPU.
            if monitor.waitForAbort(SELECT_TIMEOUT):
                break
            readable = []
        else:
            try:
                readable, _, _ = select.select(watched, [], [], SELECT_TIMEOUT)
            except Exception:                               # noqa: BLE001
                # A socket died underneath us; prune and carry on.
                for sock in list(bridge.clients):
                    try:
                        sock.fileno()
                    except Exception:                       # noqa: BLE001
                        bridge.drop(sock)
                if bridge.listener is not None:
                    try:
                        bridge.listener.fileno()
                    except Exception:                       # noqa: BLE001
                        log('listening socket lost, will rebind',
                            xbmc.LOGWARNING)
                        bridge.stop()
                        next_retry = time.time() + RETRY_INTERVAL
                readable = []
                if monitor.waitForAbort(SELECT_TIMEOUT):
                    break

        for sock in readable:
            if sock is bridge.listener:
                bridge.accept()
            else:
                bridge.receive(sock)

        now = time.time()
        if monitor.dirty:
            monitor.dirty = False
            bridge.publish()
            next_tick = now + config['interval']
        elif now >= next_tick:
            # Safety net in case a Kodi callback is missed. The delta check in
            # publish() means nothing hits the wire unless it actually changed.
            if bridge.clients:
                bridge.publish()
            next_tick = now + config['interval']

        if config['heartbeat'] and now >= next_beat:
            bridge.broadcast('HEARTBEAT')
            next_beat = now + config['heartbeat']

    log('abort requested, shutting down')
    bridge.stop()


if __name__ == '__main__':
    main()
