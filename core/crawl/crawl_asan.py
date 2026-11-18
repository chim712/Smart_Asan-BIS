#!/usr/bin/env python3
"""아산시 버스정보시스템 정적 데이터 크롤러 (계열 단위).

노선 목록 -> 계열 필터 -> route_id별 노선 상세(정류소 순서, 선형)와 시간표를 받아
JSON 파일 하나로 저장한다. 표준 라이브러리만 사용한다 (Python 3.9+).

사용 예:
    python3 crawl_asan.py                 # 400번대 + 40번대 (기본)
    python3 crawl_asan.py --series 6      # 600번대 + 60번대
    python3 crawl_asan.py --series all    # 천안 노선을 뺀 전체
    python3 crawl_asan.py --no-shape      # 지도 선형 좌표 제외 (파일 크기 축소)
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
from datetime import datetime, timezone

BASE = "https://bus.asan.go.kr/mobile/traffic/"
HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://bus.asan.go.kr",
    "Referer": "https://bus.asan.go.kr/web/traffic_total_search",
    "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) asan-bus-toy-crawler/0.1",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}
EXCLUDED_ROUTE_TYPES = {"13"}  # 천안시 노선


def post(endpoint: str, params: dict, *, timeout: float, retries: int = 3) -> dict:
    """POST 요청 후 JSON을 반환한다. 실패하면 간격을 늘려 가며 재시도한다."""
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(BASE + endpoint, data=body, headers=HEADERS, method="POST")
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last_err = err
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"{endpoint} {params} 실패: {last_err}")


def clean(record: dict) -> dict:
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
    """노선번호의 기본 계열을 돌려준다.

    100~999 -> 백의 자리, 10~99 -> 십의 자리, 가지번호(62-3)는 '-' 앞 번호 기준.
    한 자리 수, 1000 이상, 숫자가 아닌 이름(순환5, 심야1)은 None.
    """
    match = re.fullmatch(r"(\d+)(?:-\d+)?", route_name.strip())
    if not match:
        return None
    number = int(match.group(1))
    if 100 <= number <= 999:
        return str(number // 100)
    if 10 <= number <= 99:
        return str(number // 10)
    return None


def select_routes(all_routes: list[dict], series: str) -> list[dict]:
    routes = [r for r in all_routes if r.get("route_type") not in EXCLUDED_ROUTE_TYPES]
    if series != "all":
        routes = [r for r in routes if series_of(r.get("route_name", "")) == series]
    return sorted(routes, key=lambda r: (r.get("route_name", ""), r.get("route_id", "")))


def parse_route_map(raw: dict, include_shape: bool) -> dict:
    stops = []
    for rec in raw.get("busRouteDetailList1", []):
        if not rec.get("stop_ord"):  # 마지막 원소는 노선 요약 레코드
            continue
        stops.append({
            "ord": int(rec["stop_ord"]),
            "stop_id": rec["stop_id"],
            "service_id": rec.get("service_id", ""),
            "name": rec.get("stop_name", "").strip(),
            "lat": float(rec["lat"]) if rec.get("lat") else None,
            "lng": float(rec["lng"]) if rec.get("lng") else None,
            "turn_flag": rec.get("turn_flag", ""),
        })
    stops.sort(key=lambda s: s["ord"])
    result = {"stops": stops}

    points = raw.get("busRouteDetailMapVoList", [])
    if points:
        result["turn_ord"] = points[0].get("turn_ord", "")
    if include_shape:
        ordered = sorted(points, key=lambda p: (int(p["route_ord"]), int(p["ord"])))
        result["shape"] = [[float(p["lat"]), float(p["lng"])] for p in ordered]
    return result


def parse_timetable(raw: dict) -> list[dict]:
    rows = []
    for rec in raw.get("busTimeList", []):
        rows.append({
            "ord": int(rec["ord"]) if rec.get("ord") else None,
            "departure": rec.get("departure_time", ""),
            "weekday": rec.get("week_operation_flag", ""),
            "saturday": rec.get("sat_operation_flag", ""),
            "holiday": rec.get("holidy_operation_flag", ""),
            "vacation": rec.get("vacation_operation_flag", ""),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="아산시 버스 정적 데이터 크롤러")
    parser.add_argument("--series", default="4",
                        help="계열 번호 1~9 또는 all (기본 4: 400번대 + 40번대)")
    parser.add_argument("--delay", type=float, default=1.0, help="요청 사이 대기 시간(초)")
    parser.add_argument("--timeout", type=float, default=20.0, help="요청 제한 시간(초)")
    parser.add_argument("--weekcd", default="1", help="시간표 weekcd 값 (기본 1)")
    parser.add_argument("--no-shape", action="store_true", help="지도 선형 좌표를 저장하지 않음")
    parser.add_argument("--out", default=None, help="출력 파일 경로")
    args = parser.parse_args()

    out_path = args.out or f"asan_series_{args.series}.json"

    print("노선 목록을 받는 중...")
    listing = post("searchBusRoute_1", {"busRouteType": ""}, timeout=args.timeout)
    all_routes = [clean(r) for r in listing.get("busRouteList", [])]
    targets = select_routes(all_routes, args.series)
    print(f"전체 {len(all_routes)}건 중 대상 {len(targets)}건")
    if not targets:
        print("대상 노선이 없습니다. --series 값을 확인하세요.", file=sys.stderr)
        return 1

    routes, errors = [], []
    for index, info in enumerate(targets, start=1):
        route_id = info["route_id"]
        label = f"[{index}/{len(targets)}] {info.get('route_name', '?')} ({route_id})"
        entry = {"info": info, "series": series_of(info.get("route_name", ""))}
        try:
            time.sleep(args.delay)
            raw_map = post("searchBusRouteMap", {"busRouteId": route_id}, timeout=args.timeout)
            entry.update(parse_route_map(raw_map, include_shape=not args.no_shape))

            time.sleep(args.delay)
            raw_time = post("searchBusTimeList",
                            {"busRouteId": route_id, "weekcd": args.weekcd},
                            timeout=args.timeout)
            entry["timetable"] = parse_timetable(raw_time)
            print(f"{label}: 정류소 {len(entry['stops'])}개, 시간표 {len(entry['timetable'])}회")
        except Exception as err:  # 한 노선의 실패가 전체를 멈추지 않게 한다
            errors.append({"route_id": route_id, "error": str(err)})
            print(f"{label}: 실패 - {err}", file=sys.stderr)
        routes.append(entry)

    result = {
        "meta": {
            "source": BASE,
            "crawled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "series": args.series,
            "weekcd": args.weekcd,
            "route_count": len(routes),
            "errors": errors,
        },
        "routes": routes,
    }
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(result, fp, ensure_ascii=False, separators=(",", ":"))

    print(f"\n저장 완료: {out_path} (노선 {len(routes)}건, 실패 {len(errors)}건)")
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
