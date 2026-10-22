import logging

import discord
from discord import app_commands
from discord.ext import commands

from core.bot import current_guild_prefix
from utils.emojis import icon
from core.settings import settings
from utils.ui import component_card, edit_component_card, panel, send_component_card


HELP_PAGE_SIZE = 3
log = logging.getLogger("zyven.helpdesk")
HELP_NAV_EMOJI_NAMES = {
    "first": "left",
    "previous": "previous",
    "delete": "delete",
    # The supplied right/skip emoji assets are visually reversed in Discord,
    # so assign the skip asset to Next and the right asset to Last to render
    # the familiar order: > followed by >>.
    "next": "skip",
    "last": "right",
}


async def delete_help_message(interaction: discord.Interaction) -> None:
    """Delete the complete help card attached to a trash-button interaction."""
    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
    except discord.NotFound:
        return
    try:
        await interaction.delete_original_response()
        return
    except discord.NotFound:
        return
    except discord.HTTPException:
        # Some Discord clients expose the component's message more reliably
        # than the interaction original-response endpoint; try that as fallback.
        message = interaction.message
        if message is None:
            log.exception("Could not delete help panel for interaction %s", interaction.id)
            return
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.exception("Could not delete help panel message %s", message.id)
GROUP_COMMAND_DESCRIPTIONS = {
    "gstart": "Start a giveaway: gstart <time> <winners> <prize>.",
    "gconfig": "Show the giveaway staff role and image configuration.",
    "gwystaff": "Configure the giveaway staff role.",
    "gwystaff role": "Set the role allowed to manage giveaways.",
    "gembed": "Configure the image shown in new giveaways.",
    "gembed set": "Set the image shown in new giveaways.",
    "gembed reset": "Remove the image shown in new giveaways.",
    "glist": "List ongoing giveaways with their end times and links.",
    "greet setup": "Configure the welcome channel, message, and display style.",
    "autoresponder create": "Create a new autoresponder.",
    "autoresponder delete": "Delete an existing autoresponder.",
    "autoresponder edit": "Edit an existing autoresponder.",
    "autoresponder config": "List all autoresponders in the server.",
    "reactionrole add": "Add a reaction role to a message.",
    "reactionrole remove": "Remove a reaction role from a message.",
    "reactionrole list": "List configured reaction roles in this server.",
    "reactionrole reset": "Clear all reaction roles in this server.",
    "messagechannels": "Choose which channels count toward the message leaderboard.",
    "messagechannels add": "Add one text channel at a time; up to 10 selected channels total count toward the message leaderboard.",
    "messagechannels remove": "Remove a channel from selected message tracking.",
    "messagechannels show": "Show channels selected for message tracking.",
    "messagechannels reset": "Clear the selection and count all non-blacklisted channels.",
    "autoreact": "Manage automatic reactions for this server.",
    "autoreact add": "Add an automatic reaction trigger.",
    "autoreact remove": "Remove an automatic reaction trigger.",
    "autoreact list": "List this server's automatic reaction triggers.",
    "autoreact reset": "Remove all automatic reaction triggers from this server.",
    "setup reqrole": "Set the required role for custom role commands.",
    "setup guest": "Configure the guest role in this server.",
    "setup staff": "Configure the staff role in this server.",
    "setup girl": "Configure the girl role in this server.",
    "setup friend": "Configure the friend role in this server.",
    "setup vip": "Configure the VIP role in this server.",
    "setup create": "Create a custom role command.",
    "setup delete": "Delete a custom role command.",
    "setup list": "List all custom role commands.",
    "setup config": "Show the current custom role configuration.",
    "setup reset": "Reset all custom role configuration.",
    "vcrole": "Configure the role automatically added to members while they are in voice channels.",
    "vcrole add": "Adds a role to the vcrole list",
    "vcrole remove": "Removes the role from vcrole list",
    "vcrole config": "Shows the Current vcrole in this Guild",
    "extraowner add": "Add a trusted member who can access owner-level security tools.",
    "extraowner view": "Show the current trusted Extraowners.",
    "extraowner reset": "Remove all trusted Extraowners from this server.",
    "ignore command": "Manage commands ignored in this guild.",
    "ignore channel": "Manage channels where commands are ignored.",
    "ignore bypass": "Manage users allowed to bypass ignore rules.",
    "ignore user": "Manage users who cannot run commands.",
    "ignore command add": "Add a command to the ignore list.",
    "ignore command remove": "Remove a command from the ignore list.",
    "ignore command show": "Show ignored commands.",
    "ignore channel add": "Add a channel to the ignore list.",
    "ignore channel remove": "Remove a channel from the ignore list.",
    "ignore channel show": "Show ignored channels.",
    "ignore user add": "Add a user to the ignore list.",
    "ignore user remove": "Remove a user from the ignore list.",
    "ignore user show": "Show ignored users.",
    "ignore bypass add": "Add a user who can bypass ignore rules.",
    "ignore bypass remove": "Remove a user from the ignore bypass list.",
    "ignore bypass show": "Show users who bypass ignore rules.",
    "role all": "Add a role to every eligible server member.",
    "role create": "Create a new role in this server.",
    "role delete": "Delete a role after moderator confirmation.",
    "role rename": "Change the name of an existing role.",
    "role add": "Add a role to one member.",
    "role remove": "Remove a role from one member.",
    "role humans": "Add a role to human members, or use `add` to run the bulk action.",
    "role humans add": "Add a role to all eligible human members.",
    "role bots": "Add a role to bot members, or use `add` to run the bulk action.",
    "role bots add": "Add a role to all eligible bot members.",
    "role unverified": "Add a role to eligible members who have not completed verification.",
    "media setup": "Set a text channel to allow media attachments only.",
    "media remove": "Remove the media-only channel configuration.",
    "media config": "Show the configured media-only channel.",
    "media bypass add": "Add a member or role that can post non-media messages in the media channel.",
    "media bypass remove": "Remove a member or role from the media-channel bypass list.",
    "media bypass show": "Show members who can bypass media-only restrictions.",
    "play": "Play a song or add a search result/URL to the queue.",
    "search": "Search for a song and choose a music platform.",
    "pause": "Pause or resume the current song.",
    "resume": "Resume paused music.",
    "skip": "Skip the current song.",
    "previous": "Play the previous song again.",
    "stop": "Stop playback and leave the voice channel.",
    "join": "Join your voice channel.",
    "leave": "Stop playback and disconnect from voice.",
    "queue": "Show the current song and upcoming queue.",
    "nowplaying": "Show the current song and player controls.",
    "volume": "Set playback volume from 0 to 200 percent.",
    "loop": "Toggle repeat for the current song.",
    "shuffle": "Toggle shuffle for queued songs.",
    "autoplay": "Toggle similar-song autoplay when the queue is empty.",
    "ticket close": "Close this ticket.",
    "ticket add": "Add a member to this ticket.",
    "ticket remove": "Remove a member from this ticket.",
    "ticket transcript": "Generate and post an HTML transcript.",
    "ticket ticketinfo": "View details about this ticket.",
    "ticket setup": "Configure and post the ticket button panel.",
    "ticket setup2": "Configure and post the ticket dropdown panel.",
    "ticket panel2": "Repost the configured dropdown panel.",
    "ticket setform": "Configure multi-step forms for a ticket category.",
    "ticket help": "Open the ticket system help menu.",
    "rank": "View a member's leveling rank card, level, XP progress, and reward role.",
    "profile": "View a member's leveling profile, ranks, activity, and XP history.",
    "levelboard": "Browse the server leveling leaderboard. Choose XP, messages, or voice.",
    "givexp": "Give or remove XP from a member. Requires leveling administrator permissions.",
    "setlevel": "Set a member to a specific level. Requires leveling administrator permissions.",
    "resetxp": "Reset a member's leveling XP, or reset all members with `resetxp all`. Requires Manage Server.",
    "claimreward": "Claim the leveling reward roles your level has unlocked.",
    "multiplier": "View or configure XP multipliers for a channel or role.",
    "rewards": "View, add, or remove level reward roles.",
    "serverstats": "Show this server's leveling profiles, messages, XP, and voice time.",
    "levelsetup": "Open the leveling setup wizard, or enable or disable leveling.",
    "voicesetup": "Open the voice XP setup panel and choose whether voice activity earns XP.",
    "streak": "View a member's current and best leveling activity streak.",
    "voicexp": "View or configure voice XP, its earning rate, or open `voicexp setup`.",
    "xpblacklist": "View or configure channels, roles, and users excluded from XP.",
    "xpconfig": "View or configure leveling settings.",
    "xpcooldown": "View or set the delay between XP awards.",
    "xprange": "View or set the minimum and maximum XP awarded per message.",
}

