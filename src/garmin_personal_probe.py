"""Compare Garmin Personal API readings with an existing MyPTAI dump."""

import argparse
import getpass
import io
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from analytics_engine import _garmin_number, body_battery_values


def field(payload, *keys):
    for key in keys:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def sleep_score(payload):
    score = field(payload, "dailySleepDTO", "sleepScores", "overall", "value")
    return score if score is not None else field(payload, "sleepScores", "overall", "value")


def sleep_fields(payload):
    sleep = field(payload, "dailySleepDTO") or payload
    return {
        "Sleep Score": _garmin_number(sleep_score(payload), maximum=100),
        "Durata sonno (s)": _garmin_number(field(sleep, "sleepTimeSeconds"), minimum=1),
        "Stress notturno": _garmin_number(field(sleep, "avgSleepStress"), maximum=100),
    }


def hrv_fields(payload):
    summary = field(payload, "hrvSummary") or payload
    return {
        "HRV notturna": _garmin_number(field(summary, "lastNightAvg"), minimum=1),
        "HRV settimanale": _garmin_number(field(summary, "weeklyAvg"), minimum=1),
        "Baseline HRV (min)": _garmin_number(field(summary, "baseline", "balancedLow"), minimum=1),
        "Baseline HRV (max)": _garmin_number(field(summary, "baseline", "balancedUpper"), minimum=1),
        "Baseline HRV (lowUpper)": _garmin_number(field(summary, "baseline", "lowUpper"), minimum=1),
    }


def battery_fields(payload, day):
    reports = payload if isinstance(payload, list) else [payload]
    values = body_battery_values(reports, day)
    if values:
        return {"Body Battery ultimo": values[-1], "Body Battery picco": max(values)}
    return {"Body Battery ultimo": None, "Body Battery picco": None}


def daily_fields(payload):
    return {
        "FC a riposo": _garmin_number(field(payload, "restingHeartRate"), minimum=1),
        "Stress diurno": _garmin_number(field(payload, "averageStressLevel"), maximum=100),
    }


def vo2_fields(payload):
    displayed = _garmin_number(field(payload, "generic", "vo2MaxValue"), minimum=1)
    precise = _garmin_number(field(payload, "generic", "vo2MaxPreciseValue"), minimum=1)
    return {
        "VO2max corsa": displayed or precise,
    }


def latest_running_vo2(payload, day):
    if not isinstance(payload, list):
        return None
    candidates = [
        row for row in payload
        if isinstance(field(row, "generic", "calendarDate"), str)
        and field(row, "generic", "calendarDate") <= day
        and vo2_fields(row)["VO2max corsa"] is not None
    ]
    return max(candidates, key=lambda row: row["generic"]["calendarDate"], default=None)


def fit_lap_count(archive):
    from garmin_fit_sdk import Decoder, Stream

    count = 0
    with ZipFile(io.BytesIO(archive)) as zipped:
        files = [entry for entry in zipped.infolist()
                 if not entry.is_dir() and entry.filename.lower().endswith(".fit")]
        if not files or sum(entry.file_size for entry in files) > 32 * 1024 * 1024:
            raise ValueError("Archivio FIT assente o troppo grande")
        for entry in files:
            messages, errors = Decoder(Stream.from_byte_io(io.BytesIO(zipped.read(entry)))).read()
            if errors:
                raise ValueError("File FIT non decodificabile")
            count += len(messages.get("lap_mesgs", []))
    return count


