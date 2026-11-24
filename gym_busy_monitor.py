#!/usr/bin/env python3
"""
MuscleHut Ashford busyness monitor

MuscleHut's ClubRight member site publicly exposes three goldmine data
sources in one page, no login required:
    https://musclehutashford.clubright.co.uk/MemberArea/ClubInfo

  1. Live attendance       (e.g. "36 / 150")
  2. "Popular Times Today"  (Chart.js bar chart - the 24 hourly averages
                             are embedded in the raw HTML)
  3. "Busiest Days"         (avg visits per weekday, also in raw HTML)

This tool (standard library only, no pip installs needed):
  today    - fetch now: live attendance + today's hourly curve with a text
             bar chart and the quietest training windows for today.
  watch    - poll every N minutes; logs live attendance to gym_busy.csv,
             and saves each new day's 24-hour curve to gym_hours.csv the
             first time it is seen (plus the weekday-average table).
  report   - after 1-2 weeks of `watch`, prints a weekday x hour heatmap
             built from real logged headcounts, the quietest hour per day,
             and the overall top 10 quietest slots.
  once     - one-shot fetch logging the current live attendance only.
  snapshot - one full logging cycle for cron / GitHub Actions (logs live
             attendance + curve data, then exits instead of looping).

Usage:
    python gym_busy_monitor.py today
    python gym_busy_monitor.py watch --interval 900      # every 15 min
    python gym_busy_monitor.py report                    # needs logged data
    python gym_busy_monitor.py report --plot heatmap.png # needs matplotlib

Notes:
  - Numbers are server-rendered in the HTML, so a plain fetch works.
  - A Playwright headless-browser fallback runs automatically if ClubRight
    ever changes this (`pip install playwright && playwright install chromium`).
  - Be polite: keep the polling interval at 10-15 minutes or more.
"""

import argparse
import csv
import html as html_mod
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

GYM_URL = "https://musclehutashford.clubright.co.uk/MemberArea/ClubInfo"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
HOURS = list(range(5, 24))  # gym hours approx 05:00-23:00
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TZ = ZoneInfo("Europe/London")  # timestamps are UK local so hour buckets stay right in summer


def now():
    return datetime.now(TZ)


# ---------------------------------------------------------------- fetching

