# Zyven

Python Discord server bot built from the supplied Discord screenshots. Local SQLite handles runtime reads and writes for low latency; a durable SQLite outbox syncs changes to MongoDB in the background for recovery and host migration. Discord Components V2 cards keep text, buttons, and select menus together inside the same bordered container. Components V2 requires the minimum `discord.py` version shown in `requirements.txt`.

## UI and cogs

- `/config` opens the category panel for Security, AutoMod, Member Setup, and VoiceMaster.
- `?help` opens the screenshot-style Zyven help card with 20 emoji categories and a module selector. Selecting a category lists the commands currently assigned to that module; slash commands are shown as plain command names without slash notation. Prefix shortcuts include `?config`/`?setup`, `?security`, `?security status`, `?automod`, `?antinuke`/`?antinukesetup`, and `?whitelist`/`?wl` with add, remove, config, show/list, and reset/clear actions. `?h` is a case-insensitive help shortcut, and `?help whitelist` opens the Antinuke command category.
- `/antinuke` opens the compact screenshot-style `Configure Antinuke` card with Enable, Disable, and Back. The `/config` → Security → Antinuke panel also exposes Event Filters, per-event limits/responses, a confirmation-based reset, and Auto Recovery. Event filters respect Discord's 25-option menu cap; the mobile-friendly limit summary shows five events per page.
- Channel and role selects save settings immediately. The welcome message editor is a modal. Clear actions ask for confirmation before removing saved settings.
- Command responses use Components V2 cards with the `Powered By Zyven Development™` credit. The help card uses the configured Zyven invite and support server links.
- Command permissions are checked per command for ordinary users. Configure one or more bot owners with comma-separated `OWNER_IDS`; `OWNER_ID` remains supported as the primary-owner fallback. All configured owners receive owner-command and permission-bypass access. The configured `OWNER_ID` bypasses user-side Discord permission checks on normal commands, while Discord still requires the bot account itself to have permissions for actions such as deleting messages or managing roles. Security/Antinuke menus are limited to the server owner and configured Extraowners; only the server owner can enable Antinuke or change the Extraowner list.
- Command result panels, permission/validation notices, and event-log cards use the shared clean Components V2 layout without a colored accent border. New interaction commands should send panels with `send_card(interaction, panel(...))`; prefix commands should use `ctx.send(view=component_card(panel(...)))` so new features keep the same style.
- Music supports `?play <song or URL>`, `?search <query>` with a platform picker, queue, pause/resume, previous/skip, stop, join/leave, volume, loop, shuffle, and autoplay. Playback uses the configured Lavalink node through Wavelink. Set `LAVALINK_HOST`, `LAVALINK_PORT`, `LAVALINK_AUTH`, `LAVALINK_SECURE`, and `LAVALINK_NODE_NAME` in `.env`. YouTube and SoundCloud use Lavalink's built-in sources; Spotify, Apple Music, Deezer, and Yandex require matching source plugins on the node, and JioSaavn requires its Lavalink plugin. Search options without a supplied custom emoji are shown without one.
- `?image <query>` uses keyless image search and still requires an age-restricted channel. Search results are cached briefly; GIF commands reuse a single HTTP session, cache results, and fall back to GIF image search if their primary source fails. No Google API key or search-engine ID is needed.
- Slash and prefix command failures are returned as clean Components V2 cards, including unknown commands, missing permissions, invalid arguments, and Discord/API errors.
- Cogs: `configuration`, `security`, `automod`, `members`, `voice`, `music`, `tickets`, `moderation`, `information`, `helpdesk`, and `eventlogs`.
- Emoji tokens are centralized in `utils/emojis.py`; application emoji IDs are refreshed at startup, and optional `CUSTOM_EMOJIS_JSON` values can override defaults.
- On first ready, the bot syncs every configured asset in `utils/emojis.py` to the Discord application's emoji collection. It keeps matching images and uploads or replaces changed assets, including the screenshot's member/status/statistics emojis. Uploads use Discord's application emoji API with image data fetched from the configured source IDs; this is separate from server emojis and requires the bot application's API access.
- The Security and Antinuke menus and related security commands are limited to the server owner and configured Extraowners. Antinuke can only be enabled by the server owner; other unauthorized users receive a clean Components V2 `Access Denied` card.
- Optional custom emojis can be mapped without editing cogs using `CUSTOM_EMOJIS_JSON`, for example `{"security":"123456789012345678","success":"<:checkmark:123456789012345678>"}`. A numeric value uses the mapping key as the Discord emoji name; full static/animated markup and Unicode overrides are also accepted. Invalid custom markup falls back to the built-in emoji. The bot needs access to the custom emoji's server to display it.
- Utility statistics use the supplied screenshot emojis: `?users` shows bot-wide user/server/shard counts, `?mc` shows server member and presence counts, and `?bc` shows boost information. Serverinfo and userinfo cards use matching emoji headings and permission ticks.

## Included features