def compare(client, dump, end_date, activity_id, api_error):
    endpoints = dump["endpoints"]
    dump_date = date.fromisoformat(dump["timestamp"])
    lap_errors = api_error + (BadZipFile, ImportError)
    failed = False
    differences = 0
    missing = 0

    def legacy(name):
        return field(endpoints, name, "data")

    def report(day, official, current):
        nonlocal differences, missing
        for metric, value in official.items():
            previous = current.get(metric)
            if value is None:
                status = "dato API assente"
                missing += 1
            elif previous is None:
                status = "disponibile API, assente nel dump"
            else:
                status = "coincide" if value == previous else "diverso"
                differences += int(value != previous)
            print(f"{day} | {metric} | {status}")

    for offset in range(6, -1, -1):
        day = (end_date - timedelta(days=offset)).isoformat()
        sleep = legacy(f"sleep_day_{day}")
        if not sleep and day == dump_date.isoformat():
            sleep = legacy("sleep_today")
        if not sleep and day == (dump_date - timedelta(days=1)).isoformat():
            sleep = legacy("sleep_yesterday")
        for name, load, normalize, old in (
            ("sonno", lambda: client.get_daily_sleep(day), sleep_fields, sleep),
            ("HRV", lambda: client.get_daily_hrv(day), hrv_fields, None),
            ("Body Battery", lambda: client.get_daily_body_battery(day, day),
             lambda data: battery_fields(data, day), None),
        ):
            if name != "sonno":
                prefix = "hrv" if name == "HRV" else "body_battery"
                for suffix, difference in (("today", 0), ("yesterday", 1)):
                    if day == (dump_date - timedelta(days=difference)).isoformat():
                        old = legacy(f"{prefix}_{suffix}")
                        break
            try:
                report(day, normalize(load()), normalize(old))
            except api_error as exc:
                print(f"{day} | {name} | errore API ({type(exc).__name__})")
                failed = True

    day = end_date.isoformat()
    vo2_start = (end_date - timedelta(days=28)).isoformat()
    old_summary = legacy("user_summary_today") if end_date == dump_date else None
    old_max = legacy("max_metrics_latest") or field(legacy("training_status_today"), "mostRecentVO2Max")
    for name, load, normalize, old in (
        ("riepilogo giornaliero",
         lambda: client.get("/usersummary-service/usersummary/daily", params={"calendarDate": day}),
         daily_fields, old_summary),
        ("VO2max corsa",
         lambda: latest_running_vo2(client.get_daily_vo2_max(vo2_start, day), day),
         vo2_fields, old_max if end_date == dump_date else None),
    ):
        try:
            report(day, normalize(load()), normalize(old))
        except api_error as exc:
            print(f"{day} | {name} | errore API ({type(exc).__name__})")
            failed = True

    if activity_id:
        try:
            laps = fit_lap_count(client.download_activity(activity_id))
            old_laps = field(legacy(f"activity_splits_{activity_id}"), "lapDTOs")
            report("Attività", {"Numero lap": laps},
                   {"Numero lap": len(old_laps) if isinstance(old_laps, list) else None})
        except lap_errors as exc:
            print(f"Attività | Lap FIT | errore ({type(exc).__name__})")
            failed = True
    print(f"Esito confronto | differenze {differences} | dati API assenti {missing} | errori {int(failed)}")
    return int(failed or differences > 0 or missing > 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", type=Path, required=True)
    parser.add_argument("--end", type=date.fromisoformat, help="Ultimo giorno (default: data del dump)")
    parser.add_argument("--activity-id", type=int, help="ID di una corsa a ripetute nel dump")
    parser.add_argument("--key-file", type=Path, help="File contenente la chiave Garmin Personal API")
    args = parser.parse_args()
    if args.activity_id is not None and args.activity_id <= 0:
        parser.error("--activity-id deve essere positivo")

    try:
        dump = json.loads(args.dump.read_text(encoding="utf-8"))
        dump_date = date.fromisoformat(dump["timestamp"])
        if not isinstance(dump["endpoints"], dict):
            raise ValueError
    except (KeyError, TypeError, ValueError, OSError):
        parser.error("Serve un dump MyPTAI valido con timestamp ed endpoints")

    try:
        from garmin_personal_api import GarminPersonalApiError, PersonalApiAuthenticator, GarminPersonalApiClient
        from requests import RequestException
    except ImportError:
        parser.error("Installa il client Garmin come indicato nel README")

    key = os.environ.get("GARMIN_PERSONAL_API_KEY")
    if args.key_file is not None:
        try:
            key = args.key_file.expanduser().read_text(encoding="utf-8")
        except OSError:
            parser.error("File della chiave Garmin non leggibile")
    if not key and sys.stdin.isatty():
        key = getpass.getpass("Chiave Garmin Personal API: ")
    if not key or not key.strip():
        parser.error("Chiave Garmin Personal API mancante")

    try:
        with PersonalApiAuthenticator() as authenticator:
            token = authenticator.obtain_token(key.strip())
    except (GarminPersonalApiError, OSError, ValueError, RequestException) as exc:
        print(f"Autenticazione Garmin fallita ({type(exc).__name__})", file=sys.stderr)
        return 1
    with GarminPersonalApiClient(token.access_token) as client:
        return compare(client, dump, args.end or dump_date, args.activity_id,
                       (GarminPersonalApiError, RequestException, ValueError))


if __name__ == "__main__":
    sys.exit(main())
