import contextlib
import io
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from garmin_personal_probe import battery_fields, compare, hrv_fields, latest_running_vo2, sleep_fields


DAY = date(2026, 11, 16)


class PersonalApiFixture:
    def get_daily_sleep(self, day):
        return {"calendarDate": day, "sleepTimeSeconds": 25920,
                "avgSleepStress": 13, "sleepScores": {"overall": {"value": 86}}}

    def get_daily_hrv(self, day):
        return {"hrvSummary": {"calendarDate": day, "lastNightAvg": 79, "weeklyAvg": 63,
                               "baseline": {"balancedLow": 59, "balancedUpper": 105, "lowUpper": 51}}}

    def get_daily_body_battery(self, start, end):
        return [{"date": start, "bodyBatteryValuesArray": [[3000, -1], [2000, 36], [1000, 90]]}]

    def get(self, path, params):
        assert path == "/usersummary-service/usersummary/daily"
        assert params == {"calendarDate": DAY.isoformat()}
        return {"restingHeartRate": 46, "averageStressLevel": 26}

    def get_daily_vo2_max(self, start, end):
        return [{"generic": {"calendarDate": "2026-11-15", "vo2MaxValue": 54}}]


def legacy_dump(client):
    endpoints = {}
    for offset in range(7):
        day = (DAY - timedelta(days=offset)).isoformat()
        endpoints[f"sleep_day_{day}"] = {"data": {"dailySleepDTO": client.get_daily_sleep(day)}}
    endpoints["hrv_today"] = {"data": client.get_daily_hrv(DAY.isoformat())}
    endpoints["hrv_yesterday"] = {"data": client.get_daily_hrv((DAY - timedelta(days=1)).isoformat())}
    endpoints["body_battery_today"] = {"data": client.get_daily_body_battery(DAY.isoformat(), DAY.isoformat())}
    endpoints["body_battery_yesterday"] = {"data": client.get_daily_body_battery(
        (DAY - timedelta(days=1)).isoformat(), (DAY - timedelta(days=1)).isoformat())}
    endpoints["user_summary_today"] = {"data": {"restingHeartRate": 46, "averageStressLevel": 26}}
    endpoints["max_metrics_latest"] = {"data": {"generic": {"vo2MaxValue": 54}}}
    return {"timestamp": DAY.isoformat(), "endpoints": endpoints}


class TestPersonalApiProbe(unittest.TestCase):
    def test_battery_filters_missing_and_orders_multiple_reports(self):
        reports = [
            {"date": DAY.isoformat(), "bodyBatteryValuesArray": [[4000, -1], [2000, 50], [1000, 90]]},
            {"date": DAY.isoformat(), "bodyBatteryValuesArray": [[3000, 36], [5000, float("nan")], [6000, True]]},
            {"date": "2026-11-15", "bodyBatteryValuesArray": [[7000, 100]]},
        ]
        self.assertEqual(battery_fields(reports, DAY.isoformat()),
                         {"Body Battery ultimo": 36, "Body Battery picco": 90})

    def test_direct_and_nested_sleep_have_equal_fields(self):
        direct = PersonalApiFixture().get_daily_sleep(DAY.isoformat())
        self.assertEqual(sleep_fields(direct), sleep_fields({"dailySleepDTO": direct}))
        self.assertEqual(sleep_fields(None), {"Sleep Score": None, "Durata sonno (s)": None, "Stress notturno": None})

    def test_upper_hrv_bound_is_compared(self):
        fields = hrv_fields(PersonalApiFixture().get_daily_hrv(DAY.isoformat()))
        self.assertEqual(fields["Baseline HRV (max)"], 105)

    def test_latest_vo2_is_running_and_not_from_future(self):
        rows = [
            {"cycling": {"calendarDate": "2026-11-16", "vo2MaxValue": 68}},
            {"generic": {"calendarDate": "2026-11-15", "vo2MaxValue": 54}},
            {"generic": {"calendarDate": "2026-11-17", "vo2MaxValue": 55}},
        ]
        self.assertEqual(latest_running_vo2(rows, DAY.isoformat()), rows[1])
        self.assertIsNone(latest_running_vo2(None, DAY.isoformat()))

    def run_comparison(self, client, dump):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = compare(client, dump, DAY, None, (ValueError,))
        return code, output.getvalue()

    def test_matching_data_succeeds_without_printing_values(self):
        client = PersonalApiFixture()
        code, output = self.run_comparison(client, legacy_dump(client))
        self.assertEqual(code, 0)
        self.assertIn("coincide", output)
        self.assertNotIn("25920", output)
        self.assertNotIn("105", output)

    def test_difference_and_missing_api_fail(self):
        client = PersonalApiFixture()
        dump = legacy_dump(client)
        dump["endpoints"]["max_metrics_latest"]["data"]["generic"]["vo2MaxValue"] = 52
        code, output = self.run_comparison(client, dump)
        self.assertEqual(code, 1)
        self.assertIn("VO2max corsa | diverso", output)
        client.get_daily_hrv = lambda _: None
        code, output = self.run_comparison(client, legacy_dump(PersonalApiFixture()))
        self.assertEqual(code, 1)
        self.assertIn("dato API assente", output)

    def test_api_error_is_reported_without_its_message(self):
        client = PersonalApiFixture()
        def failing_call(_day):
            raise ValueError("token-riservato")
        client.get_daily_sleep = failing_call
        code, output = self.run_comparison(client, legacy_dump(PersonalApiFixture()))
        self.assertEqual(code, 1)
        self.assertIn("errore API (ValueError)", output)
        self.assertNotIn("token-riservato", output)


if __name__ == "__main__":
    unittest.main()