- MongoDB-backed guild configuration, warnings, reminders, cooldowns, leaderboards, and other bot state.
- Anti-link deletion (including `www.` links, bare domains, and Discord invites), Discord invite filtering, and mass-mention filtering for user mentions, role mentions, and `@everyone` on both new and edited messages; rate-based anti-spam uses configurable thresholds and counts new messages.
- Antinuke monitors member bans/unbans/kicks/prunes, bot additions, channel/role/emoji/sticker/integration/webhook changes, server setting changes, member nickname/role changes, and `@everyone`/role pings in both new and edited messages. New event settings default to **3 actions in 10 seconds** with **Log only** response; server owners and explicitly trusted users are exempt. Server owners can select event types, configure per-event thresholds, and change executor response to Log only, Kick, or Ban. Keep trusted moderators in the server's allowlist.
- Auto Recovery is enabled with Antinuke by default and stores snapshots of unmanaged roles and category, text, voice, stage, forum, and media channels in MongoDB. Forum snapshots include available tags and channel defaults. It can recreate deleted roles/channels, restore saved channel overrides, remove newly created channels/roles, restore unauthorized member and emoji changes, and revert channel/role updates after that event's configured antinuke threshold is reached. It can be turned off from the Antinuke panel.
- Trusted extraowners, a configurable security log channel, and an adjustable anti-raid threshold that sends a review alert when joins spike; it does not automatically punish or lock out new members.
- AutoMod exemptions can be assigned to individual members with `/whitelist` or to whole roles with `/whitelistrole` (`@everyone` cannot be exempted).
- Antinuke whitelist supports whole-member trust with `?whitelist add/remove`, plus per-member event exemptions with `?whitelist config @member`. The configuration selector updates real event exemptions immediately; `Grant All Permissions` exempts the target from all listed event types.
- `/ban`, `/unban`, `/kick`, `/timeout`, `/untimeout`, `/purge`, `/slowmode`, `/lock`, `/unlock`, `/nickname`, and `/role` moderation commands.
- Persistent `/warn`, `/warnings`, `/unwarn`, and `/clearwarnings` moderation history stored in MongoDB. Warning cases have IDs for targeted removal.
- `/purge` can delete recent messages generally, from one selected member, or from bots only.
- Channel `/lock` and `/unlock` restore the previous `@everyone` send-message override (allow, deny, or inherit) instead of always resetting it to the server default. Failed lock API calls remove a newly created restore record.
- Event logs for message edits/deletions, member joins/leaves/bans, nickname/role changes, and channel/role/server/emoji updates.
- Bulk message deletions are summarized in the configured log channel. Log-channel setup checks the bot can send embeds there, and autorole selection checks role hierarchy in both setup UI and slash command.
- `/ping` reports Discord gateway and MongoDB response time; `/botinfo` has interactive System, General, Team Info, and Partner Servers tabs, alongside `/serverinfo`, `/userinfo`, `/avatar`, `/roleinfo`, and a category-based `/help` panel.
- The bot owner can manage the Team Info tab with `.team developers add @user`, `.team developers remove @user`, `.team core add @user`, and `.team core remove @user`. Team members are stored in MongoDB; their displayed names link to their Discord profiles.
- Utility commands include invite, joined-at, GitHub, voice/channel information, badges, banner, permissions, reminders, timers, QR generation, UPI QR setup, and Prime information. Set `GITHUB_URL` to show the source link. Media-only channels use `?media setup [#channel]`, `?media config`, `?media remove`, and `?media bypass add/remove/show`; Zyven needs Manage Messages in the configured channel.
- Reminders and timers are stored in MongoDB and resume after a bot restart. They can last up to 30 days; reminders are sent by DM when possible, with a server-channel fallback.
- The configured log channel receives moderation and server activity embeds.
- Welcome message with `{user}`, `{server}`, and `{count}` placeholders (up to 1000 characters); autorole for new members. If a temporary VoiceMaster room is manually deleted, its persisted owner/tracking record is removed as well.
- VoiceMaster join-to-create temporary voice rooms, with `/voiceclaim`, `/voiceowner`, `/voicename`, `/voicelimit`, `/voicelock`, `/voiceunlock`, `/voicepermit`, `/voicedeny`, and `/voicekick` controls. Use `?voicemaster setup` to choose automatic channel creation or custom interface/trigger channels, then select the interface buttons; `?voicemaster buttons` edits those buttons, `?voicemaster config` shows the selected channels and active room count, and `?voicemaster reset` clears setup. The persistent control panel is posted in the selected interface channel and each newly created temporary voice channel.
- Voice lock/unlock remembers and restores the room owner's and manager's previous Connect overrides; temporary room ownership data is removed when a room is deleted.
- Moderation actions remain confirmed even if best-effort log delivery fails; event-log and AutoMod/antinuke delivery failures are recorded in runtime logs without crashing their listeners.
- Autorole setup checks that the role is assignable by both the configuring member and the bot.

## Setup