LEVELING_ACTION_HELP = {
    "levelsetup on": ("levelsetup on", "Enable XP after the leveling setup has been applied."),
    "levelsetup off": ("levelsetup off", "Disable message and voice XP tracking."),
    "xpconfig enable": ("xpconfig enable", "Enable leveling after setup is complete."),
    "xpconfig disable": ("xpconfig disable", "Disable leveling and voice tracking."),
    "xpconfig channel": ("xpconfig channel [#channel]", "Set or view the level-up announcement channel."),
    "xpconfig message": ("xpconfig message <text>", "Set the level-up announcement text."),
    "xprange set": ("xprange <minimum> <maximum>", "Set the XP range awarded per message."),
    "xpcooldown set": ("xpcooldown <seconds>", "Set the XP cooldown between messages."),
    "xpblacklist add": ("xpblacklist add <channel|role|user> <mention or ID>", "Exclude a channel, role, or user from XP."),
    "xpblacklist remove": ("xpblacklist remove <channel|role|user> <mention or ID>", "Remove an item from the XP blacklist."),
    "xpblacklist clear": ("xpblacklist clear", "Clear all XP blacklist entries."),
    "multiplier set": ("multiplier set <channel|role> <mention or ID> <value>", "Set an XP multiplier from 0.1x to 10x."),
    "multiplier remove": ("multiplier remove <channel|role> <mention or ID>", "Remove an XP multiplier."),
    "rewards add": ("rewards add <level> @role [@role ...]", "Add one or more reward roles for a level."),
    "rewards remove": ("rewards remove <level> @role [@role ...]", "Remove reward roles for a level."),
    "voicexp setup": ("voicexp setup", "Open the voice XP configuration panel."),
    "voicexp on": ("voicexp on", "Enable voice XP after leveling is set up and enabled."),
    "voicexp off": ("voicexp off", "Disable voice XP awards."),
    "voicexp rate": ("voicexp rate <1-100>", "Set XP earned per minute in voice."),
}


