"""Shared-login authentication: signed session cookie, CSRF token, login throttling, logout.

The session is a stateless token signed with HMAC-SHA256: base64url(payload).base64url(mac).
The signing key is derived from DASHBOARD_SESSION_SECRET *and* the configured username/password,
so changing the password (or the secret) invalidates every session. Logout additionally stores
the session id as revoked until it would have expired.

Cookie: __Host- prefix (forces Secure, Path=/, no Domain), HttpOnly, SameSite=Strict.
Every state-changing request needs the X-CSRF-Token header that /api/session hands out (it is
bound to the session) and, when the browser sends one, an Origin equal to the dashboard's own.
"""
import base64
import hashlib
import hmac
import json
import secrets

COOKIE = "__Host-enshrouded_session"
SESSION_SECONDS = 12 * 3600
THROTTLE_WINDOW = 15 * 60
MAX_FAILURES_PER_IP = 5
MAX_FAILURES_TOTAL = 30  # caps distributed guessing across all addresses


class Config:
    def __init__(self, username, password, secret):
        if not (username and password and secret):
            raise ValueError("DASHBOARD_USERNAME, DASHBOARD_PASSWORD and DASHBOARD_SESSION_SECRET must be set")
        self.username = username
        self.password = password
        self.key = hmac.new(secret.encode(), b"session\0" + username.encode() + b"\0" + password.encode(),
                            hashlib.sha256).digest()


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def check_credentials(cfg, username, password):
    # compare_digest on both, always, so timing doesn't reveal which one was wrong
    ok_user = hmac.compare_digest((username or "").encode(), cfg.username.encode())
    ok_pass = hmac.compare_digest((password or "").encode(), cfg.password.encode())
    return ok_user and ok_pass


def issue(cfg, now):
    payload = {"sid": secrets.token_urlsafe(16), "csrf": secrets.token_urlsafe(24), "exp": int(now + SESSION_SECONDS)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = _b64(hmac.new(cfg.key, body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}", payload


def verify(cfg, token, now, store):
    """The session payload, or None if missing, tampered, expired or logged out."""
    if not token or token.count(".") != 1:
        return None
    body, mac = token.split(".")
    want = _b64(hmac.new(cfg.key, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, want):
        return None
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) <= now:
        return None
    if store.get("SESSION", payload.get("sid", "")) is not None:
        return None  # logged out
    return payload


def revoke(store, session):
    store.put({"pk": "SESSION", "sk": session["sid"], "revoked": True, "ttl": int(session["exp"]) + 60})


def session_cookie(token):
    return f"{COOKIE}={token}; Path=/; Max-Age={SESSION_SECONDS}; Secure; HttpOnly; SameSite=Strict"


def clear_cookie():
    return f"{COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"


def token_from_cookies(cookies):
    for c in cookies or []:
        name, _, value = c.strip().partition("=")
        if name == COOKIE:
            return value
    return None


def csrf_ok(session, header_token, origin, own_origin):
    if origin and origin != own_origin:
        return False
    return bool(header_token) and hmac.compare_digest(header_token, session.get("csrf", ""))


# --- Throttling --------------------------------------------------------------------------

def _window(now):
    return int(now // THROTTLE_WINDOW)


def throttled(store, ip, now):
    """Seconds until login may be retried, or 0."""
    w = _window(now)
    per_ip = (store.get("THROTTLE", f"ip#{ip}#{w}") or {}).get("count", 0)
    total = (store.get("THROTTLE", f"all#{w}") or {}).get("count", 0)
    if per_ip >= MAX_FAILURES_PER_IP or total >= MAX_FAILURES_TOTAL:
        return int((w + 1) * THROTTLE_WINDOW - now) + 1
    return 0


def record_failure(store, ip, now):
    w = _window(now)
    ttl = (w + 2) * THROTTLE_WINDOW
    store.increment("THROTTLE", f"ip#{ip}#{w}", ttl)
    store.increment("THROTTLE", f"all#{w}", ttl)
