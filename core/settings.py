from dataclasses import dataclass
import asyncio
import ipaddress
import os
import socket
from urllib.parse import urljoin, urlsplit

from dotenv import load_dotenv

load_dotenv()

_APPROVED_DISCORD_CDN_HOSTS = frozenset({
    "cdn.discordapp.com",
    "media.discordapp.net",
    "images-ext-1.discordapp.net",
    "images-ext-2.discordapp.net",
})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_BLOCKED_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "100.64.0.0/10",       # Carrier-grade NAT
    "169.254.0.0/16",      # Link-local, including cloud metadata services
    "192.0.0.0/24",        # IETF protocol assignments
    "198.18.0.0/15",       # Benchmarking networks
))


def _public_ip(address: str) -> bool:
    """Return whether an address is safe for an outbound untrusted fetch."""
    ip = ipaddress.ip_address(address)
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    prohibited = (
        ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )
    return not prohibited and ip.is_global and not any(ip in network for network in _BLOCKED_NETWORKS)


def _lavalink_loopback(host: str) -> bool:
    normalized = host.strip().strip("[]").rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


async def validate_remote_url(url: str) -> str:
    """Validate an HTTPS URL and all resolved addresses before an outbound request."""
    parts = urlsplit(str(url).strip())
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme.lower() != "https" or not host or parts.username or parts.password or parts.fragment:
        raise ValueError("Only credential-free HTTPS image URLs are allowed.")
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("The image URL has an invalid port.") from error
    if port not in (None, 443):
        raise ValueError("Only HTTPS port 443 is allowed.")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            resolved = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                443,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
            )
        except (OSError, socket.gaierror) as error:
            raise ValueError("The image host could not be resolved safely.") from error
        addresses = {item[4][0] for item in resolved}
        if not addresses or any(not _public_ip(address) for address in addresses):
            raise ValueError("The image host resolves to a prohibited network.")
    else:
        if not _public_ip(str(literal)):
            raise ValueError("The image host resolves to a prohibited network.")
    # Keep the approved set explicit for auditing while allowing other HTTPS
    # hosts only after the same public-address checks above.
    if host in _APPROVED_DISCORD_CDN_HOSTS:
        return parts.geturl()
    return parts.geturl()


async def safe_request(session, url: str, *, max_redirects: int = 3):
    """Perform a request with validation and DNS checks on every redirect."""
    current = str(url)
    for _ in range(max_redirects + 1):
        current = await validate_remote_url(current)
        response = await session.get(current, allow_redirects=False)
        if response.status not in _REDIRECT_STATUSES:
            return response
        location = response.headers.get("Location")
        response.release()
        if not location:
            raise ValueError("The image host returned an invalid redirect.")
        current = urljoin(current, location)
    raise ValueError("The image URL followed too many redirects.")

def _env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else raw.strip()
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer; got {value!r}") from exc
    if (minimum is not None and parsed < minimum) or (maximum is not None and parsed > maximum):
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}; got {parsed}")
    return parsed


def _env_owner_ids() -> frozenset[int]:
    """Read comma-separated OWNER_IDS, retaining OWNER_ID compatibility."""
    raw = os.getenv("OWNER_IDS", "").strip() or os.getenv("OWNER_ID", "1498085359458717747")
    values = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError as exc:
            raise RuntimeError(f"OWNER_IDS must contain only numeric Discord user IDs; got {item!r}") from exc
        if value < 1:
            raise RuntimeError(f"OWNER_IDS must contain positive Discord user IDs; got {value}")
        values.append(value)
    if not values:
        raise RuntimeError("OWNER_IDS must contain at least one Discord user ID")
    return frozenset(values)


@dataclass(frozen=True)
class Settings:
    token: str = os.getenv("DISCORD_TOKEN", "")
    # SQLite is the fast local runtime store; MongoDB mirrors changes in background.
    sqlite_path: str = os.getenv("SQLITE_PATH", "data/zyven.sqlite3")
    mongo_uri: str = os.getenv("MONGODB_BACKUP_URI", os.getenv("MONGODB_URI", "")).strip()
    mongo_database: str = os.getenv("MONGODB_BACKUP_DATABASE", os.getenv("MONGODB_DATABASE", "zyven_dev")).strip()
    owner_id: int = _env_int("OWNER_ID", 1498085359458717747, minimum=1)
    owner_ids: frozenset[int] = _env_owner_ids()
    application_id: int = _env_int("APPLICATION_ID", 1542942496462737599, minimum=1)
    owner_log_webhook_url: str = os.getenv("OWNER_LOG_WEBHOOK_URL", "").strip()
    owner_alert_p95_ms: int = _env_int("OWNER_ALERT_P95_MS", 1000, minimum=100, maximum=60000)
    owner_alert_rate_limit_cooldown_seconds: int = _env_int("OWNER_ALERT_RATE_LIMIT_COOLDOWN_SECONDS", 300, minimum=30, maximum=86400)
    owner_alert_p95_cooldown_seconds: int = _env_int("OWNER_ALERT_P95_COOLDOWN_SECONDS", 300, minimum=30, maximum=86400)
    owner_alert_p95_min_samples: int = _env_int("OWNER_ALERT_P95_MIN_SAMPLES", 20, minimum=5, maximum=500)
    backup_webhook_url: str = os.getenv("BACKUP_WEBHOOK_URL", "").strip()
    backup_interval_hours: int = _env_int("BACKUP_INTERVAL_HOURS", 24, minimum=1, maximum=8760)
    support_url: str = os.getenv("SUPPORT_SERVER_URL", "https://discord.gg/gm6pAGpNGu")
    invite_url: str = os.getenv("BOT_INVITE_URL", "https://discord.com/oauth2/authorize?client_id=1542942496462737599")
    github_url: str = os.getenv("GITHUB_URL", "")
    lavalink_host: str = os.getenv("LAVALINK_HOST", "").strip()
    lavalink_port: int = _env_int("LAVALINK_PORT", 2333, minimum=1, maximum=65535)
    lavalink_auth: str = os.getenv("LAVALINK_AUTH", "").strip()
    lavalink_secure: bool = os.getenv("LAVALINK_SECURE", "true").strip().lower() in {"1", "true", "yes", "on"}
    lavalink_node_name: str = os.getenv("LAVALINK_NODE_NAME", "Zyven")
    prefix: str = os.getenv("COMMAND_PREFIX", "?")
    sync_guild_id: int | None = (_env_int("SYNC_GUILD_ID", 0, minimum=0) or None)

    def is_owner(self, user_id: int | None) -> bool:
        try:
            return int(user_id or 0) in self.owner_ids
        except (TypeError, ValueError, OverflowError):
            return False

    def __post_init__(self):
        if self.lavalink_auth and self.lavalink_host and not self.lavalink_secure and not _lavalink_loopback(self.lavalink_host):
            raise RuntimeError("LAVALINK_SECURE must be enabled when Lavalink credentials leave the local host.")


settings = Settings()
