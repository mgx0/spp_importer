#!/usr/bin/env python3
"""SPP IMS Electricity Consumption Collector & InfluxDB 1.8 Pipeline."""

import argparse
import base64
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import secrets
import sys
import time as time_module
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import requests

# ==========================================
# CUSTOMER CONFIGURATION
# ==========================================
CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config() -> dict[str, Any]:
    """Loads credentials and runtime settings from the local config file."""
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"Configuration file not found: {CONFIG_PATH}. "
            "Create config.json with SPP credentials, InfluxDB settings, "
            "DEBUG_OUTPUT, DAYS_BACK, END_DAYS_AGO, and SCHEDULE_TIMES."
        )

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {CONFIG_PATH}: {exc}") from exc

    required = [
        "USER_EMAIL",
        "USER_PASSWORD",
        "TARGET_EIC",
        "INFLUX_CONFIG",
        "DEBUG_OUTPUT",
        "DAYS_BACK",
        "END_DAYS_AGO",
        "SCHEDULE_TIMES",
    ]
    missing = [key for key in required if key not in data or not str(data[key]).strip()]
    if missing:
        raise ValueError(
            f"Configuration file is missing required values: {', '.join(missing)}"
        )

    influx_required = ["host", "port", "db", "measurement"]
    influx_missing = [
        key for key in influx_required
        if key not in data["INFLUX_CONFIG"]
    ]
    if influx_missing:
        raise ValueError(
            "Configuration INFLUX_CONFIG is missing required values: "
            f"{', '.join(influx_missing)}"
        )

    schedule_times = data["SCHEDULE_TIMES"]
    if not isinstance(schedule_times, list) or not schedule_times:
        raise ValueError(
            "Configuration SCHEDULE_TIMES must be a non-empty array of HH:MM strings"
        )
    try:
        for schedule_time in schedule_times:
            if not isinstance(schedule_time, str):
                raise ValueError
            parsed_time = time.fromisoformat(schedule_time)
            if parsed_time.strftime("%H:%M") != schedule_time:
                raise ValueError
    except ValueError as exc:
        raise ValueError(
            "Configuration SCHEDULE_TIMES entries must use 24-hour HH:MM format"
        ) from exc

    return data


CONFIG = load_config()
USER_EMAIL = str(CONFIG["USER_EMAIL"])
USER_PASSWORD = str(CONFIG["USER_PASSWORD"])
TARGET_EIC = str(CONFIG["TARGET_EIC"])
DEBUG_OUTPUT = CONFIG["DEBUG_OUTPUT"]
DAYS_BACK = CONFIG["DAYS_BACK"]
END_DAYS_AGO = CONFIG["END_DAYS_AGO"]
SCHEDULE_TIMES = [
    time.fromisoformat(schedule_time) for schedule_time in CONFIG["SCHEDULE_TIMES"]
]

target_day_back = -4

HIGH_TARIFF_WINDOWS = [
    (time(8, 30), time(9, 30)),
    (time(18, 30), time(19, 30)),
]
INFLUX_CONFIG = CONFIG["INFLUX_CONFIG"]

# ==========================================
# API CONFIGURATION
# ==========================================

SPP_BASE_URL = "https://moje.spp.sk"
CLIENT_ID = "nzp"
REDIRECT_URI = "https://moje.spp.sk/authentication/success"
SCOPE = "openid customer"
LOCAL_TZ = ZoneInfo("Europe/Bratislava")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# ==========================================
# AUTHENTICATION ENGINE (OAUTH2 + PKCE)
# ==========================================
def generate_pkce_pair() -> tuple[str, str]:
    """Generates PKCE code_verifier and S256 code_challenge per RFC 7636."""
    code_verifier = secrets.token_urlsafe(64)
    hashed = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = (
        base64.urlsafe_b64encode(hashed).decode("ascii").rstrip("=")
    )
    return code_verifier, code_challenge


