import unittest

from fakes import Browser, body, event, make_app

import handler
import runtime


class Entry(unittest.TestCase):
    def test_dispatch(self):
        app, *_ = make_app()
        handler._app = app
        try:
            self.assertEqual(handler.lambda_handler(event("GET", "/favicon.svg"), None)["statusCode"], 200)
            self.assertEqual(handler.lambda_handler({"task": "backup", "id": "nope"}, None), {"result": "ignored"})
            with self.assertRaises(ValueError):
                handler.lambda_handler({"Records": []}, None)
        finally:
            handler._app = None


class Routing(unittest.TestCase):
    def setUp(self):
        self.app, self.cloud, self.store, self.clock = make_app()
        self.b = Browser(self.app)

    def test_unknown_and_wrong_method(self):
        self.assertEqual(self.b.get("/api/nope")["statusCode"], 404)
        self.assertEqual(self.b.get("/api/server/stop")["statusCode"], 405)
        self.assertEqual(self.app.http(event("POST", "/"))["statusCode"], 404)

    def test_body_limits(self):
        resp = self.b.post("/api/backups", {"x": "y" * 9000})
        self.assertEqual(resp["statusCode"], 413)

    def test_settings_behind_login(self):
        data = body(self.b.get("/api/settings"))
        self.assertEqual(data["server"]["name"], "Fixture World")
        self.assertNotIn("fixture-Admin-SECRET", str(data))


class SteamListing(unittest.TestCase):
    def test_real_response_shape(self):
        data = {"response": {"success": True, "servers": [
            {"addr": "203.0.113.10:15637", "steamid": "90000000000000000", "appid": 1203620, "gamedir": "Enshrouded",
             "region": -1, "secure": False, "lan": False, "gameport": 15637, "specport": 0},
            {"addr": "203.0.113.10:27015", "appid": 730, "gamedir": "csgo", "gameport": 27015}]}}
        self.assertEqual(runtime.parse_steam_listing(data),
                         {"listed": True, "servers": [{"addr": "203.0.113.10:15637", "gameport": 15637}]})
        self.assertEqual(runtime.parse_steam_listing({"response": {"success": True}}), {"listed": False, "servers": []})


if __name__ == "__main__":
    unittest.main()