# Categories mirror the requested reference UI. Keep command names grouped by
# their owning module; the live command tree supplies slash command names too.
CATEGORIES = [
    ("antinuke", "Antinuke", "security"),
    ("automod", "AutoMod", "automod"),
    ("automations", "Automations", "automations"),
    ("autoresponder", "Autoresponder", "autoresponder"),
    ("customrole", "CustomRole", "customrole"),
    ("fun", "Fun", "fun"),
    ("general", "General", "general"),
    ("giveaway", "Giveaway", "giveaway"),
    ("leaderboard", "Leaderboard", "leaderboard"),
    ("leveling", "Leveling", "Leveling"),
    ("logging", "Logging", "logging"),
    ("moderation", "Moderation", "moderation"),
    ("music", "Music", "music"),
    ("permit", "Permit", "permit"),
    ("reactionroles", "ReactionRoles", "reactionroles"),
    ("ticket", "Ticket", "ticket"),
    ("utility", "Utility", "utility"),
    ("vanityroles", "Vanityroles", "vanityroles"),
    ("voice", "Voice", "voice"),
    ("voicemaster", "VoiceMaster", "voicemaster"),
    ("welcomer", "Welcomer", "welcome"),
]
CATEGORY_MAP = {key: (label, emoji_key) for key, label, emoji_key in CATEGORIES}
CATEGORY_MAP["security"] = CATEGORY_MAP["antinuke"]
CATEGORY_MAP["whitelist"] = CATEGORY_MAP["antinuke"]
CATEGORY_MAP["wl"] = CATEGORY_MAP["antinuke"]
CATEGORY_COMMANDS = {
    "antinuke": ("security", "antinuke", "antinukesetup", "antinukeconfig", "antinukelimit", "antinukepunishment", "antinukereset", "mainrole", "whitelist", "unwhitelist", "whitelisted", "whitelistrole", "setlog"),
    "automod": ("automod",),
    "fun": (
        "fun", "howgay", "lesbian", "cute", "intelligence", "chutya", "horny", "tharki",
        "gif", "weather", "fakeban", "image", "8ball", "truth", "dare",
        "bite", "blush", "bonk", "cry", "cuddle", "dance", "feed", "handhold", "happy",
        "highfive", "hug", "kill", "kiss", "lappillow", "pat", "poke", "punch", "slap",
        "smash", "smug", "spank", "stare", "tickle", "wave", "wink", "yeet",
    ),
    "autoresponder": ("autoresponder", "autoresponder create", "autoresponder delete", "autoresponder edit", "autoresponder config", "autoreact", "autoreact add", "autoreact remove", "autoreact list", "autoreact reset"),
    "customrole": ("setup", "setup guest", "setup staff", "setup delete", "setup friend", "setup reqrole", "setup list", "setup config", "setup girl", "setup create", "setup vip", "setup reset", "staff", "girl", "friend", "vip", "guest", "setautorole"),
    "reactionroles": ("reactionrole", "reactionrole add", "reactionrole remove", "reactionrole list", "reactionrole reset"),
    "ticket": ("ticket",),
    "music": ("play", "search", "pause", "resume", "skip", "previous", "stop", "join", "leave", "queue", "nowplaying", "volume", "loop", "shuffle", "autoplay"),
    "general": (
        "help", "commands", "prefix", "status", "afk", "avatar", "banner", "servericon", "membercount", "poll", "urban", "users",
        "list", "list boosters", "list inrole", "list emojis", "list bots", "list admins", "list invoice", "list early", "list roles",
        "config",
    ),
    "moderation": (
        "ban", "unban", "kick", "mute", "unmute", "timeout", "untimeout", "purge", "pb", "slowmode",
        "lock", "unlock", "hide", "unhide", "nuke", "nick", "nickname", "role", "transfer", "recover", "warn", "warnings", "unwarn", "clearwarnings",
    ),
    "utility": (
        "botinfo", "stats", "invite", "serverinfo", "si", "userinfo", "roleinfo", "roleicon", "boostcount", "joined-at", "embed",
        "ping", "github", "vcinfo", "channelinfo", "badges", "reminder", "permissions",
        "timer", "prime", "setupi", "makeqr", "showqr", "membercount", "users", "media",
        "media setup", "media remove", "media config", "media bypass", "media bypass add",
        "media bypass remove", "media bypass show",
    ),
    "voice": (
        "voice", "vcmod", "vcban", "vckick", "vcunmute", "vcpull", "vcrole", "vcrole add", "vcrole remove", "vcrole config", "voiceclaim", "voiceowner", "voicename", "voicelimit",
        "voicelock", "voiceunlock", "voicekick", "voicepermit", "voicedeny",
    ),
    "voicemaster": ("voicemaster",),
    "welcomer": ("greet", "greet setup", "setwelcome"),
    "leaderboard": (
        "voicelb on", "voicelb off", "vcstats", "voiceleaderboard", "dailyvoice", "weeklyvoice",
        "addvctime", "reducevctime", "clearvoice", "resetmyvoice",
        "msglb on", "msglb off", "msgstats", "messageboard", "dailymessages", "weeklymessages", "leaderboard messages", "leaderboard dailymessages",
        "leaderboard weeklymessages", "addmessages", "removemessages", "clearmsgs", "resetmymessages",
        "messagechannels add", "messagechannels remove", "messagechannels show", "messagechannels reset",
        "blacklistchannel", "unblacklistchannel", "blacklistedchannels", "blacklistcategory",
        "unblacklistcategory", "blacklistedcategories", "setmessagerole", "unsetmessagerole", "viewmessageroles",
        "invitelb on", "invitelb off", "invites", "inviter", "invited", "inviteinfo",
        "inviteleaderboard total", "inviteleaderboard regular", "inviteleaderboard fake", "inviteleaderboard left",
        "addinvites", "removeinvites", "clearinvites", "resetmyinvites", "setallthreshold",
        "unsetallthreshold", "setinviterole", "unsetinviterole", "viewinviteroles",
        "top messages", "top voice", "top invites",
    ),
    "leveling": ("rank", "profile", "levelboard", "givexp", "setlevel", "resetxp", "claimreward", "multiplier", "rewards", "serverstats", "levelsetup", "streak", "voicesetup", "voicexp", "xpblacklist", "xpconfig", "xpcooldown", "xprange", *LEVELING_ACTION_HELP),
}