def authenticate_and_get_tokens(username: str, password: str) -> dict:
    """Executes the Authorization Code Flow with PKCE against MojeSPP IdP."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "sk-SK,sk;q=0.9,en-US;q=0.8,en;q=0.7",
        }
    )

    verifier, challenge = generate_pkce_pair()
    state = secrets.token_hex(16)

    auth_params = {
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "state": state,
        "scope": SCOPE,
        "locale": "sk",
    }

    init_url = f"{SPP_BASE_URL}/prihlasenie/oauth/authorize"
    res = session.get(
        init_url, params=auth_params, allow_redirects=True, timeout=15
    )
    if res.status_code != 200:
        raise RuntimeError(
            f"Initial GET to authorize endpoint failed: HTTP {res.status_code}"
        )

    login_page_url = res.url

    # 2. Parse login form inputs and CSRF tokens
    soup = BeautifulSoup(res.text, "html.parser")
    form = soup.find("form")
    if not form:
        raise RuntimeError(
            "Login form not found (possible CAPTCHA or WAF interception)."
        )

    action_url = urljoin(login_page_url, form.get("action", ""))

    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        payload[name] = inp.get("value", "")

    username_field_found = False
    password_field_found = False

    for name in list(payload.keys()):
        lower_name = name.lower()
        if any(x in lower_name for x in ["user", "login", "email"]):
            payload[name] = username
            username_field_found = True
        elif any(x in lower_name for x in ["pass", "heslo"]):
            payload[name] = password
            password_field_found = True

    if not username_field_found:
        payload["username"] = username
    if not password_field_found:
        payload["password"] = password

    # 3. Post credentials and trace redirects
    login_headers = {
        "Referer": login_page_url,
        "Origin": SPP_BASE_URL,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    post_res = session.post(
        action_url,
        data=payload,
        headers=login_headers,
        allow_redirects=False,
        timeout=15,
    )

    auth_code: Optional[str] = None
    curr_res = post_res

    while curr_res.is_redirect or curr_res.status_code in (301, 302, 303, 307):
        redirect_url = urljoin(
            curr_res.url, curr_res.headers.get("Location", "")
        )
        parsed = urlparse(redirect_url)

        if redirect_url.startswith(REDIRECT_URI):
            qs = parse_qs(parsed.query)
            if "code" in qs:
                auth_code = qs["code"][0]
                break

        curr_res = session.get(redirect_url, allow_redirects=False, timeout=15)

    if not auth_code:
        raise RuntimeError(
            "Failed to retrieve authorization code (invalid credentials or 2FA challenge)."
        )

    # 4. Exchange authorization code for Bearer JWT token
    token_url = f"{SPP_BASE_URL}/prihlasenie/oauth/token"
    token_payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }

    token_res = session.post(token_url, data=token_payload, timeout=15)
    if token_res.status_code == 404:
        token_url = f"{SPP_BASE_URL}/oauth/token"
        token_res = session.post(token_url, data=token_payload, timeout=15)

    if token_res.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed: HTTP {token_res.status_code} - {token_res.text}"
        )

    return token_res.json()


# ==========================================
# DISCOVERY & METADATA RESOLUTION
# ==========================================
def extract_customer_id_from_token(token: str) -> str:
    """Extracts the 'sub' (customer UUID) claim from the JWT access token payload."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            raise ValueError("Malformed JWT token string")

        payload_b64 = parts[1]
        padded_b64 = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(padded_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))

        customer_id = payload.get("sub")
        if not customer_id:
            raise KeyError("JWT payload missing 'sub' claim")
        return customer_id
    except Exception as exc:
        raise ValueError(
            f"Failed to extract customer ID from token: {exc}"
        ) from exc


def find_delivery_point_by_eic(
    access_token: str,
    target_eic: str,
    customer_id: Optional[str] = None,
    spp_base_url: str = SPP_BASE_URL,
) -> Optional[dict[str, Any]]:
    """Resolves deliveryPoint metadata and contract ID by matching the target EIC."""
    if not customer_id:
        customer_id = extract_customer_id_from_token(access_token)

    search_url = (
        f"{spp_base_url}/api/customers/{customer_id}/united-delivery-points/search"
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "X-AppType": "WEB",
        "Origin": spp_base_url,
        "Referer": f"{spp_base_url}/",
        "User-Agent": USER_AGENT,
    }

    payload = {
        "pairingDone": True,
        "includeInactive": True,
        "paging": {"size": 1000},
        "shared": False,
    }

    res = requests.post(search_url, json=payload, headers=headers, timeout=15)
    res.raise_for_status()
    data = res.json()

    for entry in data.get("result", []):
        for dp in entry.get("deliveryPoints", []):
            if dp.get("eic") == target_eic:
                return dp

    return None


