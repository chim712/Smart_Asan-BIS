#!/usr/bin/env python3
"""아산시 버스정보시스템 정적 데이터 크롤러 (① 수집).

노선 목록 -> 계열 필터 -> route_id별 노선 상세(정류소 순서, 선형)와 시간표를 받아
JSON 파일 하나로 저장한다. 표준 라이브러리 + core.config(python-dotenv)만 쓴다.

실패 처리:
  - 요청 하나는 post()가 간격을 늘려 가며 3회까지 재시도한다.
  - 전체 순회가 끝나면 실패한 노선만 --retry-wait 초 뒤 한 번 더 받는다.
  - 그래도 남은 실패는 meta.errors에 남는다. ③ 빌더가 이를 치명적 오류로 보고 ④ 적재를 막는다.
  - 나중에 --retry <raw.json>으로 그 파일의 실패 노선만 다시 받아 같은 파일을 갱신할 수 있다.

프로젝트 루트에서 모듈로 실행한다.
    python -m core.crawl.crawl_asan --series all          # data/snapshots/<오늘>/raw.json
    python -m core.crawl.crawl_asan --series 6            # 600번대 + 60번대 → raw_series_6.json
    python -m core.crawl.crawl_asan --series all --out data/snapshots/2026-12-01/raw.json
    python -m core.crawl.crawl_asan --retry data/snapshots/2026-12-01/raw.json
    python -m core.crawl.crawl_asan --no-shape            # 지도 선형 좌표 제외 (파일 크기 축소)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from core.config import SNAPSHOT_DIR
from core.groups import default_group

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


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def series_label(route_name: str) -> str | None:
    """--series 필터용 계열. 기본 계열이 X(한 자리, 1000번대, 순환, 심야)면 None."""
    g = default_group(route_name)
    return None if g == "X" else g


def select_routes(all_routes: list[dict], series: str) -> list[dict]:
    routes = [r for r in all_routes if r.get("route_type") not in EXCLUDED_ROUTE_TYPES]
    if series != "all":
        routes = [r for r in routes if series_label(r.get("route_name", "")) == series]
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


def fetch_route(info: dict, args, label: str) -> tuple[dict, str | None]:
    """노선 하나를 받는다 → (entry, 오류 메시지 또는 None). 실패해도 entry는 돌려준다."""
    route_id = info["route_id"]
    entry = {"info": info, "series": series_label(info.get("route_name", ""))}
    try:
        time.sleep(args.delay)
        raw_map = post("searchBusRouteMap", {"busRouteId": route_id}, timeout=args.timeout)
        entry.update(parse_route_map(raw_map, include_shape=not args.no_shape))

        time.sleep(args.delay)
        raw_time = post("searchBusTimeList", {"busRouteId": route_id, "weekcd": args.weekcd},
                        timeout=args.timeout)
        entry["timetable"] = parse_timetable(raw_time)
        print(f"{label}: 정류소 {len(entry['stops'])}개, 시간표 {len(entry['timetable'])}회")
        return entry, None
    except Exception as err:  # 한 노선의 실패가 전체를 멈추지 않게 한다
        print(f"{label}: 실패 - {err}", file=sys.stderr)
        return entry, str(err)


def retry_failed(result: dict, args, wait: float) -> None:
    """result['meta']['errors']의 노선만 다시 받아 routes 항목을 갈아 끼운다(제자리 수정)."""
    meta = result["meta"]
    failed = {e["route_id"] for e in meta.get("errors", [])}
    if not failed:
        return
    if wait > 0:
        print(f"\n실패 {len(failed)}건 재시도: {wait:.0f}초 대기")
        time.sleep(wait)
    index = {r["info"]["route_id"]: i for i, r in enumerate(result["routes"])}
    errors, recovered = [], []
    for n, rid in enumerate(sorted(failed), start=1):
        if rid not in index:
            errors.append({"route_id": rid, "error": "routes에 항목 없음"})
            continue
        info = result["routes"][index[rid]]["info"]
        label = f"[재시도 {n}/{len(failed)}] {info.get('route_name', '?')} ({rid})"
        entry, err = fetch_route(info, args, label)
        result["routes"][index[rid]] = entry
        if err:
            errors.append({"route_id": rid, "error": err})
        else:
            recovered.append(rid)
    meta["errors"] = errors
    meta.setdefault("retry_log", []).append(
        {"at": now_utc(), "tried": sorted(failed), "recovered": recovered,
         "still_failed": [e["route_id"] for e in errors]})


def default_out(series: str) -> Path:
    """data/snapshots/<오늘>/raw.json. 이미 있으면 <오늘>_2, _3 … 새 폴더를 쓴다."""
    name = "raw.json" if series == "all" else f"raw_series_{series}.json"
    today = date.today().isoformat()
    folder, n = SNAPSHOT_DIR / today, 1
    while (folder / name).exists():
        n += 1
        folder = SNAPSHOT_DIR / f"{today}_{n}"
    return folder / name


def write_json(path: Path, obj: dict) -> None:
    """폴더를 만들고 임시 파일에 쓴 뒤 바꿔 끼운다(중간에 죽어도 기존 파일이 깨지지 않게)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(obj, fp, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="아산시 버스 정적 데이터 크롤러")
    parser.add_argument("--series", default="4",
                        help="계열 번호 1~9 또는 all (기본 4: 400번대 + 40번대)")
    parser.add_argument("--delay", type=float, default=1.0, help="요청 사이 대기 시간(초). 줄이지 않는다")
    parser.add_argument("--timeout", type=float, default=20.0, help="요청 제한 시간(초)")
    parser.add_argument("--weekcd", default="1", help="시간표 weekcd 값 (기본 1)")
    parser.add_argument("--no-shape", action="store_true", help="지도 선형 좌표를 저장하지 않음")
    parser.add_argument("--retry-wait", type=float, default=60.0,
                        help="순회 후 실패 노선 재시도 전 대기(초). 음수면 재시도하지 않음")
    parser.add_argument("--retry", metavar="RAW_JSON", default=None,
                        help="기존 수집 파일의 meta.errors 노선만 다시 받아 그 파일을 갱신")
    parser.add_argument("--out", default=None,
                        help="출력 파일 경로 (기본 data/snapshots/<오늘>/raw.json, 있으면 <오늘>_2 …)")
    args = parser.parse_args()

    if args.retry:
        path = Path(args.retry)
        with open(path, encoding="utf-8") as fp:
            result = json.load(fp)
        before = len(result["meta"].get("errors", []))
        if not before:
            print(f"{path}: 실패 노선이 없습니다.")
            return 0
        args.weekcd = result["meta"].get("weekcd", args.weekcd)
        args.no_shape = not any(r.get("shape") for r in result["routes"])
        retry_failed(result, args, wait=0)
        write_json(path, result)
        after = len(result["meta"]["errors"])
        print(f"\n갱신 완료: {path} (실패 {before}건 → {after}건)")
        return 0 if not after else 2

    out_path = Path(args.out) if args.out else default_out(args.series)

    print("노선 목록을 받는 중...")
    listing = post("searchBusRoute_1", {"busRouteType": ""}, timeout=args.timeout)
    all_routes = [clean(r) for r in listing.get("busRouteList", [])]
    targets = select_routes(all_routes, args.series)
    print(f"전체 {len(all_routes)}건 중 대상 {len(targets)}건 → {out_path}")
    if not targets:
        print("대상 노선이 없습니다. --series 값을 확인하세요.", file=sys.stderr)
        return 1

    routes, errors = [], []
    for index, info in enumerate(targets, start=1):
        label = f"[{index}/{len(targets)}] {info.get('route_name', '?')} ({info['route_id']})"
        entry, err = fetch_route(info, args, label)
        if err:
            errors.append({"route_id": info["route_id"], "error": err})
        routes.append(entry)

    result = {
        "meta": {
            "source": BASE,
            "crawled_at": now_utc(),
            "series": args.series,
            "weekcd": args.weekcd,
            "route_count": len(routes),
            "errors": errors,
        },
        "routes": routes,
    }
    if errors and args.retry_wait >= 0:
        retry_failed(result, args, wait=args.retry_wait)
    write_json(out_path, result)

    left = len(result["meta"]["errors"])
    print(f"\n저장 완료: {out_path} (노선 {len(routes)}건, 실패 {left}건)")
    if left:
        print(f"남은 실패는 다시 받을 수 있습니다: python -m core.crawl.crawl_asan --retry {out_path}",
              file=sys.stderr)
    return 0 if not left else 2


if __name__ == "__main__":
    sys.exit(main())
