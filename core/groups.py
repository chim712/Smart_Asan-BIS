"""노선 계열 기본값과 표시 정보.

번호 규칙은 기본값일 뿐이며 DB의 route_group_override(관리자 입력)가 우선한다.
GROUPS는 core/db/schema.sql 의 route_group_def 초기값과 같게 유지한다.
"""
from __future__ import annotations

import re

# code: (label, color)
GROUPS: dict[str, tuple[str, str]] = {
    "1": ("100번대 송악", "#008300"),
    "2": ("200번대 성환", "#e87ba4"),
    "3": ("300번대 신창·선장", "#1baf7a"),
    "4": ("400번대 신창·도고", "#2a78d6"),
    "5": ("500번대 평택", "#eda100"),
    "6": ("600번대 염치·영인·인주", "#6250d6"),
    "7": ("700번대 탕정", "#eb6834"),
    "8": ("800번대 배방", "#e34948"),
    "9": ("900번대 천안", "#8a5a2b"),
    "X": ("기타", "#8c8c8c"),
}


def default_group(route_name: str) -> str:
    """노선번호의 기본 계열.

    100~999 → 백의 자리, 10~99 → 십의 자리, 가지번호(62-3)는 '-' 앞 번호 기준.
    한 자리 수, 1000 이상, 숫자가 아닌 이름(순환5, 심야1)은 'X'.
    """
    m = re.fullmatch(r"(\d+)(?:-\d+)?", route_name.strip())
    if not m:
        return "X"
    n = int(m.group(1))
    if 100 <= n <= 999:
        return str(n // 100)
    if 10 <= n <= 99:
        return str(n // 10)
    return "X"