# ==========================================
# CONSUMPTION INGESTION & VALIDATION
# ==========================================
def fetch_spp_consumption(
    access_token: str,
    contract_id: str,
    date_from: Union[datetime, str],
    date_to: Union[datetime, str],
    interval: int = 1400,
    units: str = "KW",
    reading_type: str = "INTERVAL_METER_READING",
) -> dict:
    """Dispatches search POST request to MojeSPP consumption API."""
    url = f"{SPP_BASE_URL}/api/delivery-points/contracts/{contract_id}/consumptions/search"

    def format_iso_utc(dt: Union[datetime, str]) -> str:
        if isinstance(dt, str):
            return dt
        utc_dt = (
            dt.astimezone(timezone.utc)
            if dt.tzinfo
            else dt.replace(tzinfo=timezone.utc)
        )
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    payload = {
        "readAt": {
            "from": format_iso_utc(date_from),
            "to": format_iso_utc(date_to),
        },
        "type": reading_type,
        "interval": interval,
        "units": units,
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "X-AppType": "WEB",
        "Origin": SPP_BASE_URL,
        "Referer": f"{SPP_BASE_URL}/",
        "User-Agent": USER_AGENT,
    }

    res = requests.post(url, json=payload, headers=headers, timeout=20)
    res.raise_for_status()
    return res.json()


def is_time_in_window(check_time: time, start: time, end: time) -> bool:
    """Checks whether check_time falls within [start, end) interval."""
    if start <= end:
        return start <= check_time < end
    return check_time >= start or check_time < end


def is_high_tariff(local_dt: datetime) -> bool:
    """Determines if the timestamp falls into a configured High Tariff (VT) period."""
    sample_time = local_dt.time()
    for start, end in HIGH_TARIFF_WINDOWS:
        if is_time_in_window(sample_time, start, end):
            return True
    return False