def fetch_urllib(url: str) -> str:
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def fetch_playwright(url: str) -> str:
    """Full page HTML after JS has run, only needed if ClubRight changes things."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        text = page.content()
        browser.close()
        return text


def fetch_page(url: str) -> str:
    try:
        body = fetch_urllib(url)
    except Exception as exc:
        print(f"[warn] plain fetch failed ({exc}); trying headless browser...", file=sys.stderr)
        return fetch_playwright(url)
    if not re.search(r"Live\s*Attendance", body, re.I):
        print("[warn] numbers missing from raw HTML; trying headless browser...", file=sys.stderr)
        return fetch_playwright(url)
    return body


# ---------------------------------------------------------------- parsing

def readable(text: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html_mod.unescape(text))


def parse_status(body: str) -> dict:
    text = readable(body)
    data = {
        "fetched_at": now().isoformat(timespec="seconds"),
        "status": "",
        "closes_at": "",
        "attendance": "",
        "capacity": "",
    }
    m = re.search(r"Current\s*Status\s*(Open\s*24\s*Hours|Open|Closed)", text, re.I)
    if m:
        data["status"] = re.sub(r"\s+", " ", m.group(1)).strip().title()
    m = re.search(r"Closes at (\d{1,2}[:.]\d{2})", text, re.I)
    if m:
        data["closes_at"] = m.group(1).replace(".", ":")
    m = re.search(r"Live\s*Attendance\s*(\d+)\s*/\s*(\d+)", text, re.I)
    if m:
        data["attendance"], data["capacity"] = m.group(1), m.group(2)
    return data


def parse_hours(body: str):
    """The 24 hourly 'Popular Times Today' values embedded in the Chart.js config."""
    m = re.search(
        r"getElementById\('timesChartInline'\).*?data:\s*\[([^\]]*)\]", body, re.S
    )
    if not m:
        return None
    return [float(x) for x in m.group(1).split(",") if x.strip()]


def parse_busy_days(body: str):
    """The 7 weekday-average values from the 'Busiest Days' Chart.js config."""
    m = re.search(
        r"getElementById\('daysChartInline'\).*?data:\s*\[([^\]]*)\]", body, re.S
    )
    if not m:
        return None
    return [float(x) for x in m.group(1).split(",") if x.strip()]


# ---------------------------------------------------------------- logging

def append_csv(path: Path, row: dict, fieldnames=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames or list(row.keys()))
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def logged_hour_dates(path: Path) -> set:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as fh:
        return {row["date"] for row in csv.DictReader(fh)}


# ---------------------------------------------------------------- commands

def show_today(hours, busy_days, status):
    today_name = DAYS[now().weekday()]
    if status["attendance"]:
        print(f"MuscleHut Ashford NOW: {status['attendance']}/{status['capacity']} in the gym "
              f"({status['status']}, closes {status['closes_at']})\n")

    if hours:
        peak = max(hours)
        print(f"Typical Tuesday-style visit curve for today ({today_name}):")
        for h in range(24):
            v = hours[h]
            if v < 0.05:
                continue
            bar = "#" * int(round(v / max(peak, 1e-9) * 40)) if peak else ""
            print(f"  {h:02d}:00  {v:5.1f}  {bar}")
        ranked = sorted(((hours[h], h) for h in range(5, 23)), reverse=True)
        print("\nBusiest hours today typ.:",
              ", ".join(f"{h:02d}:00 ({v:.1f})" for v, h in ranked[:3]))
        print("Quietest trainable hours today:",
              ", ".join(f"{h:02d}:00 ({v:.1f})" for v, h in ranked[-4:]))

    if busy_days:
        print("\nAverage visits by weekday (gym's own data):")
        for name, v in sorted(zip(DAYS, busy_days), key=lambda x: x[1]):
            print(f"  {name}: {v:5.1f}")


def cmd_today(args):
    body = fetch_page(GYM_URL)
    show_today(parse_hours(body), parse_busy_days(body), parse_status(body))


def cmd_once(args):
    data = parse_status(fetch_page(GYM_URL))
    if data["attendance"]:
        print(f"MuscleHut Ashford now: {data['attendance']}/{data['capacity']} in the gym "
              f"({data['status']}, closes {data['closes_at']}) as of {data['fetched_at']}")
    else:
        print("Could not find the live attendance on the page.")
    append_csv(Path(args.csv), data)
    print(f"Logged to {args.csv}")


def record(body: str, csv_path: Path) -> dict:
    """One logging cycle: live attendance, plus (once per new date) today's
    24-hour curve and a snapshot of the weekday-average table."""
    data = parse_status(body)
    append_csv(csv_path, data)
    hours_path = csv_path.with_name(csv_path.stem + "_hours.csv")
    days_path = csv_path.with_name(csv_path.stem + "_days.csv")
    hours = parse_hours(body)
    today = now().date().isoformat()
    if hours and today not in logged_hour_dates(hours_path):
        row = {"date": today, "weekday": DAYS[now().weekday()]}
        row.update({f"h{h:02d}": f"{hours[h]:.2f}" for h in range(24)})
        append_csv(hours_path, row,
                   fieldnames=["date", "weekday"] + [f"h{h:02d}" for h in range(24)])
        print(f"saved today's hourly curve to {hours_path.name}")
    days = parse_busy_days(body)
    if days:
        row = {"fetched_at": data["fetched_at"]}
        row.update({name: f"{v:.2f}" for name, v in zip(DAYS, days)})
        append_csv(days_path, row, fieldnames=["fetched_at"] + DAYS)
    return data


def cmd_snapshot(args):
    """Single shot for cron / GitHub Actions: logs and exits."""
    data = record(fetch_page(GYM_URL), Path(args.csv))
    print(f"[{data['fetched_at']}] status={data['status']} "
          f"attendance={data['attendance']}/{str(data['capacity']) or '?'} -> {args.csv}")


def cmd_watch(args):
    csv_path = Path(args.csv)
    interval = max(300, args.interval)  # be polite, >= 5 minutes
    print(f"Watching {GYM_URL}\nLogging live attendance to {csv_path} every {interval}s. Ctrl-C to stop.")
    while True:
        try:
            data = record(fetch_page(GYM_URL), csv_path)
            print(f"[{data['fetched_at']}] status={data['status']} "
                  f"attendance={data['attendance']}/{str(data['capacity']) or '?'}")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[error] {exc} - will retry next cycle", file=sys.stderr)
        time.sleep(interval)


# ---------------------------------------------------------------- analysis

def load_samples(csv_path: str):
    buckets = defaultdict(list)
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            att = (row.get("attendance") or "").strip()
            if not att:
                continue
            try:
                dt = datetime.fromisoformat(row["fetched_at"])
            except (ValueError, KeyError):
                continue
            buckets[(dt.weekday(), dt.hour)].append(int(att))
    return buckets


def cmd_report(args):
    buckets = load_samples(args.csv)
    if not buckets:
        print(f"No attendance samples found in {args.csv}. Let `watch` run for a while first.")
        return

    total = sum(len(v) for v in buckets.values())
    print(f"\nAverage live attendance, MuscleHut Ashford ({total} samples)\n")
    print("      " + "".join(f"{h:>5}" for h in HOURS))
    for d in range(7):
        cells = []
        for h in HOURS:
            vals = buckets.get((d, h))
            cells.append(f"{int(sum(vals) / len(vals)):>5}" if vals else f"{'.':>5}")
        print(f"{DAYS[d]:>4}  " + "".join(cells))

    print("\nQuietest hours per day (avg headcount, >= 2 samples):")
    for d in range(7):
        scored = [(sum(v) / len(v), h, len(v)) for (dd, h), v in buckets.items() if dd == d and len(v) >= 2]
        scored.sort()
        if scored:
            avg, hour, n = scored[0]
            print(f"  {DAYS[d]}: {hour:02d}:00 - avg {avg:.1f} people (from {n} samples)")

    print("\nTop 10 quietest slots overall:")
    scored = [(sum(v) / len(v), DAYS[dd], h, len(v)) for (dd, h), v in buckets.items() if len(v) >= 2]
    scored.sort()
    for avg, day, hour, n in scored[:10]:
        print(f"  {day} {hour:02d}:00 - avg {avg:.1f} people ({n} samples)")

    if args.plot:
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            grid = [[(sum(buckets[(d, h)]) / len(buckets[(d, h)])) if (d, h) in buckets else float("nan")
                     for h in range(24)] for d in range(7)]
            fig, ax = plt.subplots(figsize=(14, 4))
            im = ax.imshow(grid, aspect="auto", cmap="RdYlGn_r")
            ax.set_yticks(range(7), DAYS)
            ax.set_xticks(range(0, 24, 2))
            ax.set_xlabel("Hour of day")
            ax.set_title("MuscleHut Ashford - average live attendance")
            fig.colorbar(im, label="People in gym")
            fig.tight_layout()
            fig.savefig(args.plot, dpi=150)
            print(f"\nSaved heatmap image to {args.plot}")
        except ImportError:
            print("\n(matplotlib not installed; skipping image. `pip install matplotlib` to enable.)")


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description="MuscleHut Ashford busyness monitor")
    ap.add_argument("command", choices=["today", "once", "watch", "snapshot", "report"])
    ap.add_argument("--csv", default="gym_busy.csv", help="CSV log file (default: gym_busy.csv)")
    ap.add_argument("--interval", type=int, default=900, help="poll seconds for watch (default 900)")
    ap.add_argument("--plot", default=None, help="report: save weekday x hour heatmap to this PNG")
    args = ap.parse_args()

    if args.command == "today":
        cmd_today(args)
    elif args.command == "once":
        cmd_once(args)
    elif args.command == "watch":
        cmd_watch(args)
    elif args.command == "snapshot":
        cmd_snapshot(args)
    else:
        cmd_report(args)


if __name__ == "__main__":
    main()
