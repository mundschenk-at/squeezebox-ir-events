#!/usr/bin/micropython
#
# Squeezebox IR Events daemon. Runs on the player, watches for status changes
# reported by the server, and sends LIRC commands for configured events.
#
# Copyright (C) 2019 Peter Putzer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

import uio
import ujson
import usocket
import uselect
import sys
import ure
import utime
import uos

# Some primitive configuration file parsing.
try:
    CONFIG_FILE = sys.argv[1]
    CONFIG = ujson.load(uio.open(CONFIG_FILE))
except:
    print('Error loading configuration file %s.' % CONFIG_FILE)
    raise


class urlencode:
    """
    Extracted from urllib.parse
    """
    _ALWAYS_SAFE = frozenset(b'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                             b'abcdefghijklmnopqrstuvwxyz'
                             b'0123456789'
                             b'_.-')
    _ALWAYS_SAFE_BYTES = bytes(_ALWAYS_SAFE)
    _safe_quoters = {}
    _unquote_cache = None

    @staticmethod
    def unquote(string):
        """unquote('abc%20def') -> 'abc def'."""
        urlencode._unquote_cache

        # Note: strings are encoded as UTF-8. This is only an issue if it contains
        # unescaped non-ASCII characters, which URIs should not.
        if not string:
            return ''

        if isinstance(string, str):
            string = string.encode('utf-8')

        bits = string.split(b'%')
        if len(bits) == 1:
            return string.decode('utf-8')

        res = [bits[0]]
        append = res.append

        # Build cache for hex to char mapping on-the-fly only for codes
        # that are actually used
        if urlencode._unquote_cache is None:
           urlencode._unquote_cache = {}

        for item in bits[1:]:
            try:
                code = item[:2]
                char = urlencode._unquote_cache.get(code)
                if char is None:
                    char = urlencode._unquote_cache[code] = bytes([int(code, 16)])
                append(char)
                append(item[2:])
            except KeyError:
                append(b'%')
                append(item)

        return b''.join(res).decode('utf-8')

    class Quoter:
        """
        A mapping from bytes (in range(0,256)) to strings.
        String values are percent-encoded byte values, unless the key < 128, and
        in the "safe" set (either the specified safe set, or default set).
        """

        # Keeps a cache internally for efficiency.
        def __init__(self, safe):
            """safe: bytes object."""
            self.safe = urlencode._ALWAYS_SAFE.union(safe)
            self.d = {}

        def __getitem__(self, key):
            try:
                return self.d[key]
            except KeyError:
                v = self.__missing__(key)
                self.d[key] = v
                return v

        def __setitem__(self, key, v):
            self.d[key] = v

        def __delitem__(self, key):
            del self.d[key]

        def __contains__(self, key):
            return key in self.d

        def __missing__(self, b):
            # Handle a cache miss. Store quoted string in cache and return.
            res = chr(b) if b in self.safe else '%{:02X}'.format(b)
            self[b] = res
            return res

        def __repr__(self):
            # Without this, will just display as a defaultdict
            return "<Quoter %r>" % dict(self)

    @staticmethod
    def quote(string, safe='/'):
        """quote('abc def') -> 'abc%20def'
        Each part of a URL, e.g. the path info, the query, etc., has a
        different set of reserved characters that must be quoted.
        RFC 2396 Uniform Resource Identifiers (URI): Generic Syntax lists
        the following reserved characters.
        reserved    = ";" | "/" | "?" | ":" | "@" | "&" | "=" | "+" |
                      "$" | ","
        Each of these characters is reserved in some component of a URL,
        but not necessarily in all of them.
        By default, the quote function is intended for quoting the path
        section of a URL.  Thus, it will not encode '/'.  This character
        is reserved, but in typical usage the quote function is being
        called on a path where the existing slash characters are used as
        reserved characters.
        string and safe may be either str or bytes objects. encoding must
        not be specified if string is a str.
        """
        if isinstance(string, str):
            if not string:
                return string
            string = string.encode('utf-8', 'strict')

        if not string:
            return ''
        if isinstance(safe, str):
            # Normalize 'safe' by converting to bytes and removing non-ASCII chars
            safe = safe.encode('ascii', 'ignore')
        else:
            safe = bytes([c for c in safe if c < 128])
        if not string.rstrip(urlencode._ALWAYS_SAFE_BYTES + safe):
            return string.decode()
        try:
            quoter = urlencode._safe_quoters[safe]
        except KeyError:
            urlencode._safe_quoters[safe] = quoter = urlencode.Quoter(safe).__getitem__
        return ''.join([quoter(char) for char in string])


def get_player_id():
    """
    Retrieves the player's ID as defined by LMS. Most likely this is the player's MAC address.

    Should be changed to retrieve the player ID from the server.
    """
    mac = uio.open('/sys/class/net/wlan0/address').readline()
    return urlencode.quote(mac)


def send_single_lirc_command(remote, cmd):
    """
    Sends an IR command using the shell.
    """
    irsend_cmd = "%s SEND_ONCE %s %s" % (CONFIG['IRSEND'], remote, cmd)
    uos.system(irsend_cmd)


def send_lirc_commands(remote, commands):
    """
    Sends one or more LIRC commands, with optional pauses in between.
    """
    for cmd in commands:
        # Wait for specified number of milliseconds.
        if cmd['DELAY'] > 0:
            utime.sleep_ms(cmd['DELAY'])

        # Send the LIRC command.
        send_single_lirc_command(remote, cmd['CODE'])

def subscribe_to_squeezebox_events():
    """
    Opens a socket to the server and watch for relevant events.
    """
    power_pattern = r'%s power ([10])' % get_player_id()
    power_regex = ure.compile(power_pattern)

    try:
        server = usocket.getaddrinfo(CONFIG['SERVER']['HOST'], CONFIG['SERVER']['PORT'])[0][-1]
        s = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
        s.connect(server)
        s.send("subscribe power\n")
    except:
        print("Unable to connect; retrying in %d seconds" % CONFIG['SERVER']['RESTART_DELAY'])
        return

    # Loop until the socket expires
    p = uselect.poll()
    p.register(s, uselect.POLLIN)

    while True:
        for event in p.ipoll(2):
            sock  = event[0]
            flags = event[1]

            if (flags & uselect.POLLHUP) or (flags & uselect.POLLERR):
                # The socket got lost, let's try again soon.
                print("Lost socket connection; restarting in %d seconds" % CONFIG['SERVER']['RESTART_DELAY'])
                return

            data = sock.readline()
            if not data:
                # The socket got lost, let's try again soon.
                print("Lost socket connection; restarting in %d seconds" % CONFIG['SERVER']['RESTART_DELAY'])
                return
            else:
                match = power_regex.search(data)
                if match is not None:
                    power_status = match.group(1)
                    if power_status == b'1':
                        send_lirc_commands(CONFIG['REMOTE'], CONFIG['EVENTS']['POWER_ON'])
                    else:
                        send_lirc_commands(CONFIG['REMOTE'], CONFIG['EVENTS']['POWER_OFF'])

if __name__ == "__main__":
    # Loop forever. If the socket expires, restart it.
    while True:
        subscribe_to_squeezebox_events()
        utime.sleep(CONFIG['SERVER']['RESTART_DELAY'])