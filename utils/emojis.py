"""Central emoji registry; optional custom emoji values come from one JSON env var."""

import json
import logging
import os
import re
import base64

import aiohttp
import discord

_NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
_ID_RE = re.compile(r"^\d{15,22}$")
_MARKUP_RE = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d{15,22}>$")
_SHORTCODE_RE = re.compile(r"^:[A-Za-z0-9_+-]+:$")
log = logging.getLogger("zyven.emojis")
APPLICATION_EMOJI_SYNC_VERSION = 2

# Application emoji name -> source emoji ID supplied for the help categories.
APPLICATION_EMOJI_SOURCES = {
    "antinuke": "1461385631098994688",
    "automod": "1461385691106775172",
    "automation": "1461396049175253250",
    "autoresponder": "1461386512527528031",
    "customrole": "1461386631163543808",
    "fun": "1461389095917060338",
    "General": "1461386931454873720",
    "giveaway": "1461405237368590530",
    # Leveling-only uploads from the supplied project. Keep these names scoped
    # so their assets never replace icons used by other bot systems.
    "Leveling": "1562184437020631070",
    "leveling_rank": "1502643594593439755",
    "leveling_xp": "1502643598649200722",
    "leveling_level": "1502643602906415175",
    "leveling_star": "1502643607449108492",
    "leveling_fire": "1502643612125761638",
    "leveling_streak": "1502643616965988463",
    "leveling_voice": "1502643621114155050",
    "leveling_crown": "1502643625006207028",
    "leveling_trophy": "1502643629670400083",
    "leveling_up": "1502643634774736947",
    "leveling_down": "1502643639300391083",
    "leveling_chart": "1502643643737964667",
    "leveling_gift": "1502643648289050644",
    "leveling_sword": "1502643652126576771",
    "leveling_shield": "1502643656618803342",
    "leveling_gem": "1502643660511121428",
    "leveling_bolt": "1502643664487452782",
    "leveling_check": "1502643668908118096",
    "leveling_cross": "1502643673186172968",
    "leveling_warning": "1502643677451780207",
    "leveling_info": "1502643681998540981",
    "leveling_loading": "1502643686163480586",
    "leveling_leaderboard": "1502643690085159032",
    "leveling_help": "1502643694333989024",
    "leveling_settings": "1502643698490544196",
    "leveling_clock": "1502643702932439081",
    "leveling_medal1": "1502643708070461501",
    "leveling_medal2": "1502643712495194132",
    "leveling_medal3": "1502643716622520361",
    "leveling_firenation": "1488896890614448342",
    "leveling_warning2": "1488897327514124359",
    # Giveaway-only emoji uploads; keep these separate from shared UI icons.
    "giveaway_entry": "1561260013937360927",
    "giveaway_reaction": "1561262732882481203",
    "giveaway_prize": "1469414882859421696",
    "giveaway_dot": "1469409134989410325",
    "giveaway_approve": "1540579826321203240",
    "lb": "1468532486849040405",
    "arrow": "1479590739490111703",
    # Exact arrow used by the leaderboard voice-stats card.
    "leaderboard_arrow": "1468502576533209109",
    "1st": "1468513755024461978",
    "2nd": "1468513791208587264",
    "3rd": "1468513826780614812",
    "vclbdot": "1468513181986193474",
    "logging": "1461389053302804514",
    "moderation": "1461386680173990029",
    "music": "1461392621787287644",
    # Music controls and platform icons synced as application emojis.
    "music_autoplay": "1561284286613823594",
    "music_shuffle": "1561284318020505673",
    "music_loop": "1561284358344548372",
    "music_skip": "1561284058888151080",
    "music_previous": "1561284055926968363",
    "music_pause": "1561284057239650385",
    "music_stop": "1561284060939288586",
    "music_volume": "1561284059886264360",
    "music_youtube_music": "1555862787169718273",
    "music_youtube": "1516482942237544600",
    "music_spotify": "1555851518089896017",
    "permit": "1461399592104099985",
    "reactionrole": "1461403122869080089",
    "ticket": "1461387700623118377",
    # Ticket-only emoji assets copied from the supplied ticket project. These
    # names are deliberately namespaced so no other bot feature changes icons.
    "ticket_success": "1518137986095644703",
    "ticket_cross": "1518137821355966494",
    "ticket_info": "1517892973482868960",
    "ticket_warning": "1518063335923257386",
    "ticket_ticket": "1513730006910500977",
    "ticket_bug": "1518063885855358996",
    "ticket_claim": "1518064508948447242",
    "ticket_view": "1518064705854373969",
    "ticket_reopen": "1518064251405733898",
    "ticket_question": "1518064034862076007",
    "ticket_delete": "1518064106723217418",
    "ticket_setup": "1518064880769568828",
    "ticket_custom": "1518065679041957989",
    "Utility": "1461386829503795353",
    "vanity": "1461406246585237554",
    "voice": "1461386731545956403",
    "voicemaster": "1462388296218054809",
    "welcomer": "1461386794921754624",
    "links": "1461420300620136660",
    "txck": "1461765763156414464",
    "tick": "1461765194874228766",
    "loading": "1560188971223621663",
    "error": "1461887220578386136",
    "disabledyes": "1461811912785526804",
    "enableno": "1461812020344258691",
    "disableno": "1461812152565497869",
    "enabledyes": "1461809966557167651",
    "delete": "1560580056252682240",
    "left": "1560579659567988777",
    "previous": "1560579927294738532",
    "right": "1560580008601452585",
    "skip": "1560579978368778270",
    "members": "1479908226135887912",
    "wtick": "1479908338992152686",
    "greentick": "1479908224017760507",
    "reason": "1479909875810304060",
    "space": "1479908228824563943",
    "cross": "1439364706157461504",
    "latency": "1468688768172163124",
    "automod_disable_no": "1439362198882353458",
    "automod_enable_yes": "1439362254658211860",
    "automod_disabled_yes": "1439362256705028378",
    "automod_enable_no": "1439362201281495102",
    "automod_left": "1553051937576853645",
    "automod_settings": "1421449928559038465",
    "automod_cross": "1553051924100816937",
    "automod_tick": "1421447669288800298",
    "approve": "1540579826321203240",
    "antibot_success": "1439364717163315485",
    "antibot_settings": "1421449928559038465",
    "arrow_arrow": "1425840972155322530",
    "dotmoving": "1439362203802140674",
    "voice_lock": "1462393672653082718",
    "voice_unlock": "1462393705339289631",
    "voice_hide": "1462393947275137024",
    "voice_unhide": "1462393979550306426",
    "voice_rename": "1462394074211291189",
    "voice_limit": "1462395369274085397",
    "voice_mute": "1462403621172805857",
    "voice_unmute": "1462403434748711075",
    "voice_deafen": "1462401241899602038",
    "voice_undeafen": "1462401539552575582",
    "voice_permit": "1462410270885544043",
    "voice_ban": "1462400668126937202",
    "voice_transfer": "1462394223792881785",
    "voice_claim": "1462394162799444076",
    # Server/member statistics emojis from the user's reference screenshots.
    "member_total_anim": "1439362266473435156",
    "member_human": "1439362218360574133",
    "member_bots_anim": "1439362216242450775",
    "status_online": "1439362288724349142",
    "status_dnd": "1406783279738392616",
    "status_idle": "1406783067116277841",
    "status_offline": "1406783559754055860",
    "stats_members": "1455858658821799968",
    "stats_users": "1455861968735965219",
    "stats_guilds": "1455861703563677778",
    "stats_shards": "1455861706642296936",
    "stats_shard_id": "1455862352669970484",
    # Autorole-only application emoji uploads. Namespaced so no other command
    # changes its current emoji when this command family is customized.
    "autorole_dotmoving": "1439362203802140674",
    "autorole_error": "1439364714994995491",
    "autorole_success": "1439364717163315485",
    "autorole_settings": "1421449928559038465",
    "autorole_arrow": "1425840972155322530",
    # AFK-only application emoji uploads from the supplied reference.
    "afk_greet": "1439362116585787614",
    "afk_success": "1439364717163315485",
    "afk_arrow_arrow": "1425840972155322530",
    "afk_global_button": "1561199386804097074",
    "afk_server_button": "1561199362284200026",
    "afk_thinking": "1435758219296505897",
    "afk_wtick": "1479908338992152686",
    "afk_members": "1479908226135887912",
    "afk_space": "1479908228824563943",
    "afk_arrow": "1479590739490111703",
    "afk_pinged": "1442874165093728358",
    "vcrole_duplicate": "1439364712654442576",
    "whitetick": "1439364708409934116",
}
ANIMATED_APPLICATION_EMOJIS = {"loading", "automod_settings", "antibot_settings", "arrow_arrow", "dotmoving", "member_total_anim", "member_bots_anim", "autorole_dotmoving", "autorole_settings", "autorole_arrow", "afk_arrow_arrow", "afk_server_button", "afk_thinking", "leveling_firenation"}
_APPLICATION_MARKUP: dict[str, str] = {}


