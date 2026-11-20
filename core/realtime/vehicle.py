"""차량 소속 판정 (core/realtime/vehicle.py).

실시간 위치 API의 veh_id·plate_no로 차량의 지역(아산/천안)과 운수사를 판정한다.

- 지역은 번호판 지역 기호가 기준이다: 충남72자 = 아산 면허, 충남70자 = 천안 면허.
  번호판이 비었거나 형식이 다르면 veh_id 앞 3자리(288 = 아산, 285 = 천안)로 대신한다.
  둘이 서로 다르면 conflict=True로 표시한다(지역은 번호판 쪽을 따른다).
- 운수사는 번호판 숫자 4자리(= veh_id 끝 4자리) 범위로 정한다. 범위 표는 버스 동호인
  자료와 사용자 제공 정보에 근거한 관례이며 공식 자료가 아니다. 범위 밖이면 company=None.
- 지역 판정은 좌표 신뢰도에 쓴다: 천안 차량은 lng 자리에 lat이 복사되거나,
  정류소를 지날 때 정류소 좌표가 들어오는 것으로 보인다(2026-11-20 HAR 관찰, 녹화본으로 검증 예정).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

REGION_BY_PLATE = {"72": "asan", "70": "cheonan"}
REGION_BY_PREFIX = {"288": "asan", "285": "cheonan"}

# (지역, 번호 시작, 번호 끝, 운수사, 비고)
COMPANY_RANGES = [
    ("asan", 1000, 1099, "온양교통", ""),
    ("asan", 1100, 1199, "아산여객", ""),
    ("asan", 2000, 2999, "온양교통", "증차분"),
    ("cheonan", 1200, 1399, "보성여객", ""),
    ("cheonan", 1400, 1599, "삼안여객", ""),
    ("cheonan", 1600, 1799, "새천안교통", ""),
]

_PLATE_RE = re.compile(r"충남\s*(\d{2})\s*[가-힣]\s*(\d{4})")


@dataclass(frozen=True)
class VehicleInfo:
    veh_id: str
    number: int | None        # 번호판 숫자 4자리
    region: str | None        # "asan" | "cheonan" | None
    company: str | None       # 운수사 (범위 밖이면 None)
    note: str = ""            # 예: "증차분"
    conflict: bool = False    # 번호판 지역과 veh_id 앞자리 지역이 다름

    @property
    def gps_trusted(self) -> bool:
        """좌표를 GPS로 써 볼 가치가 있는 차량인지. 실제 사용 여부는 정합성 검사가 정한다."""
        return self.region == "asan"


def classify(veh_id: str, plate_no: str = "") -> VehicleInfo:
    veh_id = (veh_id or "").strip()
    plate_region = number = None
    m = _PLATE_RE.search(plate_no or "")
    if m:
        plate_region = REGION_BY_PLATE.get(m.group(1))
        number = int(m.group(2))
    prefix_region = REGION_BY_PREFIX.get(veh_id[:3])
    if number is None and len(veh_id) >= 4 and veh_id[-4:].isdigit():
        number = int(veh_id[-4:])

    region = plate_region or prefix_region
    conflict = bool(plate_region and prefix_region and plate_region != prefix_region)

    company, note = None, ""
    if region and number is not None:
        for reg, lo, hi, name, remark in COMPANY_RANGES:
            if reg == region and lo <= number <= hi:
                company, note = name, remark
                break
    return VehicleInfo(veh_id, number, region, company, note, conflict)


if __name__ == "__main__":  # 2026-11-20 HAR(900번 하행)에서 본 차량
    for vid, plate in [("285041697", "충남70자1697"), ("288041015", "충남72자1015"),
                       ("288041071", "충남72자1071"), ("285041657", "충남70자1657"),
                       ("285041712", "충남70자1712"), ("288042003", ""), ("285041900", "충남70자1900")]:
        print(classify(vid, plate))
