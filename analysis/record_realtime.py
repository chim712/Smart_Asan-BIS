#!/usr/bin/env python3
"""아산시 BIS 실시간 위치 녹화기 (계열 단위).

계열 하나의 route_id(상·하행)를 돌아가며 searchBusRealLocationDetail로 호출하고
응답을 JSONL로 쌓는다. 나중에 오프라인 매핑 시험과 재생(replay)에 쓴다.
표준 라이브러리만 사용한다 (Python 3.9+, Windows·Ubuntu 공통).

출력 (기본 data/realtime/<YYYY-MM-DD_HHMM>_s<계열 또는 routes>/, HHMM = 시작 시각):
    meta.json    실행 설정, 대상 노선 목록, 종료 시 통계
    routes.json  녹화 시작 시점의 노선별 정류소 순서·선형 (녹화본만으로 매핑 시험이 되도록)
    loc.jsonl    한 줄 = 호출 1회. 값이 빈 필드는 버린다(원본에서 빈 값은 의미 없는 기본값).

사용 예:
    python record_realtime.py --series 9 --start 15:00 --until 17:00
    python record_realtime.py --series 9 --duration 10            # 10분 시험
    python record_realtime.py --routes 900,910 --duration 30      # 노선명 또는 route_id
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))  # Windows에 tzdata가 없을 수 있어 zoneinfo 대신 고정 오프셋
BASE = "https://bus.asan.go.kr/mobile/traffic/"
HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://bus.asan.go.kr",
    "Referer": "https://bus.asan.go.kr/web/traffic_bus_line_Search",
    "User-Agent": "Mozilla/5.0 asan-bus-toy-recorder/0.1",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}
EXCLUDED_ROUTE_TYPES = {"13"}  # 천안시 노선


def now_kst() -> datetime:
    return datetime.now(KST)


def post(endpoint: str, params: dict, timeout: float) -> dict:
    """POST 1회. 재시도는 호출 쪽 스케줄러가 간격을 늘려 처리한다."""
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(BASE + endpoint, data=body, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def compact(record: dict) -> dict:
    """빈 문자열/0/None 필드를 버리고 문자열 양끝 공백을 제거한다."""
    out = {}
    for key, value in record.items():
        if isinstance(value, str):
            value = value.strip()
        if value in ("", 0, None):
            continue
        out[key] = value
    return out


def series_of(route_name: str) -> str | None:
    """core/crawl/crawl_asan.py와 같은 규칙 (core/groups.py 통합 시 그쪽으로 옮긴다)."""
    match = re.fullmatch(r"(\d+)(?:-\d+)?", route_name.strip())
    if not match:
        return None
    number = int(match.group(1))
    if 100 <= number <= 999:
        return str(number // 100)
    if 10 <= number <= 99:
        return str(number // 10)
    return None


def parse_hhmm(text: str, base: datetime) -> datetime:
    hh, mm = (int(x) for x in text.split(":"))
    return base.replace(hour=hh, minute=mm, second=0, microsecond=0)


def select_targets(listing: list[dict], series: str | None, routes: str | None) -> list[dict]:
    rows = [r for r in listing if r.get("route_type") not in EXCLUDED_ROUTE_TYPES]
    if routes:
        wanted = {x.strip() for x in routes.split(",") if x.strip()}
        rows = [r for r in rows if r.get("route_id") in wanted or r.get("route_name") in wanted]
    elif series:
        rows = [r for r in rows if series_of(r.get("route_name", "")) == series]
    return sorted(rows, key=lambda r: (r.get("route_name", ""), r.get("route_id", "")))


def snapshot_route_maps(targets: list[dict], timeout: float, delay: float) -> dict:
    """노선별 정류소 순서·선형을 받아 둔다. 실패한 노선은 비워 두고 녹화는 계속한다."""
    result = {}
    for info in targets:
        rid = info["route_id"]
        time.sleep(delay)
        try:
            raw = post("searchBusRouteMap", {"busRouteId": rid}, timeout)
        except Exception as err:
            print(f"  선형 수집 실패 {info.get('route_name')} ({rid}): {err}", file=sys.stderr)
            result[rid] = {"error": str(err)}
            continue
        stops = [
            {"ord": int(s["stop_ord"]), "stop_id": s["stop_id"].strip(),
             "name": s.get("stop_name", "").strip(),
             "lat": float(s["lat"]) if s.get("lat") else None,
             "lng": float(s["lng"]) if s.get("lng") else None}
            for s in raw.get("busRouteDetailList1", []) if s.get("stop_ord")
        ]
        stops.sort(key=lambda s: s["ord"])
        pts = sorted(raw.get("busRouteDetailMapVoList", []),
                     key=lambda p: (int(p["route_ord"]), int(p["ord"])))
        result[rid] = {
            "route_name": info.get("route_name", ""),
            "stops": stops,
            "shape": [[float(p["lat"]), float(p["lng"])] for p in pts],  # 원본 그대로 (fix_shape 보정 전)
        }
    return result


class Stats:
    """종료 시 출력할 요약. 차량 소속(veh_id 앞 3자리)별 좌표 상태를 센다."""

    def __init__(self) -> None:
        self.requests = self.errors = self.empty = 0
        self.coord = Counter()     # (소속, 상태) -> 건수
        self.vehicles = set()

    def add(self, vehicles: list[dict]) -> None:
        self.requests += 1
        if not vehicles:
            self.empty += 1
        for v in vehicles:
            veh = v.get("veh_id", "")
            self.vehicles.add(veh)
            org = veh[:3] or "?"
            lat, lng = v.get("lat", ""), v.get("lng", "")
            if not lat or not lng:
                state = "좌표 없음"
            elif lat == lng:
                state = "lat=lng"
            else:
                try:
                    ok = 126.0 < float(lng) < 128.0 and 36.0 < float(lat) < 37.5
                except ValueError:
                    ok = False
                state = "정상" if ok else "범위 밖"
            self.coord[(org, state)] += 1

    def as_dict(self) -> dict:
        return {
            "requests": self.requests, "errors": self.errors, "empty_responses": self.empty,
            "vehicles_seen": len(self.vehicles),
            "coord_state": {f"{o} {s}": n for (o, s), n in sorted(self.coord.items())},
        }


def main() -> int:
    ap = argparse.ArgumentParser(description="아산시 BIS 실시간 위치 녹화기")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--series", help="계열 번호 1~9 (예: 9 = 900번대 + 90번대)")
    grp.add_argument("--routes", help="쉼표로 구분한 노선명 또는 route_id")
    ap.add_argument("--start", help="시작 시각 HH:MM (KST). 그때까지 기다린다")
    ap.add_argument("--until", help="종료 시각 HH:MM (KST)")
    ap.add_argument("--duration", type=float, help="녹화 시간(분). --until과 둘 중 하나")
    ap.add_argument("--interval", type=float, default=15.0, help="차량이 있는 노선의 호출 간격(초)")
    ap.add_argument("--idle-interval", type=float, default=60.0, help="차량이 없는 노선의 호출 간격(초)")
    ap.add_argument("--min-gap", type=float, default=0.5, help="연속한 두 호출 사이 최소 간격(초)")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--no-route-maps", action="store_true", help="routes.json(정류소·선형)을 받지 않음")
    ap.add_argument("--out-dir", help="출력 폴더 (기본 data/realtime/<날짜>_s<계열>)")
    args = ap.parse_args()
    if not args.until and not args.duration:
        ap.error("--until 또는 --duration 중 하나가 필요합니다")
    if args.interval < 3 or args.min_gap < 0.2:
        ap.error("--interval은 3초 이상, --min-gap은 0.2초 이상으로 두세요 (공식 화면이 노선당 3초)")

    today = now_kst()
    start_at = parse_hhmm(args.start, today) if args.start else today
    tag = f"s{args.series}" if args.series else "routes"
    # 실행마다 다른 폴더: 같은 날 같은 대상으로 두 번 돌려도 loc.jsonl이 섞이거나 meta.json이 덮이지 않게
    out_dir = Path(args.out_dir or Path("data") / "realtime" / f"{start_at:%Y-%m-%d_%H%M}_{tag}")
    if out_dir.exists() and any(out_dir.iterdir()):
        ap.error(f"출력 폴더가 이미 있습니다: {out_dir} (--out-dir로 다른 폴더를 지정하세요)")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.start:
        wait = (start_at - now_kst()).total_seconds()
        if wait > 0:
            print(f"{start_at:%H:%M}까지 {wait / 60:.0f}분 기다립니다 (Ctrl+C로 중단)")
            time.sleep(wait)

    print("노선 목록을 받는 중...")
    listing = [compact(r) for r in post("searchBusRoute_1", {"busRouteType": ""}, args.timeout)
               .get("busRouteList", [])]
    targets = select_targets(listing, args.series, args.routes)
    if not targets:
        print("대상 노선이 없습니다. --series / --routes 값을 확인하세요.", file=sys.stderr)
        return 1
    print(f"대상 {len(targets)}개 route_id: " + ", ".join(
        f"{t.get('route_name')}({t.get('route_direction', '?')})" for t in targets))

    if not args.no_route_maps:
        print("정류소·선형 스냅샷을 받는 중...")
        maps = snapshot_route_maps(targets, args.timeout, delay=1.0)
        with open(out_dir / "routes.json", "w", encoding="utf-8") as fp:
            json.dump({"fetched_at": now_kst().isoformat(timespec="seconds"), "routes": maps},
                      fp, ensure_ascii=False, separators=(",", ":"))

    started = now_kst()
    end_at = parse_hhmm(args.until, started) if args.until else started + timedelta(minutes=args.duration)
    if end_at <= started:
        print(f"종료 시각 {end_at:%H:%M}이 이미 지났습니다.", file=sys.stderr)
        return 1
    span = (end_at - started).total_seconds()
    est = len(targets) * span / args.interval
    print(f"녹화: {started:%H:%M:%S} ~ {end_at:%H:%M:%S}, 최대 약 {est:,.0f}회 호출 "
          f"(차량 없는 노선은 {args.idle_interval:.0f}초 간격이라 실제로는 더 적음)")

    meta = {
        "started_at": started.isoformat(timespec="seconds"),
        "planned_end": end_at.isoformat(timespec="seconds"),
        "args": vars(args),
        "targets": [{k: t.get(k) for k in ("route_id", "route_name", "route_type", "route_direction",
                                           "st_stop_name", "ed_stop_name")} for t in targets],
    }

    # 노선별 다음 호출 시각. 시작 시 고르게 흩어 둔다.
    mono0 = time.monotonic()
    due = {t["route_id"]: mono0 + i * args.min_gap for i, t in enumerate(targets)}
    fails = Counter()
    stats = Stats()
    last_req = 0.0
    end_mono = mono0 + span

    with open(out_dir / "loc.jsonl", "a", encoding="utf-8") as out:
        try:
            while True:
                rid = min(due, key=due.get)
                fire = max(due[rid], last_req + args.min_gap)
                if fire >= end_mono:
                    break
                time.sleep(max(0.0, fire - time.monotonic()))
                last_req = time.monotonic()
                ts = now_kst().isoformat(timespec="milliseconds")
                try:
                    raw = post("searchBusRealLocationDetail", {"busRouteId": rid}, args.timeout)
                    vehicles = [compact(v) for v in raw.get("busRealLocList", [])]
                    line = {"ts": ts, "route_id": rid,
                            "ms": round((time.monotonic() - last_req) * 1000), "vehicles": vehicles}
                    stats.add(vehicles)
                    fails[rid] = 0
                    due[rid] = last_req + (args.interval if vehicles else args.idle_interval)
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as err:
                    line = {"ts": ts, "route_id": rid, "error": str(err)}
                    stats.errors += 1
                    fails[rid] += 1
                    due[rid] = last_req + min(args.interval * 2 ** fails[rid], 300.0)
                out.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
                out.flush()
                if stats.requests and (stats.requests + stats.errors) % 200 == 0:
                    print(f"[{now_kst():%H:%M:%S}] 호출 {stats.requests + stats.errors}회, "
                          f"오류 {stats.errors}, 차량 {len(stats.vehicles)}대")
        except KeyboardInterrupt:
            print("\n중단됨. 지금까지 받은 것을 저장합니다.")

    meta["ended_at"] = now_kst().isoformat(timespec="seconds")
    meta["stats"] = stats.as_dict()
    with open(out_dir / "meta.json", "w", encoding="utf-8") as fp:
        json.dump(meta, fp, ensure_ascii=False, indent=1)
    print(json.dumps(meta["stats"], ensure_ascii=False, indent=1))
    print(f"저장: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
