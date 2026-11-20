"""운수사 차량목록 txt → 차량 JSON + DB 시드 SQL (core/fleet/parse_fleet.py).

원문: 버스 동호인 블로그의 회사별 차량현황을 그대로 저장한 txt(파일명에 회사명 포함).
표준 라이브러리만 쓴다. 프로젝트 루트에서:

    python -m core.fleet.parse_fleet data/fleet/src --out data/fleet/fleet.json --sql data/fleet/vehicle_seed.sql

규칙 (vehicle_db_design.md 4장)
- 차량 줄: "NNNN YY.M 모델 [패찰연식 YYYY.M] (YYYY.M 면허등록[...], 비고) (비고)"
- 섹션: "…차량/…코스 … (N대)" 제목 → 그 아래 차량. 노선 줄("830번 …") 뒤 "(N대)"로 끝나는 줄 → 고정배차 묶음.
- 같은 번호가 두 번 나오면 합친다(보성 1348·1349). 모르는 모델이 있으면 실패한다.
- 차량대수·연료별 대수를 원문 요약과 비교해 경고한다. "※ … 차량현황"부터는 요약으로 보고 차량을 읽지 않는다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent

VEH_RE = re.compile(r"^(\d{4})\s+(\d{2})\.(\d{1,2})\s+(.+)$")
ROUTE_RE = re.compile(r"^(\d+)번")
GROUP_END_RE = re.compile(r"\(([^()]*?)\d+대\)\s*$")          # "(1대)", "(전기 저상버스 4대)", "… (1대)"
PLATE_YM_RE = re.compile(r"\[패찰연식\s*(\d{4})\.(\d{1,2})\]")
REG_RE = re.compile(r"(\d{4})\.(\d{1,2})\s*면허등록")
COUNT_RE = re.compile(r"차량대수\s*:\s*(\d+)\s*대")
UPDATED_RE = re.compile(r"최종수정일\s*:\s*(\d{4})\.(\d{1,2})\.(\d{1,2})")
FUEL_SUMMARY_RE = re.compile(r"(천연가스|전기|디젤|수소전기)\s*:\s*(\d+)\s*대")
FUEL_SUMMARY_MAP = {"천연가스": "cng", "전기": "ev", "디젤": "diesel", "수소전기": "fcev"}

# 연료: 위에서부터 처음 맞는 것
FUEL_RULES = [
    (re.compile(r"FCEV"), "fcev"),
    (re.compile(r"(?<![A-Z])P?EV(?![A-Z])"), "ev"),
    (re.compile(r"CNG|NGV"), "cng"),
    (re.compile(r"디젤"), "diesel"),
]
FUEL_LABEL = {"fcev": "FCEV", "ev": "PEV", "cng": "CNG", "diesel": "디젤"}


def load_json(name: str) -> dict:
    with open(HERE / name, encoding="utf-8") as f:
        return json.load(f)


def ym(y: str, m: str, two_digit: bool = False) -> str:
    year = 2000 + int(y) if two_digit else int(y)
    return f"{year:04d}-{int(m):02d}"


def split_parens(text: str) -> tuple[str, list[str]]:
    """맨 앞 '(' 전까지 = 모델, 그 뒤 최상위 괄호 묶음들의 내용 목록."""
    head, groups, depth, buf = [], [], 0, []
    for ch in text:
        if ch == "(":
            if depth == 0:
                buf = []
            else:
                buf.append(ch)
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
            if depth == 0:
                groups.append("".join(buf).strip())
            else:
                buf.append(ch)
        elif depth > 0:
            buf.append(ch)
        elif not groups:
            head.append(ch)
    return "".join(head).strip(), groups


class Fleet:
    def __init__(self):
        self.companies = load_json("companies.json")
        self.families = [dict(f, rx=re.compile(f["match"])) for f in load_json("models.json")["families"]]
        self.errors: list[str] = []
        self.warnings: list[str] = []

    # ---------- 모델 ----------
    def classify_model(self, model: str) -> tuple[dict | None, str | None]:
        fam = next((f for f in self.families if f["rx"].search(model)), None)
        fuel = next((v for rx, v in FUEL_RULES if rx.search(model)), None)
        return fam, fuel

    # ---------- 파일 하나 ----------
    def parse_file(self, path: Path) -> dict:
        comp = next((c for c in self.companies["companies"] if c["file_keyword"] in path.name), None)
        if comp is None:
            raise SystemExit(f"회사를 알 수 없는 파일: {path.name}")
        text = path.read_text(encoding="utf-8")
        lines = [ln.replace("​", "").replace("﻿", "").strip() for ln in text.splitlines()]

        vehicles: dict[str, dict] = {}
        label, duty = "", "regular"
        header_active = False          # 섹션 제목 아래(노선 줄이 와도 제목 유지)
        pending_routes: list[str] = []
        last_was_vehicle = False
        in_footer = False
        declared = updated = None
        fuel_declared: dict[str, int] = {}

        for ln in lines:
            if not ln:
                continue
            if ln.startswith("※") and "차량현황" in ln:
                in_footer = True
            m = COUNT_RE.search(ln)
            if m and in_footer:
                declared = int(m.group(1))
            m = UPDATED_RE.search(ln)
            if m:
                updated = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if in_footer:
                for name, n in FUEL_SUMMARY_RE.findall(ln):
                    fuel_declared[FUEL_SUMMARY_MAP[name]] = int(n)
                continue

            m = VEH_RE.match(ln)
            if m:
                self._add_vehicle(vehicles, comp, m, label or "(구분 없음)", duty, path.name)
                last_was_vehicle = True
                continue
            if ln.startswith("※"):
                continue

            m = ROUTE_RE.match(ln)
            if m:
                if last_was_vehicle:
                    header_active, pending_routes = False, []
                if not header_active and m.group(1) not in pending_routes:
                    pending_routes.append(m.group(1))
                last_was_vehicle = False
                continue

            if ln.startswith("("):
                if GROUP_END_RE.search(ln) and not header_active and pending_routes:
                    label, duty = "/".join(pending_routes) + "번", "fixed"
                last_was_vehicle = False
                continue

            if "차량" in ln or "코스" in ln:          # 섹션 제목
                name = re.split(r"[(※]", ln)[0].strip()
                name = re.sub(r"\s*(운행차량|차량)$", "", name).strip() or name
                label = name
                duty = ("spare" if "예비" in ln else "idle" if "휴차" in ln
                        else "call" if "콜버스" in ln else "regular")
                header_active, pending_routes, last_was_vehicle = True, [], False

        if updated is None:
            self.errors.append(f"{path.name}: 최종수정일을 찾지 못함")
        if declared is not None and declared != len(vehicles):
            self.warnings.append(f"{comp['name']}: 원문 차량대수 {declared}대, 파싱 {len(vehicles)}대")
        if fuel_declared:
            got = Counter(v["fuel"] for v in vehicles.values())
            diff = {k: (fuel_declared.get(k, 0), got.get(k, 0))
                    for k in set(fuel_declared) | set(got) if fuel_declared.get(k, 0) != got.get(k, 0)}
            if diff:
                self.warnings.append(f"{comp['name']}: 연료별 대수 (원문, 파싱) 차이 {diff}")
        for v in vehicles.values():
            v["source_date"] = updated.isoformat() if updated else None
        return {"company": comp["code"], "source_file": path.name,
                "source_date": updated.isoformat() if updated else None,
                "declared_count": declared, "vehicles": list(vehicles.values())}

    def _add_vehicle(self, vehicles, comp, m, label, duty, fname):
        num, yy, mm, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        plate_ym = None
        pm = PLATE_YM_RE.search(rest)
        if pm:
            plate_ym = ym(pm.group(1), pm.group(2))
        rest_clean = PLATE_YM_RE.sub("", rest).strip()
        model, groups = split_parens(rest_clean)
        registered, notes = None, []
        for g in groups:
            rm = REG_RE.search(g)
            if rm:
                registered = ym(rm.group(1), rm.group(2))
                g = REG_RE.sub("", g)
            g = g.strip(" ,")
            if g:
                notes.append(g)

        fam, fuel = self.classify_model(model)
        if fam is None or fuel is None:
            self.errors.append(f"{fname} {num}: 모델/연료 판정 실패 '{model}'")
            return
        key = f"{comp['plate_region']}-{num}"
        rec = vehicles.get(key)
        if rec:                                    # 같은 차가 두 섹션에 나옴
            if label not in rec["courses"]:
                rec["courses"].append(label)
            if rec["duty"] == "fixed" and duty != "fixed":
                rec["duty"] = duty
            for n in notes:
                if n not in rec["notes"]:
                    rec["notes"].append(n)
            return
        vehicles[key] = {
            "plate_key": key,
            "plate_no": f"충남{comp['plate_region']}자{num}",
            "number": int(num),
            "company": comp["code"],
            "model_raw": model,
            "model_family": fam["family"],
            "model_code": f"{fam['slug']}_{fuel}",
            "fuel": fuel,
            "built_ym": ym(yy, mm, two_digit=True),
            "registered_ym": registered,
            "plate_ym": plate_ym,
            "duty": duty,
            "courses": [label],
            "notes": notes,
        }

    # ---------- 전체 ----------
    def run(self, src: Path) -> dict:
        files = sorted(p for p in src.glob("*.txt"))
        if not files:
            raise SystemExit(f"txt 파일이 없음: {src}")
        per_company = [self.parse_file(p) for p in files]
        seen: dict[str, str] = {}
        ranges = self.companies["ranges"]
        for pc in per_company:
            for v in pc["vehicles"]:
                if v["plate_key"] in seen:
                    self.errors.append(f"번호 중복: {v['plate_key']} ({seen[v['plate_key']]}, {pc['company']})")
                seen[v["plate_key"]] = pc["company"]
                pr = v["plate_key"][:2]
                block = next((r for r in ranges if r["plate_region"] == pr and r["from"] <= v["number"] <= r["to"]), None)
                if block is None or block["company"] != v["company"]:
                    self.warnings.append(f"{v['plate_key']}: 번호 블록과 소속({v['company']})이 다름")
        models = {}
        fam_by_name = {f["family"]: f for f in self.families}
        for pc in per_company:
            for v in pc["vehicles"]:
                if v["model_code"] in models:
                    continue
                f = fam_by_name[v["model_family"]]
                low = f["low_floor"]
                if f["slug"] == "county_breeze":           # 카운티 전기도 고상
                    low = False
                models[v["model_code"]] = {
                    "model_code": v["model_code"], "model_family": f["family"],
                    "model_name": f"{f['family']} {FUEL_LABEL[v['fuel']]}", "maker": f["maker"],
                    "size_class": f["size"], "fuel": v["fuel"], "low_floor": low,
                }
        return {"generated_at": date.today().isoformat(),
                "companies": per_company,
                "models": sorted(models.values(), key=lambda m: m["model_code"]),
                "errors": self.errors, "warnings": self.warnings}


# ---------- 아이콘 (FastAPI에서도 같은 함수를 쓴다) ----------
def pick_icon(family: str, fuel: str, built_year: int | None, size: str, rules: dict) -> str:
    best, best_score = None, -1
    for r in rules["rules"]:
        if r["family"] != family:
            continue
        if r["fuel"] is not None and r["fuel"] != fuel:
            continue
        if built_year is not None:
            if r["year_from"] is not None and built_year < r["year_from"]:
                continue
            if r["year_to"] is not None and built_year > r["year_to"]:
                continue
        elif r["year_from"] is not None or r["year_to"] is not None:
            continue
        score = sum(r[k] is not None for k in ("fuel", "year_from", "year_to"))
        if score > best_score:                      # 같으면 앞(rule_id 작은 것) 유지
            best, best_score = r, score
    return best["icon"] if best else rules["size_default"].get(size, "greencity_aerocity")


# ---------- SQL ----------
def q(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def to_sql(data: dict, companies: dict, icon_rules: dict) -> str:
    out = ["-- Smart Asan BIS 차량 시드 (parse_fleet.py 생성, 직접 고치지 말 것)",
           f"-- 생성일 {data['generated_at']}. 원문 기준일: " +
           ", ".join(f"{c['company']} {c['source_date']}" for c in data["companies"]),
           "-- 초기 적재용: 이 파일에 있는 회사의 차량 이력을 비우고 다시 넣는다.",
           "-- 이후 갱신은 load_fleet.py(이력 비교, 예정)로 한다.",
           "USE asan_bis;", "SET NAMES utf8mb4;", "START TRANSACTION;", ""]

    out.append("-- 운수사")
    for c in companies["companies"]:
        out.append("INSERT INTO company (company_code, name, region, plate_region, sort_order) VALUES "
                   f"({q(c['code'])}, {q(c['name'])}, {q(c['region'])}, {q(c['plate_region'])}, {c['sort']}) "
                   "ON DUPLICATE KEY UPDATE name=VALUES(name), region=VALUES(region), "
                   "plate_region=VALUES(plate_region), sort_order=VALUES(sort_order);")
    out += ["", "-- 번호판 블록", "DELETE FROM company_plate_range;"]
    out.append("INSERT INTO company_plate_range (plate_region, num_from, num_to, company_code, note) VALUES\n  " +
               ",\n  ".join(f"({q(r['plate_region'])}, {r['from']}, {r['to']}, {q(r['company'])}, {q(r['note'] or None)})"
                            for r in companies["ranges"]) + ";")

    out += ["", "-- 차종"]
    for m in data["models"]:
        out.append("INSERT INTO vehicle_model (model_code, model_family, model_name, maker, size_class, fuel, low_floor) VALUES "
                   f"({q(m['model_code'])}, {q(m['model_family'])}, {q(m['model_name'])}, {q(m['maker'])}, "
                   f"{q(m['size_class'])}, {q(m['fuel'])}, {q(m['low_floor'])}) "
                   "ON DUPLICATE KEY UPDATE model_family=VALUES(model_family), model_name=VALUES(model_name), "
                   "maker=VALUES(maker), size_class=VALUES(size_class), fuel=VALUES(fuel), low_floor=VALUES(low_floor);")

    out += ["", "-- 아이콘 규칙", "DELETE FROM vehicle_icon_rule;"]
    out.append("INSERT INTO vehicle_icon_rule (rule_id, model_family, fuel, year_from, year_to, icon_key, note) VALUES\n  " +
               ",\n  ".join(f"({r['rule_id']}, {q(r['family'])}, {q(r['fuel'])}, {q(r['year_from'])}, "
                            f"{q(r['year_to'])}, {q(r['icon'])}, {q(r['note'] or None)})"
                            for r in icon_rules["rules"]) + ";")

    out += ["", "-- 차량 등록"]
    codes = ", ".join(q(c["company"]) for c in data["companies"])
    out.append(f"DELETE FROM vehicle_registration WHERE company_code IN ({codes});")
    for c in data["companies"]:
        out.append(f"-- {c['company']} ({len(c['vehicles'])}대, 원문 {c['source_file']})")
        rows = []
        for v in sorted(c["vehicles"], key=lambda v: v["number"]):
            rows.append("(" + ", ".join([
                q(v["plate_key"]), q(v["source_date"]), "NULL", q(v["plate_no"]), q(v["company"]),
                q(v["model_code"]), q(v["model_raw"]), q(v["built_ym"]), q(v["registered_ym"]),
                q(v["plate_ym"]), q(v["duty"]), q(" | ".join(v["courses"])),
                q("; ".join(v["notes"]) or None), q(v["source_date"])]) + ")")
        out.append("INSERT INTO vehicle_registration (plate_key, valid_from, valid_to, plate_no, company_code, "
                   "model_code, model_raw, built_ym, registered_ym, plate_ym, duty, courses, note, source_date) VALUES\n  "
                   + ",\n  ".join(rows) + ";")
    out += ["", "COMMIT;", ""]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="차량목록 txt → JSON/SQL")
    ap.add_argument("src", type=Path, help="txt 폴더")
    ap.add_argument("--out", type=Path, required=True, help="JSON 출력")
    ap.add_argument("--sql", type=Path, help="시드 SQL 출력")
    a = ap.parse_args(argv)

    fleet = Fleet()
    data = fleet.run(a.src)
    icon_rules = load_json("icon_rules.json")
    fam_size = {m["model_family"]: m["size_class"] for m in data["models"]}
    for c in data["companies"]:
        for v in c["vehicles"]:
            v["icon_key"] = pick_icon(v["model_family"], v["fuel"], int(v["built_ym"][:4]),
                                      fam_size[v["model_family"]], icon_rules)
            v["icon_fallback"] = not any(r["family"] == v["model_family"] for r in icon_rules["rules"])

    a.out.parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    total = sum(len(c["vehicles"]) for c in data["companies"])
    print(f"차량 {total}대, 차종 {len(data['models'])}개")
    for c in data["companies"]:
        print(f"  {c['company']:<11} {len(c['vehicles']):>4}대 (원문 {c['declared_count']}대, 기준일 {c['source_date']})")
    for w in data["warnings"]:
        print("경고:", w)
    if data["errors"]:
        for e in data["errors"]:
            print("오류:", e, file=sys.stderr)
        sys.exit(1)
    if a.sql:
        with open(a.sql, "w", encoding="utf-8", newline="\n") as f:
            f.write(to_sql(data, fleet.companies, icon_rules))
        print("SQL:", a.sql)


if __name__ == "__main__":
    main()
