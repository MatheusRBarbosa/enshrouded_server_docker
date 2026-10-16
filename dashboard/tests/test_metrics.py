import unittest

from fakes import T0, Browser, body, event, make_app

import metrics


class UsageCharts(unittest.TestCase):
    def setUp(self):
        self.app, self.cloud, self.store, self.clock = make_app()
        self.b = Browser(self.app)

    def get(self):
        resp = self.b.get("/api/metrics")
        self.assertEqual(resp["statusCode"], 200)
        return body(resp)

    def test_needs_a_session(self):
        self.assertEqual(self.app.http(event("GET", "/api/metrics"))["statusCode"], 401)

    def test_cpu_and_memory_for_the_last_six_hours_of_this_instance(self):
        data = self.get()
        self.assertEqual((data["state"], data["errors"]), ("running", []))
        self.assertEqual(data["to"], "2026-10-15T19:00:00Z")
        self.assertEqual(data["from"], "2026-10-15T13:00:00Z")
        cpu, mem = data["series"]["cpu"], data["series"]["memory"]
        self.assertEqual((cpu["period"], mem["period"]), (300, 60))
        self.assertEqual((len(cpu["points"]), len(mem["points"])), (71, 359))
        q = {x["Id"]: x["MetricStat"] for x in self.cloud.metric_queries[0]}
        self.assertEqual((q["cpu"]["Metric"]["Namespace"], q["cpu"]["Metric"]["MetricName"]), ("AWS/EC2", "CPUUtilization"))
        self.assertEqual((q["memory"]["Metric"]["Namespace"], q["memory"]["Metric"]["MetricName"]), ("Enshrouded", "mem_used_percent"))
        self.assertEqual(q["memory"]["Metric"]["Dimensions"], [{"Name": "InstanceId", "Value": "i-0123"}])

    def test_points_are_sorted_rounded_and_clamped(self):
        self.cloud.metric_points = {"cpu": [[2000, 12.345], [1000, 101.2]], "memory": [[1000, -3]]}
        series = self.get()["series"]
        self.assertEqual(series["cpu"]["points"], [[1000, 100.0], [2000, 12.3]])
        self.assertEqual(series["memory"]["points"], [[1000, 0.0]])

    def test_viewers_share_one_cloudwatch_read_for_two_minutes(self):
        self.get()
        self.clock.t += metrics.CACHE_SECONDS - 1
        self.assertEqual(len(self.get()["series"]["cpu"]["points"]), 71)
        self.assertEqual(self.cloud.calls.count("metric_data"), 1)
        self.clock.t += 2
        self.get()
        self.assertEqual(self.cloud.calls.count("metric_data"), 2)

    def test_cache_follows_the_instance_and_reports_its_current_state(self):
        self.get()
        self.cloud.state = "stopped"
        self.assertEqual(self.get()["state"], "stopped")
        self.assertEqual(self.cloud.calls.count("metric_data"), 1)
        real = self.cloud.instance
        self.cloud.instance = lambda: dict(real(), id="i-new")
        self.get()
        self.assertEqual(self.cloud.calls.count("metric_data"), 2)

    def test_aws_failure_is_reported_not_fatal(self):
        self.cloud.fail.add("metric_data")
        data = self.get()
        self.assertEqual(data["errors"], ["Could not read CPU and memory usage from AWS."])
        self.assertEqual(data["series"]["cpu"]["points"], [])
        self.cloud.fail.clear()
        self.assertEqual(self.get()["errors"], [])  # a failure isn't cached

    def test_no_instance(self):
        self.cloud.instance = lambda: None
        data = self.get()
        self.assertIsNone(data["state"])
        self.assertNotIn("metric_data", self.cloud.calls)


if __name__ == "__main__":
    unittest.main()