def format_interval_ranges(missing_dts: list[datetime]) -> list[str]:
    """Compresses a sorted list of 15-minute datetimes into human-readable ranges."""
    if not missing_dts:
        return []

    def fmt(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    ranges = []
    range_start = missing_dts[0]
    range_end = missing_dts[0]
    count = 1

    for dt in missing_dts[1:]:
        if dt == range_end + timedelta(minutes=15):
            range_end = dt
            count += 1
        else:
            if count == 1:
                ranges.append(fmt(range_start))
            else:
                ranges.append(
                    f"{fmt(range_start)} -> {fmt(range_end)} ({count} intervals)"
                )
            range_start = dt
            range_end = dt
            count = 1

    # Flush last range
    if count == 1:
        ranges.append(fmt(range_start))
    else:
        ranges.append(
            f"{fmt(range_start)} -> {fmt(range_end)} ({count} intervals)"
        )

    return ranges


def validate_and_prepare_influx_records(
    api_data: dict[str, Any],
    target_date: datetime.date,
    contract_id: str,
    eic: str,
    measurement_name: str = "spp_consumption",
) -> tuple[bool, list[dict], list[str], dict[str, Any]]:
    """Validates that all 96 expected 15-minute intervals exist without gaps.

    Prepares structured point dicts and InfluxDB 1.8 Line Protocol entries.
    Identifies and logs exact missing intervals.
    """
    tariffs = api_data.get("tariffs", [])
    raw_entries = []
    for tariff in tariffs:
        raw_entries.extend(tariff.get("values", []))

    # Parse and index records by UTC timestamp
    records_by_time: dict[datetime, float] = {}
    for item in raw_entries:
        period_from = item.get("period", {}).get("from")
        if not period_from:
            continue
        if not period_from.endswith("Z"):
            period_from += "Z"

        dt_utc = datetime.fromisoformat(period_from.replace("Z", "+00:00"))
        val_kw = float(item.get("value", 0.0))
        records_by_time[dt_utc] = val_kw

    # Construct the exact expected 96 15-minute intervals for target_date in local time
    start_local = datetime.combine(target_date, time.min, tzinfo=LOCAL_TZ)
    expected_intervals = [
        start_local + timedelta(minutes=15 * i) for i in range(96)
    ]

    missing_local_dts: list[datetime] = []
    points = []
    line_protocol_lines = []

    total_kwh = 0.0
    high_kwh = 0.0
    low_kwh = 0.0

    if DEBUG_OUTPUT:
        print(
            f"[DEBUG] Validating {len(expected_intervals)} intervals for {target_date} "
            f"({len(raw_entries)} raw API entries)"
        )

    for expected_local in expected_intervals:
        expected_utc = expected_local.astimezone(timezone.utc)

        if expected_utc not in records_by_time:
            missing_local_dts.append(expected_local)
            if DEBUG_OUTPUT:
                print(
                    f"[DEBUG] MISSING {expected_local.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"local -> {expected_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC"
                )
            continue

        kw = records_by_time[expected_utc]
        kwh = kw * 0.25  # 15 min = 0.25 hour
        tariff_type = "VT" if is_high_tariff(expected_local) else "NT"

        if DEBUG_OUTPUT:
            print(
                f"[DEBUG] PROCESS {expected_local.strftime('%Y-%m-%d %H:%M:%S')} local "
                f"-> {expected_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC | "
                f"power={kw} kW | energy={kwh} kWh | tariff={tariff_type}"
            )

        if tariff_type == "VT":
            high_kwh += kwh
        else:
            low_kwh += kwh
        total_kwh += kwh

        epoch_ns = int(expected_utc.timestamp() * 1_000_000_000)

        # 1. Structured Python dict (client-ready)
        point = {
            "measurement": measurement_name,
            "tags": {
                "eic": eic,
                "contract_id": contract_id,
                "tariff": tariff_type,
                "source": "spp_ims",
            },
            "time": expected_utc.isoformat(),
            "fields": {
                "power_kw": round(kw, 4),
                "energy_kwh": round(kwh, 4),
            },
        }
        points.append(point)

        # 2. Native InfluxDB 1.8 Line Protocol line
        line = (
            f"{measurement_name},"
            f"eic={eic},"
            f"contract_id={contract_id},"
            f"tariff={tariff_type},"
            f"source=spp_ims "
            f"power_kw={round(kw, 4)},energy_kwh={round(kwh, 4)} "
            f"{epoch_ns}"
        )
        line_protocol_lines.append(line)

    summary: dict[str, Any] = {
        "total_kwh": round(total_kwh, 4),
        "low_tariff_kwh": round(low_kwh, 4),
        "high_tariff_kwh": round(high_kwh, 4),
        "count": len(points),
        "missing_count": len(missing_local_dts),
        "missing_intervals": [
            dt.strftime("%Y-%m-%d %H:%M:%S") for dt in missing_local_dts
        ],
    }

    if missing_local_dts:
        ranges = format_interval_ranges(missing_local_dts)
        print(
            f"\n[!] INCOMPLETE DATASET: Received {len(points)}/96 intervals ({len(missing_local_dts)} missing).",
            file=sys.stderr,
        )
        print("[!] Missing time ranges (local time):", file=sys.stderr)
        for r in ranges:
            print(f"    - {r}", file=sys.stderr)

        return False, points, line_protocol_lines, summary

    return True, points, line_protocol_lines, summary


def send_to_influxdb_18(
    lines: list[str],
    host: str,
    port: int,
    db: str,
    precision: str = "ns",
    timeout: int = 10,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> None:
    """Writes Line Protocol records directly to InfluxDB 1.8 via HTTP API."""
    url = f"http://{host}:{port}/write?db={db}&precision={precision}"
    payload = "\n".join(lines).encode("utf-8")

    if username or password:
        res = requests.post(
            url,
            data=payload,
            auth=(username or "", password or ""),
            timeout=timeout,
        )
    else:
        res = requests.post(url, data=payload, timeout=timeout)
    if res.status_code not in (200, 204):
        raise RuntimeError(
            f"InfluxDB ingestion failed: HTTP {res.status_code} - {res.text}"
        )
    print(f"[+] Successfully wrote {len(lines)} points to InfluxDB ({db}).")


def run_once() -> None:
    """Authenticate, fetch the configured historical window, and ingest it."""
    print("[*] Authenticating against MojeSPP IdP...")
    tokens = authenticate_and_get_tokens(USER_EMAIL, USER_PASSWORD)
    jwt_token = tokens["access_token"]
    print("[+] Authentication successful, token acquired.")

    customer_id = extract_customer_id_from_token(jwt_token)
    print(f"[+] Customer ID extracted: {customer_id}")

    dp_meta = find_delivery_point_by_eic(
        access_token=jwt_token, target_eic=TARGET_EIC
    )
    if not dp_meta or "contract" not in dp_meta:
        raise RuntimeError(
            f"No delivery point/contract found for EIC {TARGET_EIC}"
        )

    contract_id = dp_meta["contract"]["id"]
    print(f"[+] Contract ID resolved: {contract_id}")

    now_local = datetime.now(LOCAL_TZ)
    sent_days = 0

    # Process DAYS_BACK days, ending END_DAYS_AGO days before today.
    first_days_ago = END_DAYS_AGO + DAYS_BACK - 1
    for days_ago in range(first_days_ago, END_DAYS_AGO - 1, -1):
        target_date = now_local.date() - timedelta(days=days_ago)
        local_start = datetime.combine(target_date, time.min, tzinfo=LOCAL_TZ)
        local_end = datetime.combine(
            target_date + timedelta(days=1), time.min, tzinfo=LOCAL_TZ
        )

        print(
            f"[*] Querying consumption for {target_date} "
            f"({local_start} -> {local_end})..."
        )
        raw_data = fetch_spp_consumption(
            access_token=jwt_token,
            contract_id=contract_id,
            date_from=local_start,
            date_to=local_end,
            interval=1400,
            units="KW",
        )

        is_complete, _points, lp_lines, summary = (
            validate_and_prepare_influx_records(
                api_data=raw_data,
                target_date=target_date,
                contract_id=contract_id,
                eic=TARGET_EIC,
                measurement_name=INFLUX_CONFIG["measurement"],
            )
        )

        print(f"\nEnergy Balance Summary for {target_date}:")
        print(f"  Valid Intervals:    {summary['count']}/96")
        print(f"  Total Energy:       {summary['total_kwh']} kWh")
        print(f"  Low Tariff (NT):    {summary['low_tariff_kwh']} kWh")
        print(f"  High Tariff (VT):   {summary['high_tariff_kwh']} kWh")

        if not is_complete:
            print(
                f"[!] Daily dataset for {target_date} is incomplete, "
                "but valid intervals will still be sent to InfluxDB.",
                file=sys.stderr,
            )

        if not lp_lines:
            print(
                f"[!] No valid intervals for {target_date}; skipping InfluxDB write.",
                file=sys.stderr,
            )
            continue

        send_to_influxdb_18(
            lines=lp_lines,
            host=INFLUX_CONFIG["host"],
            port=INFLUX_CONFIG["port"],
            db=INFLUX_CONFIG["db"],
            username=INFLUX_CONFIG.get("username"),
            password=INFLUX_CONFIG.get("password"),
        )
        sent_days += 1

    print(f"[+] Finished processing {sent_days} day(s) to InfluxDB.")


def next_scheduled_run(now: datetime) -> datetime:
    """Return the next configured run time in the local timezone."""
    now = now.astimezone(LOCAL_TZ)
    for day_offset in (0, 1):
        run_date = now.date() + timedelta(days=day_offset)
        for scheduled_time in sorted(SCHEDULE_TIMES):
            scheduled = datetime.combine(run_date, scheduled_time, tzinfo=LOCAL_TZ)
            if scheduled > now:
                return scheduled
    raise RuntimeError("Unable to determine the next scheduled run")


def main() -> None:
    """Run ingestion on the configured Europe/Bratislava schedule."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--process-once",
        action="store_true",
        help="Process the configured date range once and exit without scheduling.",
    )
    args = parser.parse_args()

    if args.process_once:
        started_at = datetime.now(LOCAL_TZ)
        print(f"\n[*] Starting one-time ingestion run at {started_at.isoformat()}")
        try:
            run_once()
        except Exception as err:
            print(f"[!] Error during ingestion run: {err}", file=sys.stderr)
            raise SystemExit(1) from err
        return

    started_at = datetime.now(LOCAL_TZ)
    print(f"\n[*] Starting initial ingestion run at {started_at.isoformat()}")
    try:
        run_once()
    except Exception as err:
        print(f"[!] Error during initial ingestion run: {err}", file=sys.stderr)

    while True:
        now = datetime.now(LOCAL_TZ)
        next_run_at = next_scheduled_run(now)
        delay_seconds = (next_run_at - now).total_seconds()
        print(
            f"[*] Next ingestion run at {next_run_at.isoformat()} "
            f"(in {delay_seconds / 60:.1f} minutes)."
        )
        time_module.sleep(delay_seconds)

        started_at = datetime.now(LOCAL_TZ)
        print(f"\n[*] Starting ingestion run at {started_at.isoformat()}")
        try:
            run_once()
        except Exception as err:
            print(f"[!] Error during ingestion run: {err}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[*] Ingestion worker stopped.")