# Commands are rendered as plain names to match the reference help panel.
# Slash command names are included in their module, without a leading slash.
COMMAND_CATEGORY = {
    "fun": "fun", "howgay": "fun", "lesbian": "fun", "cute": "fun", "intelligence": "fun",
    "chutya": "fun", "horny": "fun", "tharki": "fun", "gif": "fun", "weather": "fun",
    "fakeban": "fun", "image": "fun", "8ball": "fun", "8b": "fun", "truth": "fun", "dare": "fun",
    "bite": "fun", "blush": "fun", "bonk": "fun", "cry": "fun",
    "cuddle": "fun", "dance": "fun", "feed": "fun", "handhold": "fun", "happy": "fun",
    "highfive": "fun", "hug": "fun", "kill": "fun", "kiss": "fun", "lappillow": "fun",
    "pat": "fun", "poke": "fun", "punch": "fun", "slap": "fun", "smash": "fun", "smug": "fun",
    "spank": "fun", "stare": "fun", "tickle": "fun", "wave": "fun", "wink": "fun", "yeet": "fun",
    "antibot": "automod",
    "antinuke": "antinuke", "security": "antinuke", "whitelist": "antinuke", "wl": "antinuke",
    "unwhitelist": "antinuke", "unwl": "antinuke", "whitelisted": "antinuke",
    "antinukeconfig": "antinuke", "antinuke_punishment": "antinuke",
    "antinukelimit": "antinuke", "antinukepunishment": "antinuke", "antinukereset": "antinuke",
    "mainrole": "antinuke", "whitelistrole": "antinuke", "setlog": "antinuke",
    "automod": "automod",
    "autoresponder": "autoresponder",
    "autoreact": "autoresponder",
    # Attribute every current and future child of the autorole group to the
    # Automations help category by its root command name.
    "autorole": "automations",
    "setup": "customrole",
    "permit": "permit",
    "vanityroles": "vanityroles",
    "tagroles": "vanityroles",
    "reactionrole": "reactionroles", "reactionroles": "reactionroles",
    "ticket": "ticket",
    "tickethelp": "ticket", "setup2": "ticket", "panel2": "ticket",
    "setform": "ticket", "close": "ticket", "add": "ticket", "remove": "ticket",
    "transcript": "ticket", "ticketinfo": "ticket",
    "play": "music", "pause": "music", "resume": "music", "skip": "music", "previous": "music",
    "stop": "music", "join": "music", "leave": "music", "queue": "music", "nowplaying": "music",
    "volume": "music", "loop": "music", "shuffle": "music", "autoplay": "music", "search": "music",
    "leaderboard": "leaderboard", "top": "leaderboard", "invitelb": "leaderboard",
    "rank": "leveling", "level": "leveling", "xp": "leveling", "profile": "leveling", "pr": "leveling", "levelboard": "leveling", "levelingboard": "leveling", "givexp": "leveling", "setlevel": "leveling", "resetxp": "leveling", "claimreward": "leveling", "claim": "leveling", "multiplier": "leveling", "mult": "leveling", "rewards": "leveling", "rl": "leveling", "serverstats": "leveling", "ss": "leveling", "levelsetup": "leveling", "levelingsetup": "leveling", "streak": "leveling", "voicesetup": "leveling", "voicelevelsetup": "leveling", "voicexp": "leveling", "xpblacklist": "leveling", "xpconfig": "leveling", "xpcooldown": "leveling", "xprange": "leveling",
    "inviteleaderboard": "leaderboard", "msglb": "leaderboard", "voicelb": "leaderboard",
    "voiceleaderboard": "leaderboard", "dailyvoice": "leaderboard", "weeklyvoice": "leaderboard",
    "vcstats": "leaderboard", "addvctime": "leaderboard", "reducevctime": "leaderboard",
    "clearvoice": "leaderboard", "resetmyvoice": "leaderboard", "msgstats": "leaderboard",
    "messageboard": "leaderboard", "dailymessages": "leaderboard", "weeklymessages": "leaderboard",
    "addmessages": "leaderboard", "removemessages": "leaderboard", "clearmessages": "leaderboard",
    "clearmsgs": "leaderboard", "resetmymessages": "leaderboard", "blacklistchannel": "leaderboard",
    "unblacklistchannel": "leaderboard", "blacklistedchannels": "leaderboard", "messagechannels": "leaderboard", "blacklistcategory": "leaderboard",
    "unblacklistcategory": "leaderboard", "blacklistedcategories": "leaderboard", "setmessagerole": "leaderboard",
    "unsetmessagerole": "leaderboard", "viewmessageroles": "leaderboard", "invites": "leaderboard",
    "inviter": "leaderboard", "invited": "leaderboard", "inviteinfo": "leaderboard", "addinvites": "leaderboard",
    "removeinvites": "leaderboard", "clearinvites": "leaderboard", "resetmyinvites": "leaderboard",
    "setallthreshold": "leaderboard", "unsetallthreshold": "leaderboard", "setinviterole": "leaderboard",
    "unsetinviterole": "leaderboard", "viewinviteroles": "leaderboard",
    "logging": "logging",
    "gstart": "giveaway", "gconfig": "giveaway", "gwystaff": "giveaway", "gembed": "giveaway", "glist": "giveaway",
    "setautorole": "customrole",
    "staff": "customrole", "girl": "customrole", "friend": "customrole", "vip": "customrole", "guest": "customrole",
    "help": "general", "commands": "general", "prefix": "general", "config": "general", "setup": "general",
    "botinfo": "utility", "stats": "utility", "serverinfo": "utility", "si": "utility", "userinfo": "utility", "embed": "utility",
    "avatar": "general", "banner": "general", "roleinfo": "utility", "roleicon": "utility",
    "invite": "utility", "joined-at": "utility", "joinedat": "utility", "github": "utility",
    "vcinfo": "utility", "channelinfo": "utility", "ci": "utility", "badges": "utility",
    "reminder": "utility", "remind": "utility", "permissions": "utility", "perms": "utility",
    "timer": "utility", "prime": "utility", "setupi": "utility", "makeqr": "utility", "showqr": "utility", "showupi": "utility",
    "media": "utility",
    "ban": "moderation", "unban": "moderation", "unbanall": "moderation", "kick": "moderation",
    "timeout": "moderation", "untimeout": "moderation", "mute": "moderation", "unmute": "moderation", "purge": "moderation", "pb": "moderation",
    "unmuteall": "moderation", "slowmode": "moderation", "unslowmode": "moderation", "lock": "moderation", "lockall": "moderation", "unlock": "moderation", "unlockall": "moderation", "hide": "moderation", "hideall": "moderation", "unhide": "moderation", "unhideall": "moderation", "nuke": "moderation",
    "nick": "moderation", "nickname": "moderation", "role": "moderation", "transfer": "moderation", "recover": "moderation", "warn": "moderation",
    "warnings": "moderation", "unwarn": "moderation", "clearwarnings": "moderation", "clearwarns": "moderation",
    "removerole": "moderation", "rrole": "moderation", "clone": "moderation", "snipe": "moderation", "enlarge": "moderation", "steal": "moderation",
    "jail": "moderation", "unjail": "moderation",
    "users": "utility", "ping": "utility",
    "afk": "general", "away": "general", "status": "general", "servericon": "general", "guildicon": "general",
    "poll": "general", "urban": "general", "list": "general",
    "membercount": "utility", "mc": "utility",
    "boostcount": "utility", "bc": "utility",
    "voice": "voice", "vcmod": "voice", "vcban": "voice", "vckick": "voice", "vcunmute": "voice", "vcpull": "voice",
    "vcrole": "voice",
    "voicemaster": "voicemaster", "voiceclaim": "voice", "voiceowner": "voice",
    "voicename": "voice", "voicelimit": "voice", "voicelock": "voice",
    "voiceunlock": "voice", "voicekick": "voice", "voicepermit": "voice",
    "voicedeny": "voice", "setwelcome": "welcomer", "greet": "welcomer",
}


# Permit module commands as shown in the help menu (name, description).
PERMIT_HELP_ENTRIES = (
    ("permit @role", "Add or remove command permissions for a role. This does not give the role Discord permissions; it only unlocks the matching bot commands."),
    ("permit reset @role", "Reset the permissions of a role."),
    ("permit reset all", "Reset the permissions of all roles."),
    ("permit owner @role", "Set a role as owner role (owner role is immune from commands)."),
)
PERMIT_HELP_NAMES = tuple(name for name, _ in PERMIT_HELP_ENTRIES)


def commands_for_category(bot, key: str) -> tuple[str, ...]:
    """Return live prefix and app command names for a help category."""
    names = list(CATEGORY_COMMANDS.get(key, ()))
    for command in bot.tree.walk_commands():
        if COMMAND_CATEGORY.get(command.name) == key and command.name not in names:
            names.append(command.name)
    return tuple(names)


