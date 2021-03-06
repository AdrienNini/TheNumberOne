import discord
from collections import namedtuple, ChainMap
from enum import Enum
from config_parser import DISCORD_TOKEN, PREFIX
import asyncio
import logging
import re
from datetime import datetime
import signal
from typing import get_type_hints
from textwrap import dedent
from os import listdir
from os.path import isfile, sep
from inspect import getfullargspec

logger = logging.getLogger(__name__)

def cast_using_type_hints(type_hints: dict, kwargs: dict):
    """
    Given type_hints of function and some key word arguments,
    cast each kwarg with the type given by typehints
    except for None values, None in kwargs stay None
    """
    return {key: value if value is None or key not in type_hints else type_hints[key](value)
            for key, value in kwargs.items()}

Command = namedtuple("Command", ["channels", "roles", "regexp", "callback"])
SubBot = namedtuple("SubBot", ["allow_commands", "callback"])
EventType = Enum("EventType", ["MSG", "REACT-ADD", "REACT-DEL"])
args_re = re.compile(r"\(\?P<(\S+?)>")

class DispatcherMeta(type):
    """Dispatcher Pattern"""
    def __new__(mcs, name, bases, attrs):
        commands = ChainMap()
        forwards = ChainMap()
        maps = commands.maps
        maps_fw = forwards.maps
        for base in bases:
            if isinstance(base, DispatcherMeta):
                maps.extend(base.__commands__.maps)
                maps_fw.extend(base.__forwards__.maps)
        attrs["__commands__"] = commands
        attrs["__forwards__"] = forwards
        attrs["dispatcher"] = property(lambda obj: commands)
        attrs["forwarder"] = property(lambda obj: forwards)
        cls = super().__new__(mcs, name, bases, attrs)
        return cls

    def set_command(cls, channels, roles, pattern, callback):
        """Register a callback"""
        cmd_name = callback.__name__.strip("_")
        logger.info("Register command '%s' in %s usable by %s with%s for callback %s.", 
                        cmd_name,
                        f"channels {channels}" if channels is not None else "all channels",
                        f"roles {roles}" if roles is not None else "any role",
                        f" pattern '{pattern}'" if pattern is not None else "out pattern",
                        callback)
        
        fap = getfullargspec(callback)
        if len(fap.args) < 1 and not fap.varargs:
            raise ValueError("Incorrect function signature. Function must accept at least one argument.")
        if pattern and fap.varkw is None:
            pargs = set(args_re.findall(pattern))
            kwoargs = set(fap.kwonlyargs)
            missings = pargs - kwoargs
            if missings:
                raise ValueError("Following pattern's named groups don't fit in function's keyword arguments: %s" % missings)
            else:
                unsued = kwoargs - pargs
                if unsued:
                    logger.warning("Unused function's arguments: %s" % unused)
        
        cls.__commands__[cmd_name] = \
            Command(channels, roles, re.compile(pattern) if pattern else None, callback)

    def add_forward(cls, channels, allow_commands, callback):
        for channel in channels:
            logger.info(f"Forward message from {channel} to {callback}.")
            if channel not in cls.__forwards__:
                cls.__forwards__[channel] = []
            cls.__forwards__[channel].append(SubBot(allow_commands, callback))

    def register(cls, channels, roles, pattern):
        """Decorator for register a command"""
        def wrapper(callback):
            cls.set_command(channels, roles, pattern, callback)
            return callback
        return wrapper

    def forward(cls, *channels, allow_commands=True):
        def wrapper(callback):
            cls.add_forward(channels, allow_commands, callback)
            return callback
        return wrapper