1. Install Python 3.10 or newer, or use the Docker setup below.
2. Create a Discord application and bot. Enable **Server Members Intent** and **Message Content Intent** in the Developer Portal.
3. Copy `.env.example` to `.env`, set `DISCORD_TOKEN`, and configure `MONGODB_URI` plus `MONGODB_DATABASE`. SQLite is used for fast bot operation. MongoDB is the remote recovery copy and syncs in the background.
4. Invite the bot with the `bot` and `applications.commands` scopes. It needs View Channels, Send Messages, Embed Links, Read Message History, View Audit Log, Manage Messages, Manage Channels, Manage Roles, Manage Nicknames, Kick Members, Ban Members, Moderate Members, and Move Members for the listed features. Keep the bot role above roles it assigns.
5. Install and run:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python main.py
   ```

### Dependency and image updates

`requirements.txt` pins the direct dependencies to the versions resolved in this review environment. A release maintainer should refresh the complete transitive lock (including hashes) in a clean environment rather than guessing versions or hashes:

```bash
python -m venv .venv-update
. .venv-update/bin/activate
python -m pip install --upgrade pip pip-tools
cp requirements.txt /tmp/zyven-requirements.in
# Edit /tmp/zyven-requirements.in to the intended compatible direct versions.
pip-compile --generate-hashes --output-file requirements.txt /tmp/zyven-requirements.in
```

The Dockerfile pins the declared `python:3.12-slim` base tag to an official registry digest. Refresh that digest from the official registry with `docker buildx imagetools inspect python:3.12-slim`, review the platform manifest, then rebuild and rerun the repository validation checks.

On launch, Zyven uses `SQLITE_PATH` as its local runtime store. If that file has no bot documents, existing MongoDB collections are restored into it automatically. Local changes are written to a durable SQLite outbox in the same transaction, then asynchronously mirrored to MongoDB. Temporary MongoDB outages do not block commands; queued writes retry automatically. Keep `data/` on a persistent volume when the host supports it. If the host disk is replaced, MongoDB restores the last synced state; changes still waiting in the old host's outbox cannot travel with a lost disk.

Slash commands sync globally on startup and may take time to appear. For faster development, set `SYNC_GUILD_ID` to a test server ID; leave it blank for global sync. Grant Manage Roles above the autorole role, Manage Channels for VoiceMaster, and the applicable moderation permissions.

VoiceMaster needs the bot's **Manage Channels** and **Move Members** permissions plus **View Channel** and **Connect** in the destination category (or server-wide when rooms are created outside categories). Welcome and log channel setup checks the relevant send permissions before saving.

AutoMod settings are managed from the AutoMod commands; the spam threshold supports 3–20 messages over 2–30 seconds, and the mention threshold supports 2–20 mentions. AutoMod exemptions use `/whitelist` and `/whitelistrole`.

The prefix AutoMod command group is `?automod`. Use `?automod enable` to select any of seven filters (spam, caps, links, invites, mass mentions, emoji spam, and NSFW links); `?automod config` to review status, limits, punishment, and logging; `?automod punishment` to assign mute, kick, or ban to selected events; `?automod logging #channel` to configure logs; and `?automod ignore role/channel`, `?automod unignore role/channel`, `?automod ignore show`, or `?automod ignore reset` to manage exemptions. Limits are configurable with `?automod limit spam <count> <seconds>` and `?automod limit mentions <count>`. The `/automod` command exposes filter switches.

### Stored data and logs

MongoDB stores per-server settings, AutoMod exemptions, warning records, scheduled Utility reminders/timers, temporary voice-room IDs/owners, and prior channel/voice permission overrides needed by unlock commands. Warning records remain until a moderator removes a case with `/unwarn` or clears a member's history with `/clearwarnings`. Zyven does not archive messages in MongoDB; when event logging is configured, deleted/edited message content may be posted to the selected Discord log channel. Restrict access to that channel to trusted moderators.

### Docker Compose

1. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`.
2. Run `docker compose up -d --build` from this folder. Bot data lives in MongoDB; `docker compose logs -f bot` shows bot logs.
3. To stop the bot, run `docker compose down`. MongoDB keeps settings and warnings across restarts and host changes; a local `data/` folder is only needed as a one-time import source for an old SQLite database.

## Branding

- Name: Zyven
- Development: Zyven Development™
- Copyright notice: Soul (see `LICENSE` and `NOTICE.md`)
- Owner ID: `1498085359458717747`
- Support server: https://discord.gg/gm6pAGpNGu
- Application ID: 1542942496462737599
- Bot invite: https://discord.com/oauth2/authorize?client_id=1542942496462737599

## Source and data backups

Set `SQLITE_PATH` to the local runtime database path, then set `MONGODB_URI` and `MONGODB_DATABASE` for remote mirroring. Commands do not wait for MongoDB on each read or write. Pending mirror changes are stored in SQLite and retried when MongoDB returns. `BACKUP_WEBHOOK_URL` remains optional for source-code archives only.

Never commit `.env` or share the bot token. The supplied invite URL has no scopes or permissions in its query, so configure them in the Discord Developer Portal before inviting the bot.

