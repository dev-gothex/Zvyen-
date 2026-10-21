"""Lavalink-backed music search, playback and controls."""

import asyncio
from collections import deque
from dataclasses import dataclass
import logging
import random
import re

import discord
from discord.ext import commands
import wavelink

from core.settings import settings
from utils.emojis import icon
from utils.ui import component_card, panel


log = logging.getLogger("zyven.music")
MUSIC_EMOJIS = {
    "autoplay": "music_autoplay",
    "shuffle": "music_shuffle",
    "loop": "music_loop",
    "skip": "music_skip",
    "previous": "music_previous",
    "pause": "music_pause",
    "stop": "music_stop",
    "volume": "music_volume",
}

PLATFORMS = {
    "youtube_music": ("YouTube Music", wavelink.TrackSource.YouTubeMusic, "music_youtube_music", "Stream from YouTube Music"),
    "youtube": ("YouTube", wavelink.TrackSource.YouTube, "music_youtube", "Stream from YouTube"),
    "spotify": ("Spotify", "spsearch", "music_spotify", "Stream from Spotify"),
    "soundcloud": ("SoundCloud", wavelink.TrackSource.SoundCloud, None, "Stream from SoundCloud"),
    "apple_music": ("Apple Music", "amsearch", None, "Stream from Apple Music"),
    "deezer": ("Deezer", "dzsearch", None, "Stream from Deezer"),
    "jiosaavn": ("JioSaavn", "jssearch", None, "Stream from JioSaavn"),
    "yandex": ("Yandex Music", "ymsearch", None, "Stream from Yandex Music"),
}


@dataclass
class Track:
    playable: wavelink.Playable
    requester: discord.Member
    text_channel: discord.abc.Messageable

    @property
    def title(self):
        return self.playable.title

    @property
    def url(self):
        return self.playable.uri or "https://discord.com"

    @property
    def artist(self):
        return self.playable.author or "Unknown artist"

    @property
    def duration(self):
        return int(self.playable.length / 1000)

    @property
    def thumbnail(self):
        return self.playable.artwork

    @property
    def identifier(self):
        return self.playable.identifier or self.url


def _duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _now_playing_embed(track: Track) -> discord.Embed:
    embed = panel(
        "Now Playing",
        f"**[{discord.utils.escape_markdown(track.title)}]({track.url})** — {discord.utils.escape_markdown(track.artist)}\n"
        f"Duration: `{_duration(track.duration)}`\nRequested by ({track.requester.mention})",
        color=0xE9AFCB,
    )
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


class VolumeModal(discord.ui.Modal, title="Set Music Volume"):
    volume = discord.ui.TextInput(label="Volume (0–200%)", min_length=1, max_length=3, default="100")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        player = await self.cog.check_interaction_player(interaction)
        if player is None:
            return
        try:
            percent = int(str(self.volume.value))
        except ValueError:
            return await interaction.response.send_message("Enter a whole number from 0 to 200.", ephemeral=True)
        if not 0 <= percent <= 200:
            return await interaction.response.send_message("Volume must be between 0 and 200 percent.", ephemeral=True)
        await player.voice_client.set_volume(percent)
        await interaction.response.send_message(f"Volume set to **{percent}%**.", ephemeral=True)


class MusicButton(discord.ui.Button):
    def __init__(self, cog, action: str, *, row: int, label: str | None = None, style=discord.ButtonStyle.secondary):
        super().__init__(
            label=label,
            emoji=discord.PartialEmoji.from_str(icon(MUSIC_EMOJIS[action])),
            style=style,
            custom_id=f"zyven:music:{action}",
            row=row,
        )
        self.cog = cog
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_control(interaction, self.action)