class TheNumberOne(discord.Client, metaclass=DispatcherMeta):
    def __init__(self):
        self.started_ = datetime.now()
        self.connected_ = False
        super().__init__()

    async def on_message(self, message):
        if message.author.id == self.user.id:
            return
        logger.debug(f"Message from {message.author} on {message.channel}: {message.content}")
        for subbot in self.forwarder.get(message.channel.name, []):
            logger.info(f"Forward to '{subbot.callback}'")
            if asyncio.iscoroutinefunction(subbot.callback):
                await subbot.callback(message)
            else:
                subbot.callback(message)
            if not subbot.allow_commands:
                return
            
        if message.content.startswith(PREFIX):
            cmd_name, *payload = message.content[1:].split(" ", 1)
        elif message.content.startswith(f"<@{self.user.id}>"):
            cmd_name, *payload = message.content.split(" ", 2)[1:]
        elif isinstance(message.channel, discord.Channel):
            return
        else:
            cmd_name, *payload = message.content.split(" ", 1)
        payload = payload[0] if payload else ""

        cmd = self.dispatcher.get(cmd_name)
        if not cmd:
            logger.warning(f"Command \"{cmd_name}\" not found.")
            await self.send_message(message.channel, f"<@{message.author.id}>, la commande \"{cmd_name}\" n'existe pas. Faites `!help` pour la liste des commandes.")
            return
        
        if cmd.channels is not None and message.channel.name not in cmd.channels:
            logger.warning(f"The command \"{cmd_name}\" is not available in the channel '{message.channel}'.")
            await self.send_message(message.channel, f"<@{message.author.id}>, la commande \"{cmd_name}\" n'est disponible que dans les salons suivants: {cmd.channels}.")
            return

        if cmd.roles is not None and discord.utils.find(lambda role: role.name in cmd.roles, message.author.roles) is None:
            logger.warning(f"The user {message.author} doesn't have the role needed to execute the command \"{cmd_name}\".")
            await self.send_message(message.channel, f"<@{message.author.id}>, la commande \"{cmd_name}\" n'est disponible que pour les roles suivants: {cmd.roles}.")
            return

        if cmd.regexp is not None:
            match = cmd.regexp.match(payload)
            if not match:
                logger.warning(f"Syntaxe error in command \"{cmd_name}\".")
                await self.send_message(message.channel, f"<@{message.author.id}>, la commande \"{cmd_name}\" n'a pas pu être exécutée car elle répond au pattern suivant: ```{cmd.regexp.pattern}\"```")
                return

            kwargs = cast_using_type_hints(
                type_hints=get_type_hints(cmd.callback),
                kwargs=match.groupdict())
            logger.debug("Dispatch to '%s' with kwargs %s", cmd.callback.__name__, kwargs)
        else:
            kwargs = {}
            logger.debug("Dispatch to '%s' with payload '%s'", cmd.callback.__name__, payload)

        try:
            if asyncio.iscoroutinefunction(cmd.callback):
                await cmd.callback(message, **kwargs)
            else:
                cmd.callback(message, **kwargs)
        except Exception as exc:
            logger.exception(f"Error in dispatched command.")
            await self.send_message(message.channel, "Internal error...")

    async def on_reaction_add(self, reaction, user):
        pass

    async def on_reaction_remove(self, reaction, user):
        pass

    async def on_ready(self):
        if not self.connected_:
            self.change_presence(game=discord.Game(name=f"{PREFIX}help", type=0))
            self.connected_ = True
            logger.info("Connected. Loading plugins...")
            try:
                for file in  listdir("plugins"):
                    if isfile(f"plugins{sep}{file}") and file != "__init__.py" and file.endswith(".py"):
                        logger.info(f"Load plugin '{file[:-3]}'")
                        __import__(f"plugins.{file[:-3]}")
                    else:
                        logger.info(f"Skip {file}")
            except:
                logger.exception(":(")

            logger.info("Done in %ss", (datetime.now() - self.started_).total_seconds())
            await self.purge_from(discord.utils.find(lambda chan: chan.name == "test-bot", list(self.servers)[0].channels), limit=200)

@TheNumberOne.register(None, None, None)
async def ping(message):
    """Répond pong dans les pus bref délais"""
    await thenumberone.send_message(message.channel, "Pong !")


@TheNumberOne.register(None, None, r"(?P<cmd_name>\S+)?")
async def help_(message, *_, cmd_name: str = ""):
    """Affiche la liste des commandes ou l'aide pour une commande spéficique"""
    if cmd_name:
        cmd = thenumberone.dispatcher.get(cmd_name)
        if cmd is None:
            await thenumberone.send_message(message.channel, f"<@{message.author.id}>, la commande \"{command}\" n'existe pas")
            return

        if cmd.regexp.pattern:
            fap = getfullargspec(cmd.callback)
            args = args_re.findall(cmd.regexp.pattern)
            if fap.kwonlydefaults:
                cli = " ".join([f"[{arg}]" if arg in fap.kwonlydefaults else f"<{arg}>" for arg in args])
            else:
                cli = " ".join([f"<{arg}>" for arg in args])
        
        payload = dedent("""
        Commande **{0}**: {1}
        Utilisable dans: {2}
        Utilisable par: {3}
        Pattern pour les arguments: {4}
        Utilisation: ```
        {5}{0}{6}```""".format(
            cmd_name,
            re.sub(r"\s+", " ", cmd.callback.__doc__) if cmd.callback.__doc__ else "On a oublié de m'écrire une doc...",
            "*partout*" if cmd.channels is None else cmd.channels,
            "*tout le monde*" if cmd.roles is None else cmd.roles,
            "*N/A*" if cmd.regexp is None else f"`{cmd.regexp.pattern}`",
            PREFIX,
            "" if cmd.regexp is None else " " + cli
        )[1:])
        await thenumberone.send_message(message.channel, payload)
    else:
        await thenumberone.send_message(message.channel, "Commandes disponibles: " + " ".join(thenumberone.dispatcher.keys()))


def start():
    logging.root.level = logging.NOTSET
    stdout = logging.StreamHandler()
    stdout.level = logging.DEBUG
    stdout.formatter = logging.Formatter(
        "[{levelname}] <{name}:{funcName}> {message}", style="{")
    logging.root.addHandler(stdout)

    logging.getLogger("discord").level = logging.WARNING
    logging.getLogger("websockets").level = logging.WARNING

    global thenumberone
    thenumberone = TheNumberOne()
    async def mainloop():
        await thenumberone.login(DISCORD_TOKEN)
        await thenumberone.connect()

    loop = asyncio.get_event_loop()
    stop = asyncio.Future()
    loop.add_signal_handler(signal.SIGTERM, stop.set_result, None)

    asyncio.ensure_future(mainloop())

    logger.info("Connecting...")
    try:
        loop.run_until_complete(stop)
    except KeyboardInterrupt:
        logger.info("CTRL-C received, stopping.")
    except Exception as exc:
        logger.exception("Runtime error, stopping.")
    else:
        logger.info("SIGTERM received, stopping.")
    if not thenumberone.is_closed:
        loop.run_until_complete(thenumberone.logout())
    loop.close()
    logger.info("Stopped.")