def live_category_command_names(bot, key: str) -> tuple[str, ...]:
    """List only commands that are actually registered, without slash markers."""
    names = []
    seen = set()

    def add(name):
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    for command in bot.walk_commands():
        if command.hidden or command.name == "help":
            continue
        root = command.root_parent or command
        if COMMAND_CATEGORY.get(root.name) != key:
            continue
        add(command.qualified_name)
    if key == "moderation":
        for name in ("jail add", "jail remove", "jail setup", "jail list", "jail config", "jail reset"):
            add(name)
    if key == "leveling":
        for name in LEVELING_ACTION_HELP:
            add(name)
    if key == "voice" and bot.get_command("vcmod"):
        add("vcmod")
    if key == "utility" and bot.get_command("serverinfo"):
        add("si")
    for command in bot.tree.walk_commands():
        root_name = command.qualified_name.split(maxsplit=1)[0]
        if command.name == "help" or COMMAND_CATEGORY.get(root_name) != key:
            continue
        add(command.qualified_name)
    if key == "utility":
        # Match the reference ordering while showing only registered commands.
        available = set(names)
        for command in bot.tree.walk_commands():
            available.add(command.name)
        ordered = (
            "afk", "botinfo", "stats", "invite", "serverinfo", "si", "userinfo", "roleinfo", "roleicon", "boostcount", "joined-at",
            "ping", "github", "vcinfo", "channelinfo", "embed", "badges", "reminder", "permissions",
            "timer", "prime", "setupi", "makeqr", "showqr", "membercount", "users", "media",
            "media setup", "media remove", "media config", "media bypass", "media bypass add",
            "media bypass remove", "media bypass show",
        )
        names = [name for name in ordered if name in available]
    if key == "general":
        available = set(names)
        ordered = (
            "status", "afk", "avatar", "banner", "servericon", "membercount", "poll", "urban", "users", "prefix",
            "list boosters", "list inrole", "list emojis", "list bots", "list admins", "list invoice", "list early", "list roles",
            "help", "commands", "config",
        )
        names = [name for name in ordered if name in available]
    if key == "voice":
        available = set(names)
        ordered = (
            "voice", "vcmod", "vcban", "vckick", "vcunmute", "vcpull", "voice kick", "voice kickall", "voice mute", "voice muteall",
            "voice unmute", "voice unmuteall", "voice deafen", "voice deafenall", "voice undeafen",
            "voice undeafenall", "voice move", "voice moveall", "voice pull", "voice pullall",
            "voice lock", "voice unlock", "voice private", "voice unprivate", "voiceclaim", "voiceowner",
            "voicename", "voicelimit", "voicepermit", "voicedeny",
        )
        names = [name for name in ordered if name in available]
        vcrole_order = ("vcrole add", "vcrole config", "vcrole remove")
        names.extend(name for name in vcrole_order if name in available)
    if key == "vanityroles":
        available = set(names)
        vanity_order = ("vanityroles", "vanityroles setup", "vanityroles config", "vanityroles reset")
        tagrole_order = ("tagroles", "tagroles setup", "tagroles config", "tagroles reset")
        names = [name for name in (*vanity_order, *tagrole_order) if name in available]
    if key == "antinuke":
        # The whitelist command takes an action argument instead of Discord.py
        # subcommands; expose its supported forms in the help catalogue anyway.
        for name in ("whitelist add", "whitelist remove", "whitelist all", "whitelist show", "whitelist reset"):
            add(name)
    return tuple(names)


def registered_command_count(bot) -> int:
    """Count currently registered command entries, excluding aliases and hidden help."""
    prefix_names = {
        command.qualified_name
        for command in bot.walk_commands()
        if not command.hidden and command.name != "help"
    }
    slash_names = {
        command.qualified_name
        for command in bot.tree.walk_commands()
        if command.name != "help"
    }
    return len(prefix_names) + len(slash_names)