class MusicControlView(discord.ui.View):
    def __init__(self, cog, requester_id: int):
        super().__init__(timeout=3600)
        self.requester_id = requester_id
        self.add_item(MusicButton(cog, "previous", row=0))
        self.add_item(MusicButton(cog, "pause", row=0))
        self.add_item(MusicButton(cog, "skip", row=0))
        self.add_item(MusicButton(cog, "volume", row=0))
        self.add_item(MusicButton(cog, "stop", row=1, style=discord.ButtonStyle.danger))
        self.add_item(MusicButton(cog, "loop", row=2, label="Loop"))
        self.add_item(MusicButton(cog, "shuffle", row=2, label="Shuffle"))
        self.add_item(MusicButton(cog, "autoplay", row=2, label="Autoplay"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Only the person who requested this track can use its controls.", ephemeral=True)
        return False


class PlatformSelect(discord.ui.Select):
    def __init__(self, view):
        options = []
        for key, (label, _source, emoji, description) in PLATFORMS.items():
            kwargs = {"label": label, "value": key, "description": description}
            if emoji:
                kwargs["emoji"] = discord.PartialEmoji.from_str(icon(emoji))
            options.append(discord.SelectOption(**kwargs))
        super().__init__(placeholder="Select a platform to search...", min_values=1, max_values=1, options=options)
        self.search_view = view

    async def callback(self, interaction: discord.Interaction):
        await self.search_view.search_on_platform(interaction, self.values[0])


class PlatformSearchView(discord.ui.View):
    def __init__(self, cog, requester: discord.Member, query: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.requester_id = requester.id
        self.query = query
        self.add_item(PlatformSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who ran this search can choose a platform.", ephemeral=True)
            return False
        return True

    async def search_on_platform(self, interaction: discord.Interaction, key: str):
        await interaction.response.defer(ephemeral=True)
        player = await self.cog.get_player_for_member_from_interaction(interaction, connect=True)
        if player is None:
            return
        label, source, _emoji, _description = PLATFORMS[key]
        try:
            results = await wavelink.Playable.search(self.query, source=source)
            playable = results[0] if results else None
            if playable is None:
                return await interaction.followup.send(f"No playable results found on **{label}**.", ephemeral=True)
            track = Track(playable, interaction.user, interaction.channel)
            await self.cog.enqueue(player, track)
            await interaction.followup.send(f"Added **{discord.utils.escape_markdown(track.title)}** from **{label}** to the queue.", ephemeral=True)
        except Exception as exc:
            log.info("Music search failed for platform %s: %s", label, exc)
            await interaction.followup.send(
                f"Couldn't search **{label}**. That source may need to be enabled on the Lavalink server.",
                ephemeral=True,
            )


class GuildPlayer:
    def __init__(self, cog, voice_client: wavelink.Player):
        self.cog = cog
        self.voice_client = voice_client
        self.guild_id = voice_client.guild.id
        self.queue: deque[Track] = deque()
        self.history: deque[Track] = deque(maxlen=30)
        self.current: Track | None = None
        self.loop_current = False
        self.shuffle_enabled = False
        self.autoplay_enabled = False
        self.stop_requested = False
        self.skip_requested = False
        self.previous_requested = False
        self._lock = asyncio.Lock()
        self._now_playing_lock = asyncio.Lock()
        self.now_playing_message: discord.Message | None = None


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}

    async def reply(self, ctx, title: str, description: str, *, color: int = 0x5865F2):
        await ctx.send(view=component_card(panel(title, description, color=color)), allowed_mentions=discord.AllowedMentions.none())

    async def ensure_player(self, guild: discord.Guild, member: discord.Member, *, connect: bool, channel=None):
        if member.voice is None or member.voice.channel is None:
            return None
        voice_client = guild.voice_client
        if voice_client and voice_client.channel.id != member.voice.channel.id:
            return None
        if voice_client is None and connect:
            try:
                voice_client = await member.voice.channel.connect(cls=wavelink.Player, self_deaf=True)
            except Exception as exc:
                log.info("Lavalink voice connect failed in guild %s: %s", guild.id, exc)
                return None
        if voice_client is None:
            return None
        player = self.players.get(guild.id)
        if player is None or player.voice_client is not voice_client:
            player = GuildPlayer(self, voice_client)
            self.players[guild.id] = player
        return player

    async def get_player_for_member(self, ctx, *, connect: bool = False) -> GuildPlayer | None:
        if not isinstance(ctx.author, discord.Member):
            return None
        player = await self.ensure_player(ctx.guild, ctx.author, connect=connect, channel=ctx.channel)
        if player is None:
            if ctx.author.voice is None or ctx.author.voice.channel is None:
                await self.reply(ctx, "Join a Voice Channel", "Join a voice channel first to use music commands.", color=0xFEE75C)
            elif ctx.guild.voice_client and ctx.guild.voice_client.channel != ctx.author.voice.channel:
                await self.reply(ctx, "Different Voice Channel", f"Join {ctx.guild.voice_client.channel.mention} to control the current player.", color=0xFEE75C)
            elif not connect:
                await self.reply(ctx, "Nothing Playing", f"Use `{settings.prefix}play <song or URL>` to start music first.", color=0xFEE75C)
            else:
                await self.reply(ctx, "Lavalink Unavailable", "The music server is unavailable or the bot lacks Connect/Speak permissions.", color=0xED4245)
        return player

    async def get_player_for_member_from_interaction(self, interaction, *, connect=False):
        if not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Join a voice channel first.", ephemeral=True)
            return None
        player = await self.ensure_player(interaction.guild, interaction.user, connect=connect, channel=interaction.channel)
        if player is None:
            await interaction.followup.send("Join a voice channel first, or check the bot's voice permissions and Lavalink connection.", ephemeral=True)
        return player

    async def check_interaction_player(self, interaction: discord.Interaction) -> GuildPlayer | None:
        player = self.players.get(interaction.guild_id or 0)
        member = interaction.user
        if player is None or not player.voice_client.connected:
            await interaction.response.send_message("There is no active music player in this server.", ephemeral=True)
            return None
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel.id != player.voice_client.channel.id:
            await interaction.response.send_message("Join the bot's voice channel to use these controls.", ephemeral=True)
            return None
        return player

    async def enqueue(self, player: GuildPlayer, track: Track):
        async with player._lock:
            if player.current is None and player.voice_client.current is None:
                player.current = track
                await player.voice_client.play(track.playable)
            else:
                if player.shuffle_enabled and player.queue:
                    player.queue.insert(random.randrange(len(player.queue) + 1), track)
                else:
                    player.queue.append(track)

    async def find_autoplay_track(self, previous: Track, used: set[str]):
        """Find a fresh, related song using YouTube-style radio queries."""
        queries = (
            f"{previous.title} {previous.artist} related songs",
            f"{previous.artist} similar songs",
            f"{previous.artist} radio",
        )
        sources = (
            wavelink.TrackSource.YouTubeMusic,
            wavelink.TrackSource.YouTube,
            wavelink.TrackSource.SoundCloud,
        )
        previous_title = re.sub(r"[^a-z0-9]+", " ", previous.title.casefold()).strip()
        for query in queries:
            for source in sources:
                try:
                    results = await wavelink.Playable.search(query, source=source)
                except Exception as exc:
                    log.debug("Autoplay search failed for %s in guild %s: %s", source, previous.requester.guild.id if previous.requester.guild else "DM", exc)
                    continue
                candidates = [
                    item for item in results[:10]
                    if (item.identifier or item.uri or item.title) not in used
                    and not getattr(item, "is_stream", False)
                    and 45_000 <= int(getattr(item, "length", 0) or 0) <= 1_200_000
                    and re.sub(r"[^a-z0-9]+", " ", item.title.casefold()).strip() != previous_title
                ]
                if candidates:
                    return random.choice(candidates)
        return None

    async def advance(self, player: GuildPlayer):
        async with player._lock:
            if player.stop_requested:
                return
            if player.current and player.skip_requested is False and player.loop_current:
                next_track = player.current
            elif player.previous_requested and player.queue:
                next_track = player.queue.popleft()
            elif player.queue:
                if player.current:
                    player.history.append(player.current)
                next_track = player.queue.popleft()
            elif player.autoplay_enabled and player.current:
                previous = player.current
                used = {previous.identifier, *(item.identifier for item in player.history)}
                selected = await self.find_autoplay_track(previous, used)
                if selected:
                    player.history.append(previous)
                    next_track = Track(selected, previous.requester, previous.text_channel)
                else:
                    log.warning("Autoplay found no related track in guild %s; check YouTube/YouTube Music sources on Lavalink", player.guild_id)
                    player.history.append(previous)
                    player.current = None
                    return
            else:
                if player.current:
                    player.history.append(player.current)
                player.current = None
                return
            player.skip_requested = False
            player.previous_requested = False
            player.current = next_track
            if next_track:
                try:
                    await player.voice_client.play(next_track.playable)
                except Exception:
                    log.exception("Could not start next track in guild %s", player.guild_id)
                    player.current = None

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        if payload.player is None or payload.player.guild is None:
            return
        player = self.players.get(payload.player.guild.id)
        if player is None or player.current is None:
            return
        async with player._now_playing_lock:
            previous_card = player.now_playing_message
            player.now_playing_message = None
            if previous_card is not None:
                try:
                    await previous_card.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    log.info("Could not remove previous now-playing card in guild %s", player.guild_id)
            try:
                player.now_playing_message = await player.current.text_channel.send(
                    view=component_card(_now_playing_embed(player.current), MusicControlView(self, player.current.requester.id)),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                log.info("Could not post now-playing card in guild %s", player.guild_id)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        if payload.player is None or payload.player.guild is None or payload.reason not in {"finished", "loadFailed", "stopped"}:
            return
        player = self.players.get(payload.player.guild.id)
        if player:
            await self.advance(player)

    async def handle_control(self, interaction: discord.Interaction, action: str):
        if action == "volume":
            if await self.check_interaction_player(interaction) is None:
                return
            return await interaction.response.send_modal(VolumeModal(self))
        player = await self.check_interaction_player(interaction)
        if player is None:
            return
        voice = player.voice_client
        message = ""
        if action == "pause":
            await voice.pause(not voice.paused)
            message = "Playback resumed." if not voice.paused else "Playback paused."
        elif action == "skip":
            if voice.playing:
                player.skip_requested = True
                await voice.skip()
                message = "Skipped the current track."
            else:
                message = "Nothing is currently playing."
        elif action == "previous":
            if not player.history:
                message = "There is no previous track yet."
            else:
                previous = player.history.pop()
                if player.current:
                    player.queue.appendleft(player.current)
                player.queue.appendleft(previous)
                player.previous_requested = True
                player.skip_requested = True
                await voice.skip()
                message = "Playing the previous track."
        elif action == "stop":
            await self.stop_player(player)
            message = "Playback stopped and the bot left the voice channel."
        elif action == "loop":
            player.loop_current = not player.loop_current
            message = f"Loop {'enabled' if player.loop_current else 'disabled'} by {interaction.user.display_name}."
        elif action == "shuffle":
            player.shuffle_enabled = not player.shuffle_enabled
            if player.shuffle_enabled:
                shuffled = list(player.queue)
                random.shuffle(shuffled)
                player.queue = deque(shuffled)
            message = f"Shuffle {'enabled' if player.shuffle_enabled else 'disabled'} by {interaction.user.display_name}."
        elif action == "autoplay":
            player.autoplay_enabled = not player.autoplay_enabled
            message = f"Autoplay {'enabled' if player.autoplay_enabled else 'disabled'} by {interaction.user.display_name}."
        await interaction.response.send_message(view=component_card(panel("Music Player", message, color=0xE9AFCB)), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def stop_player(self, player):
        player.stop_requested = True
        player.queue.clear()
        player.current = None
        if player.now_playing_message is not None:
            try:
                await player.now_playing_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            player.now_playing_message = None
        await player.voice_client.disconnect(force=True)
        self.players.pop(player.guild_id, None)

    @commands.command(name="play", aliases=("p",), case_insensitive=True)
    @commands.guild_only()
    async def play(self, ctx, *, query: str):
        player = await self.get_player_for_member(ctx, connect=True)
        if player is None:
            return
        try:
            clean_query = query.strip()
            is_url = bool(re.match(r"^https?://", clean_query, flags=re.IGNORECASE))
            sources = (None,) if is_url else (
                wavelink.TrackSource.YouTube,
                wavelink.TrackSource.YouTubeMusic,
                wavelink.TrackSource.SoundCloud,
            )
            playable = None
            failures = []
            for source in sources:
                try:
                    results = await wavelink.Playable.search(clean_query, source=source)
                    if results:
                        playable = results[0]
                        break
                except Exception as search_error:
                    failures.append(search_error)
                    log.debug("Music source %s search failed: %s", source, search_error)
            if playable is None:
                if failures and len(failures) == len(sources):
                    raise failures[-1]
                return await self.reply(ctx, "Song Not Found", "Couldn't find a playable result. Try another query or URL.", color=0xED4245)
            track = Track(playable, ctx.author, ctx.channel)
            queued_before = len(player.queue)
            starting = player.current is None
            await self.enqueue(player, track)
        except Exception as exc:
            log.info("Lavalink search failed in guild %s: %s", ctx.guild.id, exc)
            return await self.reply(ctx, "Music Search Failed", "The Lavalink node could not search this track. Check the node and its source plugins.", color=0xED4245)
        if starting:
            await self.reply(ctx, "Starting Playback", f"Added **[{discord.utils.escape_markdown(track.title)}]({track.url})** to the player.")
        else:
            await self.reply(ctx, "Added to Queue", f"**[{discord.utils.escape_markdown(track.title)}]({track.url})** — `{_duration(track.duration)}`\nPosition: **{queued_before + 1}**")

    @commands.command(name="search", aliases=("find",), case_insensitive=True)
    @commands.guild_only()
    async def search(self, ctx, *, query: str):
        """Choose a music platform and search for a track."""
        view = PlatformSearchView(self, ctx.author, query.strip())
        await ctx.send(view=component_card(panel("Search Music", f"Choose a platform to search for **{discord.utils.escape_markdown(query.strip())}**."), view), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="pause", case_insensitive=True)
    @commands.guild_only()
    async def pause(self, ctx):
        player = await self.get_player_for_member(ctx)
        if player and player.voice_client.playing:
            await player.voice_client.pause(not player.voice_client.paused)
            await self.reply(ctx, "Playback Updated", "Playback resumed." if not player.voice_client.paused else "Playback paused.")

    @commands.command(name="resume", case_insensitive=True)
    @commands.guild_only()
    async def resume(self, ctx):
        player = await self.get_player_for_member(ctx)
        if player and player.voice_client.paused:
            await player.voice_client.pause(False)
            await self.reply(ctx, "Playback Resumed", f"Resumed by {ctx.author.mention}.")
        elif player:
            await self.reply(ctx, "Nothing Paused", "There is no paused track to resume.", color=0xFEE75C)

    @commands.command(name="skip", aliases=("next",), case_insensitive=True)
    @commands.guild_only()
    async def skip(self, ctx):
        player = await self.get_player_for_member(ctx)
        if player and player.voice_client.playing:
            player.skip_requested = True
            await player.voice_client.skip()
            await self.reply(ctx, "Track Skipped", f"Skipped by {ctx.author.mention}.")
        elif player:
            await self.reply(ctx, "Nothing Playing", "There is no active track to skip.", color=0xFEE75C)

    @commands.command(name="previous", aliases=("prev", "back"), case_insensitive=True)
    @commands.guild_only()
    async def previous(self, ctx):
        player = await self.get_player_for_member(ctx)
        if player is None:
            return
        if not player.history:
            return await self.reply(ctx, "No Previous Track", "Play a track first.", color=0xFEE75C)
        previous = player.history.pop()
        if player.current:
            player.queue.appendleft(player.current)
        player.queue.appendleft(previous)
        player.previous_requested = True
        player.skip_requested = True
        await player.voice_client.skip()
        await self.reply(ctx, "Previous Track", f"Playing **{discord.utils.escape_markdown(previous.title)}** again.")

    @commands.command(name="stop", case_insensitive=True)
    @commands.guild_only()
    async def stop(self, ctx):
        player = await self.get_player_for_member(ctx)
        if player:
            await self.stop_player(player)
            await self.reply(ctx, "Music Stopped", f"Playback stopped by {ctx.author.mention}.")

    @commands.command(name="join", aliases=("summon",), case_insensitive=True)
    @commands.guild_only()
    async def join(self, ctx):
        player = await self.get_player_for_member(ctx, connect=True)
        if player:
            await self.reply(ctx, "Joined Voice Channel", f"Connected to {player.voice_client.channel.mention}.")

    @commands.command(name="leave", aliases=("disconnect",), case_insensitive=True)
    @commands.guild_only()
    async def leave(self, ctx):
        player = await self.get_player_for_member(ctx)
        if player:
            await self.stop_player(player)
            await self.reply(ctx, "Left Voice Channel", "Stopped playback and disconnected.")

    @commands.command(name="queue", aliases=("q",), case_insensitive=True)
    @commands.guild_only()
    async def queue(self, ctx):
        player = self.players.get(ctx.guild.id)
        if player is None or (player.current is None and not player.queue):
            return await self.reply(ctx, "Music Queue", "The queue is empty.", color=0xFEE75C)
        rows = []
        if player.current:
            rows.append(f"**Now:** [{discord.utils.escape_markdown(player.current.title)}]({player.current.url})")
        rows.extend(f"**{index}.** [{discord.utils.escape_markdown(track.title)}]({track.url}) — `{_duration(track.duration)}`" for index, track in enumerate(list(player.queue)[:10], start=1))
        if len(player.queue) > 10:
            rows.append(f"…and {len(player.queue) - 10} more track(s).")
        await self.reply(ctx, "Music Queue", "\n".join(rows))

    @commands.command(name="nowplaying", aliases=("np",), case_insensitive=True)
    @commands.guild_only()
    async def now_playing(self, ctx):
        player = self.players.get(ctx.guild.id)
        if player is None or player.current is None:
            return await self.reply(ctx, "Nothing Playing", "There is no current track.", color=0xFEE75C)
        await ctx.send(view=component_card(_now_playing_embed(player.current), MusicControlView(self, ctx.author.id)), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="volume", aliases=("vol",), case_insensitive=True)
    @commands.guild_only()
    async def volume(self, ctx, percent: int):
        player = await self.get_player_for_member(ctx)
        if player is None:
            return
        if not 0 <= percent <= 200:
            return await self.reply(ctx, "Invalid Volume", "Choose a volume from **0** to **200** percent.", color=0xED4245)
        await player.voice_client.set_volume(percent)
        await self.reply(ctx, "Volume Updated", f"Volume set to **{percent}%**.")

    @commands.command(name="loop", case_insensitive=True)
    @commands.guild_only()
    async def loop(self, ctx):
        player = await self.get_player_for_member(ctx)
        if player:
            player.loop_current = not player.loop_current
            await self.reply(ctx, "Loop Updated", f"Loop {'enabled' if player.loop_current else 'disabled'} by {ctx.author.display_name}.")

    @commands.command(name="shuffle", case_insensitive=True)
    @commands.guild_only()
    async def shuffle(self, ctx):
        player = await self.get_player_for_member(ctx)
        if player:
            player.shuffle_enabled = not player.shuffle_enabled
            if player.shuffle_enabled:
                shuffled = list(player.queue)
                random.shuffle(shuffled)
                player.queue = deque(shuffled)
            await self.reply(ctx, "Shuffle Updated", f"Shuffle {'enabled' if player.shuffle_enabled else 'disabled'} by {ctx.author.display_name}.")

    @commands.command(name="autoplay", case_insensitive=True)
    @commands.guild_only()
    async def autoplay(self, ctx):
        player = await self.get_player_for_member(ctx)
        if player:
            player.autoplay_enabled = not player.autoplay_enabled
            await self.reply(ctx, "Autoplay Updated", f"Autoplay {'enabled' if player.autoplay_enabled else 'disabled'} by {ctx.author.display_name}.")


async def setup(bot):
    await bot.add_cog(Music(bot))
