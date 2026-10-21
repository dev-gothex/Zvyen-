"""Fun games and image-based roleplay commands."""
import asyncio
import random
import re
import logging
import time
from collections import deque
from io import BytesIO

import aiohttp
import discord
from discord.ext import commands
from ddgs import DDGS
from PIL import Image, ImageDraw

from core.settings import settings
from utils.ui import panel, component_card

log = logging.getLogger("zyven.fun")


def _local_fallback_gif(label: str) -> BytesIO:
    """Render a small animated fallback so GIF commands still return media offline."""
    label = re.sub(r"\s+", " ", label).strip()[:34] or "GIF"
    frames = []
    width, height = 480, 270
    for index in range(18):
        frame = Image.new("RGB", (width, height), (20, 15, 27))
        draw = ImageDraw.Draw(frame)
        x = 55 + (index * 19) % 370
        draw.rounded_rectangle((18, 18, 462, 252), radius=28, outline=(105, 75, 130), width=3)
        draw.ellipse((x - 22, 82, x + 22, 126), fill=(255, 74, 160))
        draw.text((32, 151), label, fill=(248, 239, 255), stroke_width=1, stroke_fill=(60, 42, 76))
        draw.text((32, 205), "Zyven • animated fallback", fill=(185, 165, 205))
        frames.append(frame)
    output = BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=85, loop=0, optimize=True)
    output.seek(0)
    return output


def _search_images(query: str, *, safesearch: str, image_type: str | None = None) -> list[str]:
    """Run no-key image search outside Discord's event loop."""
    with DDGS(timeout=3) as search:
        results = search.images(
            query,
            region="wt-wt",
            safesearch=safesearch,
            type_image=image_type,
            max_results=8,
            backend="bing",
        )
    return [item["image"] for item in results if isinstance(item.get("image"), str) and item["image"].startswith(("https://", "http://"))]

ROLEPLAY_ACTIONS = {
    "bite": "bite", "blush": "blush", "bonk": "bonk", "cry": "cry",
    "cuddle": "cuddle", "dance": "dance", "feed": "feed", "handhold": "handhold",
    "happy": "happy", "highfive": "highfive", "hug": "hug", "kill": "slap",
    "kiss": "kiss", "lappillow": "lappillow", "pat": "pat", "poke": "poke",
    "punch": "punch", "slap": "slap", "smash": "punch", "smug": "smug",
    "spank": "slap", "stare": "stare", "tickle": "tickle", "wave": "wave",
    "wink": "wink", "yeet": "yeet",
}
ROLEPLAY_HELP = {
    name: f"Send a random anime GIF of you {name}ing another member."
    for name in ROLEPLAY_ACTIONS
}
ROLEPLAY_HELP.update({"kill": "Playfully bonk another member with a random anime GIF.", "smash": "Playfully punch another member with a random anime GIF."})
SCORE_COMMANDS = {
    "howgay": "Only you get to choose how you identify. Be kind and proud of who you are.",
    "lesbian": "Only you get to choose how you identify. Be kind and proud of who you are.",
    "cute": "Cute meter",
    "intelligence": "Big-brain meter",
    "chutya": "Silly meter",
    "horny": "Bonk meter (keep it wholesome)",
    "tharki": "Bonk meter (keep it respectful)",
}


