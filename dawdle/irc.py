import asyncio
import collections
import logging
import os
import re
import textwrap
import time

from dawdle import conf
from dawdle.log import log

def grouper(iterable, n):
    """Collect data into fixed-length chunks or blocks

    grouper('ABCDEFG', 3, 'x') --> ABC DEF Gxx
    """
    return [iterable[i:i+n] for i in range(0, len(iterable), n)]


class IRCClient:
    """IRCClient acts as a layer between the IRC protocol and the bot protocol.

    This class has the following responsibilities:
    - Connection and disconnection
    - Nick recovery
    - Output throttling
    - NickServ authentication
    - IRC message decoding and parsing
    - Tracking users in the channel
    - Calling methods on the bot interface
 """
    MESSAGE_RE = re.compile(r'^(?:@(\S*) )?(?::([^ !]*)(?:!([^ @]*)(?:@([^ ]*))?)?\s+)?(\S+)\s*((?:[^:]\S*(?:\s+|$))*)(?::(.*))?')

    Message = collections.namedtuple('Message', ['tags', 'src', 'user', 'host', 'cmd', 'args', 'trailing', 'line', 'time'])


    class User:
        """An IRC user in the channel."""

        def __init__(self, nick, userhost, modes, joined):
            self.nick = nick
            self.userhost = userhost
            self.modes = set(modes)
            self.joined = joined


    def __init__(self, bot):
        self._bot = bot
        self._writer = None
        self._nick = conf.get("botnick")
        self._bytes_sent = 0
        self._bytes_received = 0
        self._caps = set()
        self._server = None
        self._messages_sent = 0
        self._writeq = []
        self._flushq_task = None
        self._prefixmodes = {}
        self._maxmodes = 3
        self._modetypes = {}
        self._users = {}
        self._caps = set()             # all enabled capabilities
        self.quitting = False


    async def connect(self, addr, port):
        """Connect to IRC network and handle messages."""
        reader, self._writer = await asyncio.open_connection(addr, port, ssl=True, local_addr=conf.get("localaddr"))
        self._server = addr
        self._connected = True
        self._messages_sent = 0
        self._writeq = []
        self._flushq_task = None
        self._prefixmodes = {}
        self._maxmodes = 3
        self._modetypes = {}
        self._users = {}
        self._caps = set()             # all enabled capabilities
        self.sendnow("CAP REQ :multi-prefix userhost-in-names")
        self.sendnow("CAP END")
        if 'BOTPASS' in os.environ:
            self.sendnow(f"PASS {os.environ['BOTPASS']}")
        self.sendnow(f"NICK {conf.get('botnick')}")
        self.sendnow(f"USER {conf.get('botuser')} 0 * :{conf.get('botrlnm')}")
        self._bot.connected(self)
        while True:
            line = await reader.readline()
            if not line:
                if self._flushq_task:
                    self._flushq_task.cancel()
                self._bot.disconnected()
                await reader.close()
                break
            self._bytes_received += len(line)
            # Assume utf-8 encoding, fall back to latin-1, which has no invalid encodings from bytes.
            try:
                line = str(line, encoding='utf8')
            except UnicodeDecodeError:
                line = str(line, encoding='latin-1')
            line = line.rstrip('\r\n')
            loglevel = logging.SPAMMY if re.match(r"^PING ", line) else logging.DEBUG
            log.log(loglevel, "<- %s", line)
            msg = self.parse_message(line)
            self.dispatch(msg)


    def send(self, s, loglevel=logging.DEBUG):
        """Send throttled messages."""
        b = bytes(s+"\r\n", encoding='utf8')

        if not conf.get("throttle"):
            log.log(loglevel, "-> %s", s)
            self._writer.write(b)
            self._bytes_sent += len(b)
            return

        if self._messages_sent < conf.get("throttle_rate"):
            log.log(loglevel, "(%d)-> %s", self._messages_sent, s)
            self._writer.write(b)
            self._messages_sent += 1
            self._bytes_sent += len(b)
        else:
            self._writeq.append(b)

        # The flushq task will reset messages_sent after the throttle period.
        if not self._flushq_task:
            self._flushq_task = asyncio.create_task(self.flushq_task())


    def sendnow(self, s, loglevel=logging.DEBUG):
        """Send messages ignoring throttle."""
        log.log(loglevel, "=> %s", s)
        b = bytes(s+"\r\n", encoding='utf8')
        self._writer.write(b)
        self._messages_sent += 1
        self._bytes_sent += len(b)
        if conf.get("throttle") and not self._flushq_task:
            self._flushq_task = asyncio.create_task(self.flushq_task())


    async def flushq_task(self):
        """Flush send queue and release throttle."""
        await asyncio.sleep(THROTTLE_PERIOD)
        self._messages_sent = max(0, self._messages_sent - conf.get("throttle_rate"))
        while self._writeq:
            while self._writeq and self._messages_sent < conf.get("throttle_rate"):
                log.debug("(%d)~> %s", self._messages_sent, str(self._writeq[0], encoding='utf8').rstrip())
                self._writer.write(self._writeq[0])
                self._messages_sent += 1
                self._bytes_sent += len(self._writeq[0])
                self._writeq = self._writeq[1:]
            if self._writeq:
                await asyncio.sleep(conf.get("throttle_period"))
                self._messages_sent = max(0, self._messages_sent - conf.get("throttle_rate"))

        self._flushq_task = None


    def parse_message(self, line):
        """Parse IRC line into a Message."""
        # Parse IRC message with a regular expression
        match = IRCClient.MESSAGE_RE.match(line)
        if not match:
            return None
        rawtags, src, user, host, cmd, args, trailing = match.groups()
        # IRCv3 supports tags
        tags = dict()
        if rawtags is not None and rawtags != "":
            for pairstr in rawtags.split(';'):
                pair = pairstr.split('=')
                if len(pair) == 2:
                    tags[pair[0]] = re.sub(r"\\(.)",
                                           lambda m: {":": ";", "s": " ", "r": "\r", "n": "\n"}.get(m[1], m[1]),
                                           pair[1])
                else:
                    tags[pair[0]] = None
        # Arguments before the trailing argument (after the colon) are space-delimited
        if args == "":
            args = []
        else:
            args = args.rstrip().split(' ')
        # There's nothing special about the trailing argument except it can have spaces.
        if trailing is not None:
            args.append(trailing)
        # Numeric responses specify a useless target afterwards
        if re.match(r'\d+', cmd):
            args = args[1:]
        # Support time tag, which allows servers and bouncers to send history
        if 'time' in tags:
            msgtime = time.mktime(time.strptime(tags['time'], "%Y-%m-%dT%H:%M:%S"))
        else:
            msgtime = time.time()
        return IRCClient.Message(tags, src, user, host, cmd, args, trailing, line, msgtime)


    def dispatch(self, msg):
        """Dispatch the IRC command to a handler method."""
        if hasattr(self, "handle_"+msg.cmd.lower()):
            getattr(self, "handle_"+msg.cmd.lower())(msg)


    def handle_ping(self, msg):
        """PING - sends PONG back to server for keepalive."""
        self.sendnow(f"PONG :{msg.trailing}", loglevel=logging.SPAMMY)


    def handle_005(self, msg):
        """RPL_ISUPPORT - server features and information"""
        self._server = msg.src
        params = dict([arg.split('=') if '=' in arg else (arg, arg) for arg in msg.args])
        if 'MODES' in params:
            self._maxmodes = int(params['MODES'])
        if 'PREFIX' in params:
            m = re.match(r'\(([^)]*)\)(.*)', params['PREFIX'])
            if m:
                self._prefixmodes.update(zip(m[2], m[1]))
                for mode in m[1]:
                    self._modetypes[mode] = 2
        if 'CHANMODES' in params:
            m = re.match(r'([^,]*),([^,]*),([^,]*),(.*)', params['CHANMODES'])
            if m:
                for mode in m[1]:
                    self._modetypes[mode] = 1 # adds to a list and always has a parameter
                for mode in m[2]:
                    self._modetypes[mode] = 2 # changes a setting and always has param
                for mode in m[3]:
                    self._modetypes[mode] = 3 # only has a parameter when set
                for mode in m[4]:
                    self._modetypes[mode] = 4 # never has a parameter


    def handle_376(self, msg):
        """RPL_ENDOFMOTD - server is ready"""
        self.mode(conf.get("botnick"), conf.get("botmodes"))
        self.join(conf.get("botchan"))


    def handle_422(self, msg):
        """ERR_NOTMOTD - server is ready, but without a MOTD"""
        self.mode(conf.get("botnick"), conf.get("botmodes"))
        self.join(conf.get("botchan"))


    def handle_352(self, msg):
        """RPL_WHOREPLY - Response to WHO command"""
        self.add_user(msg.args[4],
                      f"{msg.src}!{msg.args[1]}@{msg.args[2]}",
                      [self._prefixmodes[p] for p in msg.args[5][1:]], # Format is [GH]\S*
                      msg.time)


    def handle_315(self, msg):
        """RPL_ENDOFWHO - End of WHO command response"""
        self._bot.ready()


    def handle_353(self, msg):
        """RPL_NAMREPLY - names in the channel"""
        if 'userhost-in-names' not in self._caps:
            return
        prefixes=''.join(self._prefixmodes.keys())
        userhost_re = re.compile(f"([{prefixes}]*)" + r"((\S+)!\S+@\S+)")
        for u in msg.trailing.split(' '):
            m = userhost_re.match(u)
            if m:
                self.add_user(m[3], m[2], [self._prefixmodes[p] for p in m[1]], msg.time)


    def handle_366(self, msg):
        """RPL_ENDOFNAMES - the actual end of channel joining"""
        # We know who is in the channel now
        if conf.has("botopcmd"):
            self.sendnow(re.sub(r'%botnick%', self._nick, conf.get("botopcmd")))
        if 'userhost-in-names' in self._caps:
            self._bot.ready()
        else:
            self.send(f"WHO {conf.get('botchan')}")


    def handle_433(self, msg):
        """ERR_NICKNAME_IN_USE - try another nick"""
        self._nick = self._nick + "0"
        self.nick(self._nick)
        if conf.has("botghostcmd"):
            self.send(conf.get("botghostcmd"))


    def handle_cap(self, msg):
        """CAP - notification of capability"""
        # We only care about enabled capabilities.
        if msg.args[1] == "ACK":
            self._caps.update(msg.args[2].split(' '))


    def handle_join(self, msg):
        """JOIN - bot or user joined the channel."""
        self.add_user(msg.src, f"{msg.src}!{msg.user}@{msg.host}", [], msg.time)


    def handle_part(self, msg):
        """PART - bot or user left the channel."""
        user = self.remove_user(msg.src)
        self._bot.nick_parted(user)


    def handle_kick(self, msg):
        """KICK - user was kicked from the channel."""
        user = self.remove_user(msg.args[1])
        self._bot.nick_kicked(user)


    def handle_mode(self, msg):
        """MODE - bot or channel changed its mode."""
        # ignore mode changes to everything except the bot channel
        if msg.args[0] != conf.get("botchan"):
            return
        changes = []
        params = []
        for arg in msg.args[1:]:
            m = re.match(r'([-+])(.*)', arg)
            if m:
                changes.extend([(m[1], term) for term in m[2]])
            else:
                params.append(arg)
        for change in changes:
            # all this modetype machinery is required to accurately parse modelines
            modetype = self._modetypes[change[1]]
            if modetype == 1 or modetype == 2 or (modetype == 3 and change[0] == '+'):
                param = params.pop()
                if modetype != 2:
                    continue
                if change[0] == '+':
                    self._users[param].modes.add(change[1])
                    if param == self._nick and change[1] == 'o':
                        # Acquiring op is special to the bot
                        self._bot.acquired_ops()
                else:
                    self._users[param].modes.discard(change[1])


    def handle_nick(self, msg):
        """NICK - bot or user had its nick changed."""

        # Do this first so that the user still matches the player.
        self._bot.nick_changed(self._users[msg.src], msg.args[0])

        self._users[msg.args[0]] = self._users[msg.src]
        self._users[msg.args[0]].nick = msg.args[0]
        del self._users[msg.src]

        if msg.src == self._nick:
            # Update my nick
            self._nick = msg.args[0]
            return

        if msg.src == conf.get("botnick"):
            # Grab my nick that someone left
            self.nick(conf.get("botnick"))


    def handle_quit(self, msg):
        """QUIT - bot or user was disconnected."""
        if msg.src == conf.get("botnick"):
            # Grab my nick that someone left
            self.nick(conf.get("botnick"))
        user = self.remove_user(msg.src)
        if conf.get("detectsplits") and re.match(r'\S+\.\S+ \S+\.\S+', msg.trailing):
            # Don't penalize on netsplit
            self._bot.netsplit(user)
        elif re.match(r"Read error|Ping timeout", msg.trailing):
            self._bot.nick_dropped(user)
        else:
            self._bot.nick_quit(user)


    def handle_notice(self, msg):
        """NOTICE - Message sent, used to prevent loops in bots."""
        if msg.args[0] != self._nick and msg.src in self._users and self.user_is_ok(msg):
            # we ignore private notices
            self._bot.channel_notice(self._users[msg.src], msg.trailing)


    def handle_privmsg(self, msg):
        """PRIVMSG - Message sent."""
        if msg.src not in self._users:
            # Server messages
            return
        if msg.args[0] == self._nick:
            self._bot.private_message(self._users[msg.src], msg.trailing)
        elif self.user_is_ok(msg):
            self._bot.channel_message(self._users[msg.src], msg.trailing)


    def add_user(self, nick, userhost, modes, joined):
        """Adds channel user with the given properties."""
        self._users[nick] = IRCClient.User(nick, userhost, modes, joined)


    def remove_user(self, nick):
        """Remove user with the given nick.  Returns that user."""
        user = self._users[nick]
        del self._users[nick]
        if len(self._users) == 1 and not self.bot_has_ops():
            # Try to acquire ops by leaving and joining
            self.sendnow(f"PART {conf.get('botchan')} :Acquiring ops")
            self.sendnow(f"JOIN {conf.get('botchan')}")
        return user


    def user_is_ok(self, msg):
        """Check to see if msg should cause user to be kickbanned."""
        if not conf.get("doban"):
            # Bot doesn't do bans
            return True
        if not self.bot_has_ops():
            # Bot can't do bans
            return True
        if msg.src == self._nick:
            # Bot is always ok
            return True
        if msg.src not in self._users:
            # Not in channel - maybe channel could use mode +n
            return False
        if msg.time > self._users[msg.src].joined + conf.get("bannable_time"):
            # Been in channel for a while, prob ok?
            return True

        for host in re.findall(r"https?://([^/]+)/", msg.trailing):
            if host not in conf.get("okurls"):
                # User not okay
                self.kickban(msg.src)
                return False
        return True


    def match_user(self, nick, userhost):
        """Return True if the nick and userhost match an existing user."""
        return (nick in self._users and userhost == self._users[nick].userhost)


    def bot_has_ops(self):
        """Return True if the bot has ops in the channel."""
        return (self._nick in self._users and 'o' in self._users[self._nick].modes)


    def kickban(self, nick):
        """Kick a nick from the channel and ban them."""
        self.sendnow(f"MODE {conf.get('botchan')} +b {nick}")
        self.sendnow(f"KICK {conf.get('botchan')} {nick} :No advertising")


    def nick(self, nick):
        """Send nick change request."""
        self.sendnow(f"NICK {nick}")


    def join(self, channel):
        """Send channel join request."""
        self.sendnow(f"JOIN {channel}")


    def notice(self, target, text):
        """Send notice text to target."""
        for line in textwrap.wrap(text, width=conf.get("message_wrap_len")):
            self.send(f"NOTICE {target} :{line}")


    def grant_voice(self, *targets):
        for subset in grouper(targets, self._maxmodes):
            self.send(f"MODE {conf.get('botchan')} +{'v' * len(subset)} {' '.join(subset)}")


    def revoke_voice(self, *targets):
        for subset in grouper(targets, self._maxmodes):
            self.send(f"MODE {conf.get('botchan')} -{'v' * len(subset)} {' '.join(subset)}")


    def mode(self, target, *modeinfo):
        """Send mode change request."""
        for modes in grouper(modeinfo, self._maxmodes):
            self.send(f"MODE {target} {' '.join(modes)}")


    def chanmsg(self, text):
        """Send message text to bot channel."""
        for line in textwrap.wrap(text, width=conf.get("message_wrap_len")):
            self.send(f"PRIVMSG {conf.get('botchan')} :{line}")


    def quit(self, *text):
        """Send quit request to server."""
        self.quitting = True
        if text:
            self.sendnow(f"QUIT :{text}")
        else:
            self.sendnow("QUIT")
