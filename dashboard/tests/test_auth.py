import json
import unittest

from fakes import ENV, HOST, Browser, body, event, make_app

import auth

ADMIN_ENDPOINTS = [
    ("GET", "/api/status"), ("GET", "/api/settings"), ("GET", "/api/schedules"), ("GET", "/api/backups"),
    ("GET", "/api/backups/20261015T190000Z-manual"), ("GET", "/api/backups/20261015T190000Z-manual/download"),
    ("GET", "/api/logs"), ("GET", "/api/operations/x"),
    ("POST", "/api/server/start"), ("POST", "/api/server/stop"), ("POST", "/api/backups"),
    ("POST", "/api/schedules/stop/skip"),
    ("POST", "/api/schedules/stop/unskip"), ("POST", "/api/logout"),
]
GOOD = {"username": ENV["DASHBOARD_USERNAME"], "password": ENV["DASHBOARD_PASSWORD"]}


class Unauthenticated(unittest.TestCase):
    def setUp(self):
        self.app, self.cloud, self.store, self.clock = make_app()

    def test_every_admin_endpoint_rejects_without_session(self):
        for method, path in ADMIN_ENDPOINTS:
            resp = self.app.http(event(method, path, {"confirm": "stop", "enabled": False}))
            self.assertEqual(resp["statusCode"], 401, f"{method} {path}")
        # Nothing reached AWS: no start/stop/backup/schedule change
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(self.cloud.async_payloads, [])

    def test_page_and_assets_are_public(self):
        for path in ("/", "/app.js", "/styles.css", "/favicon.svg"):
            resp = self.app.http(event("GET", path))
            self.assertEqual(resp["statusCode"], 200, path)
            self.assertIn("Content-Security-Policy", resp["headers"])

    def test_session_endpoint_says_logged_out(self):
        self.assertEqual(self.app.http(event("GET", "/api/session"))["statusCode"], 401)

    def test_not_configured_fails_closed(self):
        app, cloud, *_ = make_app(env={"DASHBOARD_USERNAME": "x"})
        self.assertEqual(app.http(event("POST", "/api/login", GOOD))["statusCode"], 503)
        self.assertEqual(app.http(event("GET", "/api/status"))["statusCode"], 503)
        self.assertEqual(app.http(event("GET", "/"))["statusCode"], 200)


class Login(unittest.TestCase):
    def setUp(self):
        self.app, self.cloud, self.store, self.clock = make_app()

    def test_cookie_attributes_and_no_secrets(self):
        resp = self.app.http(event("POST", "/api/login", GOOD))
        self.assertEqual(resp["statusCode"], 200)
        cookie = resp["cookies"][0]
        self.assertTrue(cookie.startswith("__Host-enshrouded_session="))
        for attr in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attr, cookie)
        self.assertNotIn("Domain", cookie)
        text = json.dumps(resp)
        self.assertNotIn(ENV["DASHBOARD_PASSWORD"], text)
        self.assertNotIn(ENV["DASHBOARD_SESSION_SECRET"], text)
        self.assertIn("csrf", body(resp))

    def test_wrong_credentials(self):
        for creds in ({"username": "friends", "password": "nope"}, {"username": "nope", "password": GOOD["password"]}, {}):
            self.assertEqual(self.app.http(event("POST", "/api/login", creds))["statusCode"], 401)

    def test_throttling_per_ip(self):
        for _ in range(auth.MAX_FAILURES_PER_IP):
            self.app.http(event("POST", "/api/login", {"username": "friends", "password": "x"}))
        resp = self.app.http(event("POST", "/api/login", GOOD))  # right password, still blocked
        self.assertEqual(resp["statusCode"], 429)
        self.assertIn("Retry-After", resp["headers"])
        self.assertEqual(self.app.http(event("POST", "/api/login", GOOD, ip="192.0.2.99"))["statusCode"], 200)
        self.clock.sleep(auth.THROTTLE_WINDOW)
        self.assertEqual(self.app.http(event("POST", "/api/login", GOOD))["statusCode"], 200)

    def test_throttling_global(self):
        for i in range(auth.MAX_FAILURES_TOTAL):
            self.app.http(event("POST", "/api/login", {"username": "x", "password": "x"}, ip=f"192.0.2.{i}"))
        self.assertEqual(self.app.http(event("POST", "/api/login", GOOD, ip="203.0.113.200"))["statusCode"], 429)

    def test_cross_origin_login_blocked(self):
        resp = self.app.http(event("POST", "/api/login", GOOD, headers={"origin": "https://evil.example"}))
        self.assertEqual(resp["statusCode"], 403)


class Sessions(unittest.TestCase):
    def setUp(self):
        self.app, self.cloud, self.store, self.clock = make_app()
        self.b = Browser(self.app)

    def test_session_works(self):
        resp = self.b.get("/api/session")
        self.assertEqual(body(resp)["csrf"], self.b.csrf)
        self.assertEqual(self.b.get("/api/status")["statusCode"], 200)

    def test_expiry(self):
        self.clock.sleep(auth.SESSION_SECONDS + 1)
        self.assertEqual(self.b.get("/api/status")["statusCode"], 401)

    def test_logout_revokes_the_cookie(self):
        resp = self.b.post("/api/logout")
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("Max-Age=0", resp["cookies"][0])
        self.assertEqual(self.b.get("/api/status")["statusCode"], 401)  # old cookie replayed

    def test_tampered_token(self):
        name, value = self.b.cookie.split("=", 1)
        payload, mac = value.split(".")
        forged = auth._b64(json.dumps({"sid": "x", "csrf": "y", "exp": 9999999999}).encode())
        for bad in (f"{forged}.{mac}", f"{payload}.{mac[:-2]}AA", "garbage", ""):
            resp = self.app.http(event("GET", "/api/status", cookies=[f"{name}={bad}"]))
            self.assertEqual(resp["statusCode"], 401, bad)

    def test_password_change_invalidates_sessions(self):
        env = dict(ENV, DASHBOARD_PASSWORD="new password")
        self.app.auth = auth.Config(env["DASHBOARD_USERNAME"], env["DASHBOARD_PASSWORD"], env["DASHBOARD_SESSION_SECRET"])
        self.assertEqual(self.b.get("/api/status")["statusCode"], 401)

    def test_csrf_required_on_post(self):
        for headers in ({"x-csrf-token": ""}, {"x-csrf-token": "wrong"}, {"origin": "https://evil.example"},
                        {"sec-fetch-site": "cross-site"}):
            resp = self.b.post("/api/backups", headers=headers)
            self.assertEqual(resp["statusCode"], 403, headers)
        self.assertEqual(self.cloud.async_payloads, [])

    def test_errors_do_not_leak_internals(self):
        self.cloud.fail.add("instance")
        resp = self.b.post("/api/server/stop", {"confirm": "stop"})
        self.assertEqual(resp["statusCode"], 500)
        self.assertNotIn("arn:", resp["body"])

    def test_own_origin_is_the_function_url(self):
        self.assertEqual(event("POST", "/api/x")["headers"]["host"], HOST)


if __name__ == "__main__":
    unittest.main()
