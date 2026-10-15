"""Known-device tracking for new-login alerts.

A fingerprint is SHA-256(user-agent + coarse IP). Coarse IP means the /24
(IPv4) or /48 (IPv6): DHCP churn on the same network doesn't spam alerts,
while a genuinely new network or device does. First sighting of a
fingerprint for a user raises a `new_device` security alert and, when SMTP
is configured, emails the account owner.
"""
import hashlib
import ipaddress

from db import db
from lib.alerts import raise_alert
from lib.base import new_id, now_iso
from lib.mailer import send_email


def _coarse_ip(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip)
        if isinstance(addr, ipaddress.IPv4Address):
            return str(ipaddress.ip_network(f"{ip}/24", strict=False).network_address)
        return str(ipaddress.ip_network(f"{ip}/48", strict=False).network_address)
    except ValueError:
        return "unknown"


def fingerprint(request) -> str:
    ua = request.headers.get("user-agent", "")
    ip = request.client.host if request.client else "unknown"
    raw = f"{ua}|{_coarse_ip(ip)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def check_device(user: dict, request) -> bool:
    """Record this login's device. Returns True when it's a new device."""
    fp = fingerprint(request)
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "")[:200]
    known = await db.devices.find_one({"user_id": user["id"], "fp": fp})
    if known:
        await db.devices.update_one(
            {"user_id": user["id"], "fp": fp},
            {"$set": {"last_seen": now_iso(), "ip": ip}})
        return False
    await db.devices.insert_one({
        "id": new_id(), "user_id": user["id"], "tenant_id": user["tenant_id"],
        "fp": fp, "ua": ua, "ip": ip,
        "first_seen": now_iso(), "last_seen": now_iso(),
    })
    title = "New device signed in"
    detail = f"{user.get('email')} signed in from an unrecognized device/network (IP {ip})."
    await raise_alert(user["tenant_id"], user["id"], "new_device", title, detail)
    # Best-effort email; the in-app alert always fires.
    await send_email(user.get("email", ""),
                     f"[Security] {title}",
                     f"{detail}\n\nIf this was you, no action is needed. "
                     f"If not, sign out all devices in Settings → Active sessions "
                     f"and change your password immediately.")
    return True