def _load_custom() -> dict[str, str]:
    try:
        values = json.loads(os.getenv("CUSTOM_EMOJIS_JSON", "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(values, dict):
        return {}
    return {
        name: value
        for name, value in values.items()
        if isinstance(name, str)
        and _NAME_RE.fullmatch(name)
        and isinstance(value, str)
        and value.strip()
    }

EMOJI = {
    "ticket_success": "✅",
    "ticket_cross": "❌",
    "ticket_info": "ℹ️",
    "ticket_warning": "⚠️",
    "ticket_ticket": "🎫",
    "ticket_bug": "🐛",
    "ticket_claim": "🙋",
    "ticket_view": "👁️",
    "ticket_reopen": "🔓",
    "ticket_question": "❓",
    "ticket_delete": "🗑️",
    "ticket_setup": "⚙️",
    "ticket_custom": "📝",
    "success": "<:tick:1461765194874228766>",
    "tick": "<:tick:1461765194874228766>",
    "txck": "<:txck:1461765763156414464>",
    "error": "<:error:1461887220578386136>",
    "disabledyes": "<:disabledyes:1461811912785526804>",
    "enableno": "<:enableno:1461812020344258691>",
    "disableno": "<:disableno:1461812152565497869>",
    "enabledyes": "<:enabledyes:1461809966557167651>",
    "delete": "<:delete:1560580056252682240>",
    "left": "<:left:1560579659567988777>",
    "previous": "<:previous:1560579927294738532>",
    "right": "<:right:1560580008601452585>",
    "skip": "<:skip:1560579978368778270>",
    "arrow": "<:arrow:1479590739490111703>",
    "leaderboard_arrow": "<:leaderboard_arrow:1468502576533209109>",
    "1st": "<:1st:1468513755024461978>",
    "2nd": "<:2nd:1468513791208587264>",
    "3rd": "<:3rd:1468513826780614812>",
    "vclbdot": "<:vclbdot:1468513181986193474>",
    "members": "<:members:1479908226135887912>",
    "wtick": "<:wtick:1479908338992152686>",
    "greentick": "<:greentick:1479908224017760507>",
    "reason": "<:reason:1479909875810304060>",
    "space": "<:space:1479908228824563943>",
    "cross": "<:cross:1439364706157461504>",
    "automod_disable_no": "<:disable_no:1439362198882353458>",
    "automod_enable_yes": "<:enable_yes:1439362254658211860>",
    "automod_disabled_yes": "<:disabled_yes:1439362256705028378>",
    "automod_enable_no": "<:enable_no:1439362201281495102>",
    "approve": "<:approve:1540579826321203240>",
    "automod_left": "<:left:1553051937576853645>",
    "automod_settings": "<a:settings:1421449928559038465>",
    "automod_cross": "<:cross:1553051924100816937>",
    "automod_tick": "<:Tick:1421447669288800298>",
    "automod_approve": "<:approve:1540579826321203240>",
    "antibot_success": "<:antibot_success:1439364717163315485>",
    "antibot_settings": "<a:antibot_settings:1421449928559038465>",
    "arrow_arrow": "<a:arrow_arrow:1425840972155322530>",
    "dotmoving": "<a:dotmoving:1439362203802140674>",
    "autorole_dotmoving": "<a:autorole_dotmoving:1439362203802140674>",
    "autorole_error": "<:autorole_error:1439364714994995491>",
    "autorole_success": "<:autorole_success:1439364717163315485>",
    "autorole_settings": "<a:autorole_settings:1421449928559038465>",
    "autorole_arrow": "<a:autorole_arrow:1425840972155322530>",
    "afk_greet": "<:afk_greet:1439362116585787614>",
    "afk_success": "<:afk_success:1439364717163315485>",
    "afk_arrow_arrow": "<a:afk_arrow_arrow:1425840972155322530>",
    "afk_global_button": "<:afk_global_button:1561199386804097074>",
    "afk_server_button": "<a:afk_server_button:1561199362284200026>",
    "afk_thinking": "<a:afk_thinking:1435758219296505897>",
    "afk_wtick": "<:afk_wtick:1479908338992152686>",
    "afk_members": "<:afk_members:1479908226135887912>",
    "afk_space": "<:afk_space:1479908228824563943>",
    "afk_arrow": "<:afk_arrow:1479590739490111703>",
    "afk_pinged": "<:thug_wala_yoo:1442874165093728358>",
    "vcrole_duplicate": "<:duplicate:1439364712654442576>",
    "latency": "<:latency:1468688768172163124>",
    "antispam": "📨",
    "anticaps": "🔠",
    "antilink": "🔗",
    "antiinvite": "📩",
    "antimention": "📣",
    "antiemoji": "😵",
    "antinsfw": "🔞",
    "warning": "⚠️",
    "info": "ℹ️",
    "security": "<:antinuke:1461385631098994688>",
    "settings": "⚙️",
    "moderation": "<:moderation:1461386680173990029>",
    "member": "👤",
    "channel": "📍",
    "enabled": "🟢",
    "disabled": "🔴",
    "loading": "⏳",
    "shield": "🔐",
    "automod": "<:automod:1461385691106775172>",
    "welcome": "<:welcomer:1461386794921754624>",
    "voice": "<:voice:1461386731545956403>",
    "role": "🎭",
    "antinuke": "<:antinuke:1461385631098994688>",
    "kick": "👢",
    "timeout": "⏱️",
    "cleanup": "🧹",
    "back": "↩️",
    "edit": "✏️",
    "folder": "📁",
    "emoji": "😀",
    "automations": "<:automation:1461396049175253250>",
    "automation": "<:automation:1461396049175253250>",
    "autoresponder": "<:autoresponder:1461386512527528031>",
    "customrole": "<:customrole:1461386631163543808>",
    "fun": "<:fun:1461389095917060338>",
    "general": "<:General:1461386931454873720>",
    "giveaway": "<:giveaway:1461405237368590530>",
    "giveaway_entry": "<:giveaway_entry:1561260013937360927>",
    "giveaway_reaction": "<:winner:1561262732882481203>",
    "giveaway_prize": "<:prize:1469414882859421696>",
    "giveaway_dot": "<:dot:1469409134989410325>",
    "giveaway_approve": "<:approve:1540579826321203240>",
    "leaderboard": "<:lb:1468532486849040405>",
    "logging": "<:logging:1461389053302804514>",
    "music": "<:music:1461392621787287644>",
    "music_autoplay": "<:autoplay:1561284286613823594>",
    "music_shuffle": "<:shuffle:1561284318020505673>",
    "music_loop": "<:loop:1561284358344548372>",
    "music_skip": "<:skip:1561284058888151080>",
    "music_previous": "<:previous:1561284055926968363>",
    "music_pause": "<:pause:1561284057239650385>",
    "music_stop": "<:stop:1561284060939288586>",
    "music_volume": "<:volume:1561284059886264360>",
    "music_youtube_music": "<:yt:1555862787169718273>",
    "music_youtube": "<:YouTube_Logo:1516482942237544600>",
    "music_spotify": "<:spotify:1555851518089896017>",
    "permit": "<:permit:1461399592104099985>",
    "reactionroles": "<:reactionrole:1461403122869080089>",
    "ticket": "<:ticket:1461387700623118377>",
    "utility": "<:Utility:1461386829503795353>",
    "vanityroles": "<:vanity:1461406246585237554>",
    "vanity_status": "<:emoji_1770819770230:1561250479281799198>",
    "voicemaster": "<:voicemaster:1462388296218054809>",
    "welcomer": "<:welcomer:1461386794921754624>",
    "links": "<:links:1461420300620136660>",
    "voice_lock": "<:lock:1462393672653082718>",
    "voice_unlock": "<:unlock:1462393705339289631>",
    "voice_hide": "<:hide:1462393947275137024>",
    "voice_unhide": "<:unhide:1462393979550306426>",
    "voice_rename": "<:rename:1462394074211291189>",
    "voice_limit": "<:limit:1462395369274085397>",
    "voice_mute": "<:mute:1462403621172805857>",
    "voice_unmute": "<:unmute:1462403434748711075>",
    "voice_deafen": "<:deafen:1462401241899602038>",
    "voice_undeafen": "<:undeafen:1462401539552575582>",
    "voice_permit": "<:permit:1462410270885544043>",
    "voice_ban": "<:ban:1462400668126937202>",
    "voice_transfer": "<:transfer:1462394223792881785>",
    "voice_claim": "<:Claim:1462394162799444076>",
    "member_total_anim": "<a:dcusers:1439362266473435156>",
    "member_human": "<:friend:1439362218360574133>",
    "member_bots_anim": "<a:bots:1439362216242450775>",
    "status_online": "<:online:1439362288724349142>",
    "status_dnd": "<:dnd:1406783279738392616>",
    "status_idle": "<:idle:1406783067116277841>",
    "status_offline": "<:offline:1406783559754055860>",
    "stats_members": "<:members:1455858658821799968>",
    "stats_users": "<:users:1455861968735965219>",
    "stats_guilds": "<:Clan:1455861703563677778>",
    "stats_shards": "<:PowerShard:1455861706642296936>",
    "stats_shard_id": "<:FireSoulShard:1455862352669970484>",
    "whitetick": "<:whitetick:1439364708409934116>",
}


def icon(name: str) -> str:
    # Prefer the emoji uploaded to this bot application's Developer Portal.
    # Old CUSTOM_EMOJIS_JSON values may point at another app or contain
    # literal Discord shortcodes such as :arrow:, which render as plain text.
    # Keep the supplied arrow emoji consistent in leaderboard and other
    # command responses. This exact markup also works before app sync finishes.
    if name == "arrow":
        return _APPLICATION_MARKUP.get(name, "<:arrow:1479590739490111703>")
    if name in _APPLICATION_MARKUP:
        return _APPLICATION_MARKUP[name]
    # If an application emoji has not synced yet, render Unicode instead of
    # emitting custom markup that is unavailable in the destination server.
    if name == "giveaway_entry":
        return "<:giveaway_entry:1561260013937360927>"
    if name == "giveaway_reaction":
        return "🎉"
    if name == "delete":
        return "🗑️"
    raw = _CUSTOM.get(name)
    if isinstance(raw, str) and raw:
        if _ID_RE.fullmatch(raw) and _NAME_RE.fullmatch(name):
            return f"<:{name}:{raw}>"
        if _MARKUP_RE.fullmatch(raw):
            return raw
        # Non-markup strings intentionally allow Unicode emoji overrides.
        if not raw.startswith("<") and not _SHORTCODE_RE.fullmatch(raw):
            return raw
    application_name = {
        "security": "antinuke",
        "info": "General",
        "member": "members",
        "role": "customrole",
        "channel": "voice",
        "general": "General",
        "automod_approve": "approve",
        "automations": "automation",
        "leaderboard": "lb",
        "reactionroles": "reactionrole",
        "utility": "Utility",
        "vanityroles": "vanity",
        "welcome": "welcomer",
    }.get(name, name)
    if application_name in _APPLICATION_MARKUP:
        return _APPLICATION_MARKUP[application_name]
    return EMOJI.get(name, "•")


async def sync_application_emojis(bot) -> None:
    """Refresh the app emoji set once per version, then reuse the synced assets."""
    application_id = getattr(bot, "application_id", None) or getattr(bot.user, "id", None)
    if not application_id:
        log.warning("Skipping application emoji sync: application ID is unavailable")
        return

    try:
        route = discord.http.Route
        existing_payload = await bot.http.request(route("GET", "/applications/{application_id}/emojis", application_id=application_id))
        existing = {item["name"]: item for item in existing_payload.get("items", []) if item.get("name")}
    except Exception:
        log.exception("Could not list application emojis; preserving the current emoji set")
        return

    try:
        saved_version = await bot.db.get_application_emoji_sync_version(application_id)
    except Exception:
        log.exception("Could not read application emoji sync version; forcing a full refresh")
        saved_version = None
    force_refresh = saved_version != APPLICATION_EMOJI_SYNC_VERSION
    desired_names = set(APPLICATION_EMOJI_SOURCES)
    resolved: dict[str, str] = {}
    if not force_refresh:
        for name in desired_names.intersection(existing):
            current = existing[name]
            tag = "a" if current.get("animated") else ""
            resolved[name] = f"<{tag}:{name}:{current['id']}>"

    missing_sources = {
        name: source_id
        for name, source_id in APPLICATION_EMOJI_SOURCES.items()
        if force_refresh or name not in existing
    }
    images: dict[str, tuple[bytes, str]] = {}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for name, source_id in missing_sources.items():
                extension = "gif" if name in ANIMATED_APPLICATION_EMOJIS else "png"
                async with session.get(f"https://cdn.discordapp.com/emojis/{source_id}.{extension}") as response:
                    response.raise_for_status()
                    image = await response.read()
                    if not image:
                        raise ValueError(f"Source emoji {name} returned an empty image")
                    content_type = response.headers.get("Content-Type", "image/png").split(";", 1)[0]
                    images[name] = image, content_type
    except Exception:
        log.exception("Could not fetch all source emojis; leaving application emojis unchanged")
        return

    if force_refresh:
        log.info("Refreshing old application emojis for application %s", application_id)
        for item in existing.values():
            try:
                await bot.http.request(
                    route(
                        "DELETE",
                        "/applications/{application_id}/emojis/{emoji_id}",
                        application_id=application_id,
                        emoji_id=item["id"],
                    )
                )
                log.info("Removed old application emoji %s", item.get("name", item["id"]))
            except Exception:
                log.exception("Could not remove old application emoji %s; aborting refresh", item.get("name", item["id"]))
                return

    sync_complete = True
    for name, (image, content_type) in images.items():
        encoded_image = base64.b64encode(image).decode("ascii")
        try:
            created = await bot.http.request(
                route("POST", "/applications/{application_id}/emojis", application_id=application_id),
                json={"name": name, "image": f"data:{content_type};base64,{encoded_image}"},
            )
            tag = "a" if created.get("animated") else ""
            resolved[name] = f"<{tag}:{created['name']}:{created['id']}>"
            log.info("Uploaded application emoji %s", name)
        except Exception:
            log.exception("Could not upload application emoji %s", name)
            sync_complete = False

    if not sync_complete or len(resolved) != len(desired_names):
        log.error("Application emoji sync incomplete; skipped removal of unmatched emojis")
        return

    # Remove stale names after the desired set has been uploaded successfully.
    for item in existing.values():
        name = item.get("name", "")
        if name in desired_names or force_refresh:
            continue
        try:
            await bot.http.request(
                route(
                    "DELETE",
                    "/applications/{application_id}/emojis/{emoji_id}",
                    application_id=application_id,
                    emoji_id=item["id"],
                )
            )
            log.info("Removed duplicate legacy application emoji %s", name)
        except Exception:
            log.exception("Could not remove duplicate legacy application emoji %s", name)

    _APPLICATION_MARKUP.clear()
    _APPLICATION_MARKUP.update(resolved)
    try:
        await bot.db.set_application_emoji_sync_version(application_id, APPLICATION_EMOJI_SYNC_VERSION)
    except Exception:
        log.exception("Could not save application emoji sync version")
    log.info("Application emoji sync complete: %s emoji(s)", len(resolved))


_CUSTOM = _load_custom()
