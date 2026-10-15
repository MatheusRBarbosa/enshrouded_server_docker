#!/usr/bin/env python3
"""Run the whole dashboard locally against in-memory fakes; no AWS access needed.

    python3 dashboard/dev_server.py                 # http://localhost:8000, login dev / dev
    python3 dashboard/dev_server.py --state stopped

Uses the same api.App as the Lambda, with tests/fakes.py standing in for AWS (EC2, SSM, S3,
CloudWatch Logs, Scheduler) and an in-memory store instead of DynamoDB. Start/stop/backup work
against the fake, so every button and state can be exercised.
"""
import argparse
import http.server
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "tests"), os.path.join(HERE, "backend")]
import api  # noqa: E402
import fakes  # noqa: E402
import ops  # noqa: E402
import store  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--state", default="running", help="initial machine state: running, stopped, ...")
    args = p.parse_args()

    clock = ops.Clock()
    cloud = fakes.Cloud(clock, args.state)
    now = time.time()
    cloud.snapshot["inspectedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 30))
    real_instance = cloud.instance
    cloud.instance = lambda: dict(real_instance(), launchTime=now - 3600)
    cloud.events = [{"stream": "game", "t": int((now - 600 + i * 7) * 1000), "m": f"[{'IWE'[i % 7 // 5]} 00:{i:02d}:00,000] sample line {i}"}
                    for i in range(80)]
    env = dict(fakes.ENV, DASHBOARD_USERNAME="dev", DASHBOARD_PASSWORD="dev")
    app = api.App(store.MemoryStore(), cloud, clock, env)
    app.own_origin = f"http://localhost:{args.port}"

    def worker():  # stands in for Lambda's async invocations
        while True:
            while cloud.async_payloads:
                try:
                    app.task(cloud.async_payloads.pop(0))
                except Exception as e:  # noqa: BLE001
                    print("task failed:", e)
            if cloud.state == "stopping":
                cloud.state = "stopped"
            elif cloud.state == "pending":
                cloud.state = "running"
            time.sleep(2)
    threading.Thread(target=worker, daemon=True).start()

    class Handler(http.server.BaseHTTPRequestHandler):
        def handle_any(self, method):
            path, _, query = self.path.partition("?")
            length = int(self.headers.get("Content-Length") or 0)
            headers = {k.lower(): v for k, v in self.headers.items()}
            event = {"rawPath": path, "rawQueryString": query, "headers": headers,
                     "cookies": [c.strip() for c in headers.get("cookie", "").split(";") if c.strip()],
                     "body": self.rfile.read(length).decode() if length else "", "isBase64Encoded": False,
                     "requestContext": {"http": {"method": method, "sourceIp": self.client_address[0]}}}
            resp = app.http(event)
            data = resp.get("body", "").encode()
            self.send_response(resp["statusCode"])
            for k, v in resp["headers"].items():
                self.send_header(k, v)
            for c in resp.get("cookies", []):
                # Browsers only accept Secure/__Host- cookies on https or localhost
                self.send_header("Set-Cookie", c)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

        def log_message(self, *a):
            pass

    print(f"Dashboard on http://localhost:{args.port} (login dev / dev, fake AWS, machine {args.state})")
    http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