def category_list_embed(bot, key: str, requester_name: str = "Member"):
    if key in {"security", "whitelist", "wl"}:
        key = "antinuke"
    label, emoji_key = CATEGORY_MAP[key]
    names = live_category_command_names(bot, key)
    if key == "antinuke":
        # Present the grouped command names in the same spaced format people
        # use when typing them, even where the registered prefix name is compact.
        antinuke_names = (
            "security", "antinuke", "antinuke limit", "antinuke punishment", "antinuke config",
            "antinuke reset", "mainrole", "whitelist add", "whitelist remove", "whitelist show", "whitelist reset",
        )
        antinuke_text = ", ".join(f"**`{name}`**" for name in antinuke_names) or "Commands are not available yet."
        description = f"**{icon(emoji_key)} {label}**\n\n**__Antinuke__**\n{antinuke_text}\n** **"
        embed = panel("", description)
        if bot.user:
            embed.set_thumbnail(url=bot.user.display_avatar.url)
    else:
        if key == "leveling":
            voice_xp_names = {"voicesetup", "voicexp", "voicexp setup", "voicexp on", "voicexp off", "voicexp rate"}
            leveling_names = [name for name in names if name not in voice_xp_names]
            leveling_text = ", ".join(f"**`{name}`**" for name in leveling_names) or "Commands are not available yet."
            voice_xp_text = ", ".join(f"**`{name}`**" for name in names if name in voice_xp_names) or "Commands are not available yet."
            description = (
                f"**{icon(emoji_key)} {label}**\n\n"
                f"**__Leveling__**\n{leveling_text}\n\n"
                f"**__Voice XP__**\n{voice_xp_text}"
            )
        elif key == "automod":
            automod_names = [name for name in names if name == "automod" or name.startswith("automod ")]
            antibot_names = [name for name in names if name == "antibot" or name.startswith("antibot ")]
            automod_text = ", ".join(f"**`{name}`**" for name in automod_names) or "Commands are not available yet."
            antibot_text = ", ".join(f"**`{name}`**" for name in antibot_names) or "Commands are not available yet."
            description = f"**{icon(emoji_key)} {label}**\n\n**__AutoMod__**\n{automod_text}\n\n**__Antibot__**\n{antibot_text}"
        elif key == "fun":
            roleplay = tuple(name for name in CATEGORY_COMMANDS["fun"] if name in names and name not in {
                "fun", "howgay", "lesbian", "cute", "intelligence", "chutya", "horny", "tharki",
                "gif", "weather", "fakeban", "image", "8ball", "truth", "dare",
            })
            casual = tuple(name for name in CATEGORY_COMMANDS["fun"] if name in names and name not in roleplay)
            casual_text = ", ".join(f"**`{name}`**" for name in casual) or "Commands are not available yet."
            roleplay_text = ", ".join(f"**`{name}`**" for name in roleplay) or "Commands are not available yet."
            description = f"**{icon(emoji_key)} {label}**\n\n**__Fun__**\n{casual_text}\n\n**__Roleplay__**\n{roleplay_text}"
        elif key == "autoresponder":
            responder_names = [name for name in names if name == "autoresponder" or name.startswith("autoresponder ")]
            react_names = ("react", "react add", "react remove", "react list", "react reset") if bot.get_command("react") else ()
            responder_text = ", ".join(f"**`{name}`**" for name in responder_names) or "Commands are not available yet."
            react_text = ", ".join(f"**`{name}`**" for name in react_names) or "Commands are not available yet."
            description = (
                f"**{icon(emoji_key)} {label}**\n\n"
                f"**__Autoresponder__**\n{responder_text}\n\n"
                f"**__Autoreact__**\n{react_text}"
            )
        elif key == "reactionroles":
            reactionrole_names = ("reactionrole add", "reactionrole remove", "reactionrole list", "reactionrole reset")
            command_text = ", ".join(f"**`{name}`**" for name in reactionrole_names)
            description = f"**{icon(emoji_key)} {label}**\n\n**__ReactionRoles__**\n{command_text}"
        elif key == "customrole":
            customrole_names = (
                "setup", "setup create", "setup delete", "setup list", "setup staff", "setup girl",
                "setup friend", "setup vip", "setup guest", "setup reqrole", "setup config", "setup reset",
                "staff", "girl", "friend", "vip", "guest",
            )
            command_text = ", ".join(f"**`{name}`**" for name in customrole_names)
            description = f"**{icon(emoji_key)} {label}**\n\n**__CustomRole__**\n{command_text}"
        elif key == "moderation":
            jail_names = {"jail", "unjail", "jail add", "jail remove", "jail setup", "jail list", "jail config", "jail reset"}
            moderation_text = ", ".join(f"**`{name}`**" for name in names if name not in jail_names)
            jail_text = ", ".join(f"**`{name}`**" for name in ("jail add", "jail remove", "jail setup", "jail list", "jail config", "jail reset"))
            description = f"**{icon(emoji_key)} {label}**\n\n**__Moderation__**\n{moderation_text or 'Commands are not available yet.'}\n\n**__Jail__**\n{jail_text}"
        elif key == "utility":
            utility_names = [name for name in names if not name.startswith("media")]
            media_names = [name for name in names if name.startswith("media")]
            utility_text = ", ".join(f"**`{name}`**" for name in utility_names) or "Commands are not available yet."
            media_text = ", ".join(f"**`{name}`**" for name in media_names) or "Commands are not available yet."
            description = f"**{icon(emoji_key)} {label}**\n\n**__Utility__**\n{utility_text}\n\n**__Media__**\n{media_text}"
        elif key == "permit":
            permit_text = ", ".join(f"**`{name}`**" for name in PERMIT_HELP_NAMES) if bot.get_command("permit") else "Commands are not available yet."
            description = f"**{icon(emoji_key)} {label}**\n\n**__Permit__**\n{permit_text}"
        elif key == "leaderboard":
            sections = (
                ("Voice Leaderboard", ("voicelb on", "voicelb off", "vcstats", "voiceleaderboard", "dailyvoice", "weeklyvoice", "addvctime", "reducevctime", "clearvoice", "resetmyvoice")),
                ("Message Leaderboard", ("msglb on", "msglb off", "msgstats", "m", "messageboard", "dailymessages", "weeklymessages", "leaderboard messages", "leaderboard dailymessages", "leaderboard weeklymessages", "addmessages", "removemessages", "clearmsgs", "resetmymessages", "messagechannels add", "messagechannels remove", "messagechannels show", "messagechannels reset", "blacklistchannel", "unblacklistchannel", "blacklistedchannels", "blacklistcategory", "unblacklistcategory", "blacklistedcategories", "setmessagerole", "unsetmessagerole", "viewmessageroles")),
                ("Invite Leaderboard", ("invitelb on", "invitelb off", "invites", "inviter", "invited", "inviteinfo", "inviteleaderboard total", "inviteleaderboard regular", "inviteleaderboard fake", "inviteleaderboard left", "addinvites", "removeinvites", "clearinvites", "resetmyinvites", "setallthreshold", "unsetallthreshold", "setinviterole", "unsetinviterole", "viewinviteroles")),
                ("Top Leaderboard", ("top messages", "top voice", "top invites")),
            )
            sections_text = []
            for heading, commands_list in sections:
                pills = ", ".join(f"**`{command}`**" for command in commands_list)
                sections_text.append(f"**__{heading}__**\n{pills}")
            shortcuts = "**__Shortcuts__**\n`m [@member]` = message stats; `lb m` = message leaderboard; `i` = invites; `v` = voice; `dm` / `wm` = daily / weekly messages; `dv` / `wv` = daily / weekly voice."
            description = f"**{icon(emoji_key)} {label}**\n\n{shortcuts}\n\n" + "\n\n".join(sections_text)
        elif key == "voice":
            voice_names = [name for name in names if not name.startswith("vcrole")]
            vcrole_names = [name for name in names if name.startswith("vcrole")]
            voice_text = ", ".join(f"**`{name}`**" for name in voice_names) or "Commands are not available yet."
            vcrole_text = ", ".join(f"**`{name}`**" for name in vcrole_names) or "Commands are not available yet."
            description = (
                f"**{icon(emoji_key)} {label}**\n\n"
                f"**__Voice__**\n{voice_text}\n\n"
                f"**__VC Roles__**\n{vcrole_text}"
            )
        elif key == "vanityroles":
            vanity_names = [name for name in names if name == "vanityroles" or name.startswith("vanityroles ")]
            tagrole_names = [name for name in names if name == "tagroles" or name.startswith("tagroles ")]
            vanity_text = ", ".join(f"**`{name}`**" for name in vanity_names) or "Commands are not available in this module yet."
            tagrole_text = ", ".join(f"**`{name}`**" for name in tagrole_names) or "Commands are not available in this module yet."
            description = f"**{icon(emoji_key)} {label}**\n\n**__Vanityroles__**\n{vanity_text}\n\n**__Tag Roles__**\n{tagrole_text}"
        else:
            command_text = ", ".join(f"**`{name}`**" for name in names)
            if not command_text:
                command_text = "Commands are not available in this module yet."
            description = f"**{icon(emoji_key)} {label}**\n\n**__{label}__**\n{command_text}"
        embed = panel("", description)
        if bot.user:
            embed.set_thumbnail(url=bot.user.display_avatar.url)
    page = next((index + 1 for index, (category_key, _, _) in enumerate(CATEGORIES) if category_key == key), 1)
    embed.set_footer(text=f"Requested by {requester_name} | Page {page} / {len(CATEGORIES)}")
    return embed


class ModulePagesView(discord.ui.View):
    def __init__(self, bot, *, requester_id: int, requester_name: str, page: int):
        super().__init__(timeout=240)
        self.bot, self.requester_id, self.requester_name = bot, requester_id, requester_name
        self.page = max(0, min(page, len(CATEGORIES) - 1))
        for key, callback in (("first", self.first_page), ("previous", self.previous_page), ("delete", self.close), ("next", self.next_page), ("last", self.last_page)):
            button = discord.ui.Button(emoji=discord.PartialEmoji.from_str(icon(HELP_NAV_EMOJI_NAMES[key])), style=discord.ButtonStyle.secondary, row=0)
            button.callback = callback
            button.disabled = (self.page == 0 if key in {"first", "previous"} else self.page == len(CATEGORIES) - 1 if key in {"next", "last"} else False)
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Only the person who opened this help panel can use these buttons.", ephemeral=True)
        return False

    async def show_page(self, interaction: discord.Interaction, page: int):
        # Acknowledge the button immediately. Building the module card and
        # editing the CV2 message can otherwise exceed Discord's 3-second
        # interaction response window, especially during gateway reconnects.
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except discord.NotFound:
            return
        page = max(0, min(page, len(CATEGORIES) - 1))
        key = CATEGORIES[page][0]
        view = ModulePagesView(self.bot, requester_id=self.requester_id, requester_name=self.requester_name, page=page)
        try:
            await edit_component_card(interaction, category_list_embed(self.bot, key, self.requester_name), view)
        except discord.NotFound:
            return

    async def first_page(self, interaction: discord.Interaction):
        await self.show_page(interaction, 0)

    async def previous_page(self, interaction: discord.Interaction):
        await self.show_page(interaction, self.page - 1)

    async def next_page(self, interaction: discord.Interaction):
        await self.show_page(interaction, self.page + 1)

    async def last_page(self, interaction: discord.Interaction):
        await self.show_page(interaction, len(CATEGORIES) - 1)

    async def close(self, interaction: discord.Interaction):
        await delete_help_message(interaction)


