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


def field(payload, *keys):
    for key in keys:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def sleep_score(payload):
    score = field(payload, "dailySleepDTO", "sleepScores", "overall", "value")
    return score if score is not None else field(payload, "sleepScores", "overall", "value")


def hrv_fields(payload):
    summary = field(payload, "hrvSummary") or payload
    return {
        "HRV notturna": field(summary, "lastNightAvg"),
        "HRV settimanale": field(summary, "weeklyAvg"),
        "Baseline HRV (min)": field(summary, "baseline", "balancedLow"),
        "Baseline HRV (lowUpper)": field(summary, "baseline", "lowUpper"),
    }


def battery_fields(payload, day):
    reports = payload if isinstance(payload, list) else [payload]
    for report in reports:
        if not isinstance(report, dict) or report.get("date", day) != day:
            continue
        samples = report.get("bodyBatteryValuesArray") or []
        values = [sample[1] for sample in samples
                  if isinstance(sample, list) and len(sample) > 1
                  and isinstance(sample[1], (float, int))]
        if values:
            return {"Body Battery ultimo": values[-1], "Body Battery picco": max(values)}
    return {"Body Battery ultimo": None, "Body Battery picco": None}


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

    def legacy(name):
        return field(endpoints, name, "data")

    def report(day, official, current):
        for metric, value in official.items():
            previous = current.get(metric)
            if value is None:
                status = "dato API assente"
            elif previous is None:
                status = "disponibile API, assente nel dump"
            else:
                status = "coincide" if value == previous else "diverso"
            print(f"{day} | {metric} | {status}")

    for offset in range(6, -1, -1):
        day = (end_date - timedelta(days=offset)).isoformat()
        for name, load, normalize, old in (
            ("sonno", lambda: client.get_daily_sleep(day),
             lambda data: {"Sleep Score": sleep_score(data)}, legacy(f"sleep_day_{day}")),
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

    if activity_id:
        try:
            laps = fit_lap_count(client.download_activity(activity_id))
            old_laps = field(legacy(f"activity_splits_{activity_id}"), "lapDTOs")
            report("Attività", {"Numero lap": laps},
                   {"Numero lap": len(old_laps) if isinstance(old_laps, list) else None})
        except lap_errors as exc:
            print(f"Attività | Lap FIT | errore ({type(exc).__name__})")
            failed = True
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", type=Path, required=True)
    parser.add_argument("--end", type=date.fromisoformat, help="Ultimo giorno (default: data del dump)")
    parser.add_argument("--activity-id", type=int, help="ID di una corsa a ripetute nel dump")
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
