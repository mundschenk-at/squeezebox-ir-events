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

import sys
import uio
import ujson
import uos
import ure
import uselect
import usocket
import utime

from ucollections import namedtuple


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

        # Note: strings are encoded as UTF-8. This is only an issue if it
        # contains unescaped non-ASCII characters, which URIs should not.
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
                    char = urlencode._unquote_cache[code] = bytes(
                        [int(code, 16)])
                append(char)
                append(item[2:])
            except KeyError:
                append(b'%')
                append(item)

        return b''.join(res).decode('utf-8')

    class Quoter:
        """
        A mapping from bytes (in range(0,256)) to strings.
        String values are percent-encoded byte values, unless the key < 128,
        and in the "safe" set (either the specified safe set, or default set).
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
            # Normalize 'safe' by converting to bytes and removing
            # non-ASCII characters.
            safe = safe.encode('ascii', 'ignore')
        else:
            safe = bytes([c for c in safe if c < 128])
        if not string.rstrip(urlencode._ALWAYS_SAFE_BYTES + safe):
            return string.decode()
        try:
            quoter = urlencode._safe_quoters[safe]
        except KeyError:
            urlencode._safe_quoters[safe] = quoter = urlencode.Quoter(
                safe).__getitem__
        return ''.join([quoter(char) for char in string])


class SBIREvents:
    """
    A class encapsulating the IR events handler.
    """

    def __init__(self, config_file, player_name=None):
        """
        Initialize the IR events handler from the command line arguments.
        """
        self.socket = None
        self.power_regex = None
        self.volume_regex = None

        # Volume handling. Should be detected by querying the server.
        self.volume_lock = True   # Fake config setting, should be queried
        self.changed_volume = None
        self.changed_steps = None
        self.previous_volume = 100

        # Some primitive configuration file parsing.
        try:
            config = ujson.load(uio.open(config_file))
        except OSError:
            print('Error loading configuration file "{}".'.format(config_file))
            raise

        # Set player name.
        self.player_name = config.get('player_name')
        if player_name is not None:
            self.player_name = player_name
        if self.player_name is None:
            raise ValueError('No player name.')

        # Server settings.
        Server = namedtuple('Server', ('host', 'port', 'restart_delay'))
        self.server = Server(
            config['server']['host'],
            config['server']['port'],
            config['server']['restart_delay']
        )

        # Default settings.
        self.default_script = config['default_script']

        # Event commands.
        self.events = config['events']

    def get_player_id(self, player_name):
        """
        Retrieve the player's ID as defined by LMS. Most likely this is the
        player's MAC address.
        """
        player_count = 0
        player_id = None

        # Retrieve player count.
        player_count = int(self.sb_query('player count'))

        # Retrieve the complete players information.
        players = ure.compile(
            r' playerindex%3A[0-9]+ ').split(
                self.sb_command('players 0 {}', player_count)
        )

        # The first item will be the command we just sent.
        players.pop(0)

        # Prepare lookup expression.
        player_name_regex = ure.compile(
            'name%s%s ' % (r'%3A', urlencode.quote(player_name)))

        for player in players:
            if player_name_regex.search(player):
                player_id = urlencode.unquote(
                    ure.match(r'playerid%3A([^ ]+) ', player).group(1))
                break

        return player_id

    def run_single_command(self, script, param=None):
        """
        Run a single command using the shell.
        """
        cmd = script
        if param is not None and param != '':
            cmd += ' ' + param

        print("Running '%s' shell command" % cmd)
        uos.system(cmd)

    def run_commands(self, commands, value=None):
        """
        Run commands for an event, with optional pauses in between.
        """
        for cmd in commands:
            # Wait for specified number of milliseconds.
            delay = cmd.get('delay', 0)
            if delay > 0:
                utime.sleep_ms(delay)

            # Send the LIRC command.
            script = cmd.get('script', self.default_script)
            if script is None:
                continue

            param = cmd.get('param')
            if value is not None and cmd.get('include_value', False):
                param += str(value)

            self.run_single_command(script, param)

    def sb_parse_result(self, regex, string, group=1):
        """
        Parses an LMS command result by using a regex.
        """
        if isinstance(regex, str):
            return ure.match(regex, string).group(group)
        else:
            return regex.match(string).group(group)

    def sb_command(self, command, *args):
        """
        Send a command to the LMS server and returns the (immediate) result.
        The final newline will outomatically be added and all optional
        arguments will be URL encoded.
        """
        self.socket.write(self.sb_prepare_string(command, *args) + '\n')
        return self.socket.readline().decode('utf-8')

    def sb_prepare_string(self, string, *args):
        """
        Prepare a string for being sent to the LMS server.
        """
        # String arguments need to be URL encoded.
        args = list(args)
        for num, argument in enumerate(args):
            if isinstance(argument, str):
                args[num] = urlencode.quote(argument)

        return string.format(*args)

    def sb_query(self, query, *args):
        """
        Sends a query command and parses the result. The final '?' and newline
        will automatically be added to the query.
        """
        prepared = self.sb_prepare_string(query, *args)

        return self.sb_parse_result(
            # Match everything after the returned command string.
            prepared + ' (.*)',
            # Add query indicator to command string.
            self.sb_command(prepared + ' ?')
        )

    def connect(self, server):
        """
        Open a socket to the server and watch for relevant events.
        """
        try:
            addr = usocket.getaddrinfo(server.host, server.port)[0][-1]
            self.socket = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
            self.socket.connect(addr)
        except OSError:
            print("Unable to connect; retrying in %d seconds" %
                  server.restart_delay)
            return

    def handle_power_event(self, match):
        """
        Handle power on and off events.
        """
        power_status = match.group(1)
        if power_status == '1':
            self.run_commands(self.events['power:on'])
        else:
            self.run_commands(self.events['power:off'])

    def handle_volume_event(self, match):
        """
        Handle volume change events.
        """
        volume = int(match.group(1))
        steps = round((volume - self.previous_volume) / 5)

        if self.volume_lock:
            if self.changed_volume is None and \
               self.changed_steps is None and \
               steps != 0:
                # Store initial volume change, but don't call script yet.
                self.changed_volume = volume
                self.changed_steps = steps
                volume = self.previous_volume
                steps = 0
            else:
                # Ignore the second volume event
                volume = self.changed_volume
                steps = self.changed_steps
                self.changed_volume = None
                self.changed_steps = None
        else:
            self.previous_volume = volume

        if steps and steps != 0:
            if steps < 0:
                self.run_commands(self.events['volume:lower'], steps)
            else:
                self.run_commands(self.events['volume:raise'], steps)

    def wait_for_events(self, poll):
        """
        Wait for events sent by the LMS server.
        """
        for (sock, flags, *_) in poll.ipoll(2):
            if (flags & uselect.POLLHUP) or (flags & uselect.POLLERR):
                # The socket got lost, let's try again soon.
                raise ValueError(
                        'Lost socket connection; restarting in {} seconds.'
                        .format(self.server.restart_delay)
                    )

            data = sock.readline().decode('utf-8')
            if not data:
                # The socket got lost, let's try again soon.
                raise ValueError(
                        'No data (socket probably lost connection); '
                        'restarting in {} seconds.'
                        .format(self.server.restart_delay)
                    )

            match = self.power_regex.search(data)
            if match is not None:
                self.handle_power_event(match)
                continue

            match = self.volume_regex.search(data)
            if match is not None:
                self.handle_volume_event(match)
                continue

    def prepare_events_regexes(self, player_id):
        """
        Prepare regular expressions used for events parsing. The server
        connection must be already open.
        """
        player_id = urlencode.quote(player_id)

        self.power_regex = ure.compile('{} power ([10])'.format(player_id))
        self.volume_regex = ure.compile(
            '{} mixer volume ([0-9]+)'.format(player_id))

    def listen(self):
        """
        Listen for events affecting the player.
        """
        self.connect(self.server)

        # We need the player ID to identify relevant events.
        player_id = self.get_player_id(self.player_name)
        self.prepare_events_regexes(player_id)

        # Retrieve current volume for handling relative changes.
        self.previous_volume = int(self.sb_query('{} mixer volume', player_id))

        # Subscribe to events.
        self.sb_command('subscribe power,mixer')

        # Loop until the socket expires
        p = uselect.poll()
        p.register(self.socket, uselect.POLLIN)

        while True:
            try:
                self.wait_for_events(p)
            except ValueError as e:
                print(e)
                return

    def wait_until_restart(self):
        """
        Wait the configured time before trying to resume the connection.
        """
        utime.sleep(self.server.restart_delay)


if __name__ == "__main__":
    handler = SBIREvents(*sys.argv[1:3])

    # Loop forever. If the socket expires, restart it.
    while True:
        handler.listen()
        handler.wait_until_restart()