class Fun(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._dynamic_commands = []
        self._image_cache: dict[tuple[str, str, str], tuple[float, list[str]]] = {}
        self._gif_cache: dict[str, tuple[float, list[str]]] = {}
        self._gif_api_requests: dict[str, deque[float]] = {}
        self._gif_api_cooldowns: dict[str, float] = {}
        self._gif_api_lock = asyncio.Lock()
        self._gif_slots = asyncio.Semaphore(4)
        self._search_slots = asyncio.Semaphore(3)
        self._http_session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        timeout = aiohttp.ClientTimeout(total=3, connect=1.5, sock_read=1.5)
        self._http_session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": f"ZyvenBot ({settings.support_url})"},
        )
        for action in ROLEPLAY_ACTIONS:
            callback = self._make_roleplay_callback(action)
            commands.cooldown(1, 2, commands.BucketType.user)(callback)
            command = commands.Command(callback, name=action, help=ROLEPLAY_HELP[action])
            self.bot.add_command(command)
            self._dynamic_commands.append(action)
        for name, label in SCORE_COMMANDS.items():
            callback = self._make_score_callback(name, label)
            command = commands.Command(callback, name=name, help=f"Send a harmless random {name} joke score.")
            self.bot.add_command(command)
            self._dynamic_commands.append(name)

    def _make_roleplay_callback(self, action):
        async def callback(ctx, member: discord.Member | None = None, *extra):
            # Be tolerant of Cog dispatch adding a bound cog argument; this
            # used to raise a positional-argument TypeError on GIF commands.
            values = (ctx, member, *extra)
            command_ctx = next((value for value in values if isinstance(value, commands.Context)), None)
            if command_ctx is None:
                return
            target = next((value for value in reversed(values) if isinstance(value, discord.Member)), command_ctx.author)
            async with command_ctx.typing():
                await self._roleplay(command_ctx, action, target)
        return callback

    def _make_score_callback(self, name, label):
        async def callback(ctx, member: discord.Member | None = None, *extra):
            values = (ctx, member, *extra)
            command_ctx = next((value for value in values if isinstance(value, commands.Context)), None)
            if command_ctx is None:
                return
            member = next((value for value in reversed(values) if isinstance(value, discord.Member)), command_ctx.author)
            score = random.randint(0, 100)
            if name == "howgay":
                title = "About your gayness"
                description = f"{member.mention} is **{score}% Gay** 🌈"
                footer = f"How gay are you? – {command_ctx.author.display_name}"
            elif name == "lesbian":
                title = "About your lesbianism"
                description = f"{member.mention} is **{score}% Lesbian** 🌈"
                footer = f"Lesbian meter • {command_ctx.author.display_name}"
            elif name == "cute":
                title = "About your cuteness"
                description = f"{member.mention} is **{score}% Cute** 🥰"
                footer = f"How cute are you? – {command_ctx.author.display_name}"
            elif name == "intelligence":
                title = "About your intelligence"
                description = f"{member.mention} has an IQ of **{score}%** 🧠"
                footer = f"How intelligent are you? – {command_ctx.author.display_name}"
            else:
                title = f"About your {name}"
                description = f"{label} for {member.mention}: **{score}%**"
                footer = f"Just a random fun score • {command_ctx.author.display_name}"
            embed = discord.Embed(title=title, description=description, color=0xFF1688)
            embed.set_footer(text=footer)
            await command_ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        return callback

    async def cog_unload(self):
        for name in self._dynamic_commands:
            self.bot.remove_command(name)
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    async def _cached_search(self, query: str, *, safe: str, image_type: str | None = None) -> list[str]:
        key = (query.casefold(), safe, image_type or "")
        now = time.monotonic()
        cached = self._image_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        async with self._search_slots:
            # Another command may have filled this cache entry while this
            # request waited for a search slot.
            cached = self._image_cache.get(key)
            now = time.monotonic()
            if cached and cached[0] > now:
                return cached[1]
            results = await asyncio.to_thread(_search_images, query, safesearch=safe, image_type=image_type)
        if len(self._image_cache) > 256:
            self._image_cache = {cache_key: value for cache_key, value in self._image_cache.items() if value[0] > now}
        # Cache misses briefly too, so a provider outage does not make every
        # roleplay/GIF request repeat the same slow external lookup.
        self._image_cache[key] = (now + (180 if results else 20), results)
        return results

    async def _gif_results(self, cache_key: str, *, api_url: str, params: dict | None, fallback_query: str, search_fallback: bool = True) -> list[str]:
        now = time.monotonic()
        cached = self._gif_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
        cached = None
        urls = []
        request_key = "search" if api_url.rstrip("/").endswith("/search") else api_url.rstrip("/").rsplit("/", 1)[-1]
        async with self._gif_slots:
            cached = self._gif_cache.get(cache_key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            async with self._gif_api_lock:
                request_times = self._gif_api_requests.setdefault(request_key, deque())
                now = time.monotonic()
                window, limit = (5.0, 6) if request_key == "search" else (60.0, 180)
                while request_times and now - request_times[0] >= window:
                    request_times.popleft()
                cooldown_until = self._gif_api_cooldowns.get(request_key, 0.0)
                allowed = now >= cooldown_until and len(request_times) < limit
                if allowed:
                    request_times.append(now)
            if not allowed:
                log.info("Skipping GIF provider request after local %s request limit", request_key)
                self._gif_cache[cache_key] = (now + 15, [])
                return []
            if self._http_session and not self._http_session.closed:
                try:
                    async with self._http_session.get(api_url, params=params) as response:
                        if response.status == 429:
                            retry_after = response.headers.get("Retry-After", "15")
                            try:
                                pause_for = min(60.0, max(15.0, float(retry_after)))
                            except ValueError:
                                pause_for = 15.0
                            self._gif_api_cooldowns[request_key] = time.monotonic() + pause_for
                            self._gif_cache[cache_key] = (time.monotonic() + pause_for, [])
                            log.warning("GIF provider rate-limited %s requests; cooling this query for %.0f seconds", request_key, pause_for)
                            return []
                        response.raise_for_status()
                        data = await response.json()
                    urls = [item["url"] for item in data.get("results", []) if isinstance(item.get("url"), str) and item["url"].startswith(("https://", "http://"))]
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
                    log.info("GIF source timed out or returned invalid data for category %s", request_key)
        if not urls and search_fallback:
            try:
                urls = await self._cached_search(fallback_query, safe="on", image_type="gif")
            except Exception as exc:
                log.info("GIF image-search fallback failed: %s", exc)
        if urls:
            self._gif_cache[cache_key] = (now + 90, urls)
        elif not cached:
            self._gif_cache[cache_key] = (time.monotonic() + 15, [])
        return urls

    async def _gif_file(self, url: str | None, fallback_label: str) -> tuple[BytesIO, str]:
        """Download the chosen media; if the host is flaky, always return a local GIF."""
        if url and self._http_session and not self._http_session.closed:
            try:
                async with self._http_session.get(url) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                    payload = await response.content.read(8 * 1024 * 1024 + 1)
                if content_type == "image/gif" and len(payload) <= 8 * 1024 * 1024:
                    stream = BytesIO(payload)
                    stream.seek(0)
                    return stream, "zyven-result.gif"
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                log.info("GIF media host failed; using locally generated fallback")
        return _local_fallback_gif(fallback_label), "zyven-fallback.gif"

    async def _roleplay(self, ctx, action: str, member: discord.Member):
        category = ROLEPLAY_ACTIONS[action]
        api_url = f"https://nekos.best/api/v2/{category}"
        urls = await self._gif_results(
            f"roleplay:{category}", api_url=api_url, params={"amount": 5},
            fallback_query=f"anime {category} reaction gif", search_fallback=False,
        )
        actor = discord.utils.escape_markdown(ctx.author.display_name)
        target = discord.utils.escape_markdown(member.display_name)
        verb = {
            "bite": "bit", "blush": "blushed at", "bonk": "bonked", "cry": "cried with",
            "cuddle": "cuddled", "dance": "danced with", "feed": "fed", "handhold": "held hands with",
            "happy": "cheered for", "highfive": "high-fived", "hug": "hugged", "kill": "bonked",
            "kiss": "kissed", "lappillow": "offered a lap pillow to", "pat": "patted", "poke": "poked",
            "punch": "punched", "slap": "slapped", "smash": "smashed", "smug": "smugly looked at",
            "spank": "playfully bonked", "stare": "stared at", "tickle": "tickled", "wave": "waved at",
            "wink": "winked at", "yeet": "yeeted",
        }.get(action, f"{action}ed")
        embed = discord.Embed(description=f"**{actor} {verb} {target}.**", color=0x5865F2)
        if urls:
            # These are direct, public GIF URLs from the provider. Let Discord
            # proxy the animation instead of downloading and re-uploading it.
            embed.set_image(url=random.choice(urls))
            await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        else:
            image = _local_fallback_gif(action)
            filename = "zyven-fallback.gif"
            embed.set_image(url=f"attachment://{filename}")
            await ctx.send(embed=embed, file=discord.File(image, filename=filename), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="fun", help="Show the fun games and roleplay commands.")
    async def fun_help(self, ctx):
        await ctx.send(view=component_card(panel(
            "Fun Commands",
            "Use `fun <member>` for playful scores, `8ball <question>`, `truth`, `dare`, `gif`, `image`, or `weather <city>`.\n\n"
            "Roleplay commands include `hug`, `kiss`, `pat`, `cuddle`, `slap`, `tickle`, `feed`, "
            "`bite`, `bonk`, `cry`, `dance`, `highfive`, `poke`, `punch`, `smash`, `wink`, and more."
        )), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="8ball", aliases=("8b",), help="Ask the magic 8-ball a question.")
    async def eight_ball(self, ctx, *, question: str):
        answers = ("It is certain.", "Signs point to yes.", "Without a doubt.", "Ask again later.", "Cannot predict that now.", "Don't count on it.", "My reply is no.")
        await ctx.send(embed=discord.Embed(title="🎱 Magic 8-Ball", description=f"**{discord.utils.escape_markdown(question)}**\n\n{random.choice(answers)}", color=0x5865F2))

    @commands.command(name="truth", help="Get a random truth question.")
    async def truth(self, ctx):
        prompts = ("What is something you are secretly proud of?", "What is the funniest thing you believed as a kid?", "What is one song you know by heart?", "What is your most harmless guilty pleasure?")
        await ctx.send(embed=discord.Embed(title="🗣️ Truth", description=random.choice(prompts), color=0x5865F2))

    @commands.command(name="dare", help="Get a safe, random dare.")
    async def dare(self, ctx):
        prompts = ("Talk like a pirate for your next three messages.", "Share a wholesome meme with the chat.", "Describe your day using only three emojis.", "Give someone in the chat a genuine compliment.")
        await ctx.send(embed=discord.Embed(title="🎯 Dare", description=random.choice(prompts), color=0x5865F2))

    @commands.command(name="fakeban", help="Send a clearly labeled fake ban prank; no moderation action is taken.")
    async def fakeban(self, ctx, member: discord.Member):
        await ctx.send(embed=discord.Embed(title="🔨 Fake Ban Prank", description=f"{member.mention} has been **fake banned**. This is only a joke; no action was taken.", color=0x5865F2), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="gif", help="Search for a GIF, or get a random one with no query.")
    @commands.cooldown(1, 2, commands.BucketType.user)
    async def gif(self, ctx, *, query: str | None = None):
        category = random.choice(tuple(set(ROLEPLAY_ACTIONS.values())))
        url = "https://nekos.best/api/v2/search" if query else f"https://nekos.best/api/v2/{category}"
        params = {"query": query, "type": 2} if query else None
        cache_key = f"search:{query.casefold()}" if query else f"random:{category}"
        async with ctx.typing():
            urls = await self._gif_results(cache_key, api_url=url, params=params, fallback_query=f"{query or 'anime ' + category} animated gif")
        title = f"Random GIF for '{discord.utils.escape_markdown(query)}'" if query else "Random GIF"
        embed = discord.Embed(title=title, color=0x20D65A)
        if urls:
            embed.set_image(url=random.choice(urls))
            await ctx.send(embed=embed)
        else:
            image = _local_fallback_gif(query or category)
            filename = "zyven-fallback.gif"
            embed.set_image(url=f"attachment://{filename}")
            await ctx.send(embed=embed, file=discord.File(image, filename=filename))

    @commands.hybrid_command(name="image", aliases=("img",), with_app_command=True, help="Search for an image in an age-restricted channel.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def image(self, ctx, *, query: str | None = None):
        if not query or not query.strip():
            help_command = self.bot.get_command("help")
            if help_command:
                await ctx.invoke(help_command, query="image")
            else:
                embed = discord.Embed(
                    title="Image",
                    description=f"Search for an image. Usage: `{settings.prefix}image <query>` (18+ channel only).",
                    color=0x5865F2,
                )
                await ctx.send(embed=embed)
            return
        channel = ctx.channel
        is_nsfw = getattr(channel, "is_nsfw", None)
        if not callable(is_nsfw) and getattr(channel, "parent", None):
            is_nsfw = getattr(channel.parent, "is_nsfw", None)
        if not callable(is_nsfw) or not is_nsfw():
            embed = discord.Embed(
                title="18+ Channel Required",
                description="The `image` command can only be used in an age-restricted (18+) channel.",
                color=0xED4245,
            )
            kwargs = {"ephemeral": True} if ctx.interaction else {}
            await ctx.reply(embed=embed, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
            return
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer(ephemeral=True)
        adult_terms = {
            "porn", "porno", "pornographic", "nsfw", "hentai", "nude", "nudes", "naked",
            "sex", "xxx", "explicit", "erotic", "boobs", "tits", "pussy", "penis", "dick",
            "vagina", "anal", "cum", "masturbation", "blowjob",
        }
        query_terms = set(re.findall(r"[a-z0-9_]+", query.lower()))
        is_adult_search = bool(adult_terms & query_terms)
        if is_adult_search and query_terms & {"loli", "shota", "child", "underage", "minor", "young"}:
            embed = discord.Embed(
                title="Search Not Allowed",
                description="I can't search for sexual content involving minors or young-looking characters.",
                color=0xED4245,
            )
            kwargs = {"ephemeral": True} if ctx.interaction else {}
            await ctx.reply(embed=embed, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
            return
        try:
            async with ctx.typing():
                results = await self._cached_search(query, safe="off" if is_adult_search else "on")
            if not results:
                embed = discord.Embed(title="No Images Found", description=f"No images found for **{discord.utils.escape_markdown(query)}**.", color=0xED4245)
                kwargs = {"ephemeral": True} if ctx.interaction else {}
                await ctx.reply(embed=embed, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
                return
            image_url = random.choice(results)
            title = f"Random Image for '{discord.utils.escape_markdown(query)}'"
            embed = discord.Embed(title=title, color=0xFF1688 if is_adult_search else 0x5865F2)
            embed.set_image(url=image_url)
            kwargs = {"ephemeral": True} if ctx.interaction else {}
            await ctx.reply(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        except Exception:
            log.warning("No-key image search failed for query %r; the image provider may be unavailable", query)
            embed = discord.Embed(title="Image Search Failed", description="Image search is temporarily unavailable. Please try again in a moment.", color=0xED4245)
            kwargs = {"ephemeral": True} if ctx.interaction else {}
            await ctx.reply(embed=embed, allowed_mentions=discord.AllowedMentions.none(), **kwargs)

    @commands.command(name="weather", help="Show current weather for a city.")
    async def weather(self, ctx, *, city: str):
        timeout = aiohttp.ClientTimeout(total=12)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1, "language": "en", "format": "json"}) as response:
                    response.raise_for_status()
                    place = (await response.json()).get("results", [])[0]
                async with session.get("https://api.open-meteo.com/v1/forecast", params={"latitude": place["latitude"], "longitude": place["longitude"], "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code", "timezone": "auto"}) as response:
                    response.raise_for_status()
                    current = (await response.json())["current"]
            embed = discord.Embed(title=f"🌤️ Weather • {place['name']}, {place.get('country', '')}", description=f"Temperature: **{current['temperature_2m']}°C**\nFeels like: **{current['apparent_temperature']}°C**\nHumidity: **{current['relative_humidity_2m']}%**", color=0x5865F2)
            await ctx.send(embed=embed)
        except (aiohttp.ClientError, TimeoutError, KeyError, IndexError, TypeError, ValueError):
            await ctx.send(view=component_card(panel(
                "Weather Lookup Failed",
                "I couldn't find weather for that city right now. Check the city name and try again.",
                color=0xED4245,
            )))


async def setup(bot):
    await bot.add_cog(Fun(bot))