def _all_help_commands(bot):
    """Index prefix and application commands by their visible name."""
    found = {}
    for command in bot.walk_commands():
        if command.hidden or command.name == "help":
            continue
        found.setdefault(command.qualified_name.lower(), (command, None))
        found.setdefault(command.name.lower(), (command, None))
        for alias in command.aliases:
            found.setdefault(alias.lower(), (command, None))
    for command in bot.tree.walk_commands():
        if command.name == "help":
            continue
        found.setdefault(command.name.lower(), (None, command))
    return found


def _command_details(bot, name: str):
    prefix_command, app_command = _all_help_commands(bot).get(name.lower(), (None, None))
    if prefix_command is None and app_command is None:
        return None
    command_name = prefix_command.qualified_name if prefix_command else app_command.qualified_name
    signature = prefix_command.signature if prefix_command else " ".join(
        f"<{parameter.name}>" for parameter in app_command.parameters
    )
    description = ""
    if prefix_command:
        description = prefix_command.help or prefix_command.brief or ""
    if not description and app_command:
        description = app_command.description
    if not description:
        description = GROUP_COMMAND_DESCRIPTIONS.get(command_name.lower(), "")
    if not description:
        description = "Command details are not available yet."
    return command_name, signature, description


class HelpPagesView(discord.ui.View):
    def __init__(self, *, requester_id: int, title: str, entries: list[tuple[str, str]], page: int = 0, page_size: int = HELP_PAGE_SIZE, guild_id: int | None = None, prefix: str = settings.prefix):
        super().__init__(timeout=240)
        self.requester_id = requester_id
        self.title = title
        self.entries = entries
        self.page_size = page_size
        self.page = page
        self.guild_id = guild_id
        self.prefix = prefix
        self.page_count = max(1, (len(entries) + page_size - 1) // page_size)
        self.page = min(max(page, 0), self.page_count - 1)
        self._set_buttons()

    def _set_buttons(self):
        for key, callback in (("first", self.first_page), ("previous", self.previous_page), ("delete", self.close), ("next", self.next_page), ("last", self.last_page)):
            emoji_markup = icon(HELP_NAV_EMOJI_NAMES[key])
            button = discord.ui.Button(emoji=discord.PartialEmoji.from_str(emoji_markup), style=discord.ButtonStyle.secondary, row=0)
            button.callback = callback
            button.disabled = (self.page == 0 if key in ("first", "previous") else self.page >= self.page_count - 1 if key in ("next", "last") else False)
            self.add_item(button)

    def make_embed(self):
        start = self.page * self.page_size
        body = []
        for command, description in self.entries[start:start + self.page_size]:
            safe_description = description.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
            body.append(f"> `{command}`\n> {icon('arrow')} {safe_description}")
        embed = panel(self.title.upper(), "\n\n".join(body))
        embed.set_footer(text=f"Requested by {self._requester_name} | Page {self.page + 1}/{self.page_count}")
        return embed

    def set_requester(self, name: str):
        self._requester_name = name
        return self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Only the person who opened this help panel can use these buttons.", ephemeral=True)
        return False

    async def _show(self, interaction: discord.Interaction, page: int):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except discord.NotFound:
            return
        if self.guild_id is not None:
            active_prefix = await current_guild_prefix(interaction.client, self.guild_id)
            if active_prefix != self.prefix:
                self.entries = [
                    (active_prefix + usage[len(self.prefix):] if usage.startswith(self.prefix) else usage, description)
                    for usage, description in self.entries
                ]
                self.prefix = active_prefix
        updated = HelpPagesView(
            requester_id=self.requester_id,
            title=self.title,
            entries=self.entries,
            page=page,
            page_size=self.page_size,
            guild_id=self.guild_id,
            prefix=self.prefix,
        ).set_requester(self._requester_name)
        try:
            await edit_component_card(interaction, updated.make_embed(), updated)
        except discord.NotFound:
            return

    async def first_page(self, interaction: discord.Interaction):
        await self._show(interaction, 0)

    async def previous_page(self, interaction: discord.Interaction):
        await self._show(interaction, self.page - 1)

    async def next_page(self, interaction: discord.Interaction):
        await self._show(interaction, self.page + 1)

    async def last_page(self, interaction: discord.Interaction):
        await self._show(interaction, self.page_count - 1)

    async def close(self, interaction: discord.Interaction):
        await delete_help_message(interaction)


def _group_entries(bot, key: str, prefix: str = settings.prefix):
    entries = []
    for name in commands_for_category(bot, key):
        if key == "leveling" and name in LEVELING_ACTION_HELP:
            usage, description = LEVELING_ACTION_HELP[name]
            entries.append((f"{prefix}{usage}", description))
            continue
        prefix_command = bot.get_command(name)
        if isinstance(prefix_command, commands.Group) and prefix_command.commands:
            entries.extend(_prefix_group_entries(prefix_command, prefix))
            continue
        details = _command_details(bot, name)
        if details:
            command_name, signature, description = details
            entries.append((f"{prefix}{command_name} {signature}".rstrip(), description))
        else:
            entries.append((f"{prefix}{name}", "Command details are not available yet."))
    return entries


def _prefix_group_entries(command, prefix: str = settings.prefix):
    entries = []

    def add_children(group):
        children = list(group.commands)
        if group.qualified_name.casefold() == "vcrole":
            order = {name: index for index, name in enumerate(("add", "config", "remove"))}
            children.sort(key=lambda child: order.get(child.name.casefold(), len(order)))
        elif group.qualified_name.casefold() == "ticket":
            order = {name: index for index, name in enumerate(("setup", "setup2", "panel2", "setform", "close", "add", "remove", "transcript", "ticketinfo", "help"))}
            children.sort(key=lambda child: order.get(child.name.casefold(), len(order)))
        elif group.qualified_name.casefold() == "setup":
            order = {name: index for index, name in enumerate(("guest", "staff", "delete", "friend", "reqrole", "list", "config", "girl", "create", "vip", "reset"))}
            children.sort(key=lambda child: order.get(child.name.casefold(), len(order)))
        for child in children:
            if isinstance(child, commands.Group) and child.commands:
                add_children(child)
                continue
            usage = f"{prefix}{child.qualified_name} {child.signature}".rstrip()
            description = child.help or child.brief or GROUP_COMMAND_DESCRIPTIONS.get(
                child.qualified_name, "Run this command to use this group action."
            )
            entries.append((usage, description))

    add_children(command)
    return entries


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=240)
        self.add_item(HelpCategorySelect())


class HelpCategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=label, value=key, emoji=icon(emoji_key))
            for key, label, emoji_key in CATEGORIES
        ]
        super().__init__(placeholder="Select Module From Here", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        # A category view walks the live command tree. Acknowledge the select
        # immediately so building that view cannot consume Discord's 3-second
        # interaction response window.
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound as exc:
            if exc.code == 10062:
                log.info("Help category interaction %s expired before acknowledgement", interaction.id)
                return
            raise
        key = self.values[0]
        page = next(index for index, item in enumerate(CATEGORIES) if item[0] == key)
        view = ModulePagesView(interaction.client, requester_id=interaction.user.id, requester_name=interaction.user.display_name, page=page)
        try:
            await interaction.followup.send(
                view=component_card(category_list_embed(interaction.client, key, interaction.user.display_name), view),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound as exc:
            if exc.code == 10062:
                log.info("Help category interaction %s expired before its panel was sent", interaction.id)
                return
            raise


class Helpdesk(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Browse Zyven help categories")
    async def help(self, interaction: discord.Interaction):
        prefix = await current_guild_prefix(self.bot, interaction.guild_id)
        await send_component_card(interaction, self.home_embed(prefix), HelpView(), ephemeral=True)

    @commands.command(name="help", aliases=("commands", "h"), case_insensitive=True)
    async def prefix_help(self, ctx: commands.Context, *, query: str | None = None):
        prefix = await current_guild_prefix(self.bot, ctx.guild.id if ctx.guild else None)
        if query:
            key = query.strip().lower().lstrip("./")
            compact_key = key.replace(" ", "")
            if compact_key in CATEGORY_MAP:
                key = compact_key
            if key in {"whitelist", "wl"}:
                entries = [
                    (f"{prefix}whitelist remove <member>", "Unwhitelist a user from antinuke."),
                    (f"{prefix}whitelist reset", "Resets the whitelisted users."),
                    (f"{prefix}whitelist show", "Shows the list of whitelisted users."),
                    (f"{prefix}whitelist add <member>", "Whitelists a user from antinuke for a specific action."),
                ]
                view = HelpPagesView(
                    requester_id=ctx.author.id,
                    title="Zyven Whitelist Group",
                    entries=entries,
                    page_size=5,
                    guild_id=ctx.guild.id if ctx.guild else None,
                    prefix=prefix,
                ).set_requester(ctx.author.display_name)
                await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())
                return
            if key == "leveling":
                entries = _group_entries(self.bot, key, prefix)
                view = HelpPagesView(
                    requester_id=ctx.author.id,
                    title="Zyven Leveling Group",
                    entries=entries,
                    page_size=HELP_PAGE_SIZE,
                    guild_id=ctx.guild.id if ctx.guild else None,
                    prefix=prefix,
                ).set_requester(ctx.author.display_name)
                await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())
                return
            if key == "music":
                entries = _group_entries(self.bot, key, prefix)
                view = HelpPagesView(
                    requester_id=ctx.author.id,
                    title="Zyven Music Group",
                    entries=entries,
                    page_size=5,
                    guild_id=ctx.guild.id if ctx.guild else None,
                    prefix=prefix,
                ).set_requester(ctx.author.display_name)
                await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())
                return
            if key == "permit":
                entries = [(f"{prefix}{name}", description) for name, description in PERMIT_HELP_ENTRIES]
                view = HelpPagesView(
                    requester_id=ctx.author.id,
                    title="Zyven Permit Group",
                    entries=entries,
                    page_size=5,
                    guild_id=ctx.guild.id if ctx.guild else None,
                    prefix=prefix,
                ).set_requester(ctx.author.display_name)
                await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())
                return
            prefix_command = self.bot.get_command(key) or self.bot.get_command(compact_key)
            if ctx.author.id == settings.owner_id and prefix_command and prefix_command.hidden:
                owner_help = self.bot.get_command("ownerhelp")
                if owner_help:
                    await ctx.invoke(owner_help, command_name=prefix_command.qualified_name)
                    return
            if isinstance(prefix_command, commands.Group):
                entries = _prefix_group_entries(prefix_command, prefix)
                # Some groups (such as voice) dispatch their controls as slash
                # commands. Keep `help <group>` focused on that group's commands.
                if not entries:
                    group_key = COMMAND_CATEGORY.get(prefix_command.name)
                    if group_key:
                        entries = _group_entries(self.bot, group_key, prefix)
                        entries = [entry for entry in entries if entry[0].split()[0].strip("`") != f"{prefix}{prefix_command.qualified_name}"]
                if entries:
                    title = f"Zyven {prefix_command.qualified_name} Group"
                    view = HelpPagesView(
                        requester_id=ctx.author.id,
                        title=title,
                        entries=entries,
                        page_size=5 if prefix_command.qualified_name.casefold() in {"purge", "setup", "ticket"} else HELP_PAGE_SIZE,
                        guild_id=ctx.guild.id if ctx.guild else None,
                        prefix=prefix,
                    ).set_requester(ctx.author.display_name)
                    # Group help pages (for example `help voice`) stay visible;
                    # only a single `help <command>` detail is temporary.
                    await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())
                    return
            details = _command_details(self.bot, key) or _command_details(self.bot, compact_key)
            if details:
                command_name, signature, description = details
                view = HelpPagesView(
                    requester_id=ctx.author.id,
                    title="Zyven Command",
                    entries=[(f"{prefix}{command_name} {signature}".rstrip(), description)],
                    guild_id=ctx.guild.id if ctx.guild else None,
                    prefix=prefix,
                ).set_requester(ctx.author.display_name)
                await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())
                return
            # Categories are opened from the main help menu. A name passed as
            # `help <query>` is resolved as a command/group, not a category.
            # Keep unknown help lookups in the same compact usage-card style
            # as invalid command arguments. A category overview is the useful
            # fallback when the requested command does not exist.
            embed = panel("", f"Command usage: `{prefix}help`")
            await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())
            return
        await ctx.send(view=component_card(self.home_embed(prefix), HelpView()), allowed_mentions=discord.AllowedMentions.none())

    def home_embed(self, prefix: str = settings.prefix):
        module_list = "\n".join(
            f"> {icon(emoji_key)} `»`  {label}" for _, label, emoji_key in CATEGORIES
        )
        description = (
            "A powerful multipurpose bot with Fastest Antinuke\n\n"
            f"• **My Prefix is** `{prefix}`\n"
            f"• **Total Commands:** `{registered_command_count(self.bot)}`\n"
            "• **Choose a Specific Module of your Desire**\n"
            f"{module_list}\n\n"
            f"**{icon('links')} Links**\n[Invite Zyven]({settings.invite_url}) | [Support Server]({settings.support_url})"
        )
        embed = panel("Hey, I'm Zyven™", description)
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text="Powered By Zyven Development™")
        return embed


async def setup(bot):
    await bot.add_cog(Helpdesk(bot))
