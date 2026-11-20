# Smart Asan BIS — Claude Code 작업 지침

아산시 버스정보시스템(bus.asan.go.kr) 데이터를 크롤링해, 번호가 아니라 **실제로 다니는 길(도로 그래프)** 기준으로 노선을 합쳐 보여 주는 개인 토이프로젝트다. 통합 노선도, 통합 도착정보, 실시간 위치가 목표다.

- 배포: Raspberry Pi 5 (Ubuntu 24.04), 개발·테스트: Windows
- 스택: Python 파이프라인 → MariaDB 10.11 → FastAPI → React (SVG 노선도)
- **상세 도메인 지식, 데이터 결함, 분석 결과, 설계 결정의 배경은 `docs/PROJECT_CONTEXT.md`에 있다.** 노선 체계·데이터 해석·그래프 구조에 관한 작업 전에는 해당 장을 먼저 읽는다.

## 사용자와의 소통

- 응답은 한국어 존댓말("~합니다")로 한다. 문서·주석은 "~한다"체로 쓴다.
- 코드 검토 시 **전체 흐름을 먼저**, 그다음 효율·유효성 검토와 개선에 집중한다.

## 저장소 구조

```
Smart_Asan-BIS/
├─ CLAUDE.md
├─ docs/PROJECT_CONTEXT.md     # 상세 컨텍스트 (도메인, 데이터 결함, 분석, 결정)
├─ core/                       # 파이프라인 패키지 (backend가 가져다 씀, 역방향 의존 금지)
│  ├─ config.py                # .env/환경변수 → db_params(), ROOT, DATA_DIR, SNAPSHOT_DIR
│  ├─ geo.py                   # 기하 공통 (거리·투영·dedupe·평면 좌표, cos(36.78°) 기준)
│  ├─ groups.py                # 계열 기본값 default_group(), 계열 이름·색 GROUPS
│  ├─ crawl/crawl_asan.py      # ① 수집
│  ├─ graph/fix_shape.py       # ② 선형 꼭짓점 순서 보정
│  ├─ graph/build_city_graph.py# ③ 시 전체 유향 도로 그래프 + 검증
│  ├─ db/schema.sql, load_graph.py  # ④ MariaDB 적재·버전 전환
│  └─ web/export.py            # ⑤ 활성 버전 → 웹용 정적 JSON (frontend/public/data)
├─ analysis/                   # 탐색·검증용 실험 코드 (core를 가져다 씀, python -m analysis.<모듈>)
│  ├─ build_road_graph.py      # 계열별 방향 그래프 + 본선/가닥 판정
│  ├─ draw_road_graph.py       # matplotlib 지도형·본선축 그림
│  ├─ merge_routes.py          # 크롤링 JSON에서 노선 골라 합치기
│  ├─ build_city_net.py        # 시 전체 무방향 노선망(시각화용, 25m snap)
│  └─ draw_city_net.py         # 노선망 SVG (계열별 색, 공통 구간 다색 병렬)
├─ frontend/                   # Vite + React(JS) SVG 노선망 뷰어 (권역·노선 선택, 브라우저에서 필터)
├─ data/snapshots/<YYYY-MM-DD>/ # raw.json, fixed.json, graph.json, report.txt (git 제외)
├─ .env / .env.example         # DB 접속 (.env는 git 제외)
├─ requirements.txt            # 파이프라인: pymysql, python-dotenv (그 외 표준 라이브러리만)
└─ requirements-analysis.txt   # analysis용: requirements.txt + matplotlib
```

예정: `core/graph/corridor.py`(무방향 층), `core/cli.py`, `backend/`, `deploy/`(systemd timer, nginx), `tests/`.

## 실행

항상 **프로젝트 루트에서 모듈로** 실행한다(`from core.config import ...`가 동작해야 한다). analysis도 `python -m analysis.<모듈>`로 실행한다. 출력 폴더는 자동으로 만들어진다.

```bash
python -m core.crawl.crawl_asan --series all --out data/snapshots/2026-12-01/raw.json
python -m core.graph.fix_shape data/snapshots/2026-12-01/raw.json data/snapshots/2026-12-01/fixed.json
python -m core.graph.build_city_graph data/snapshots/2026-12-01/fixed.json      # → 같은 폴더 graph.json
python -m core.db.load_graph data/snapshots/2026-12-01/graph.json --dry-run   # 변경 여부만
python -m core.db.load_graph data/snapshots/2026-12-01/graph.json             # 적재 + 전환
python -m core.web.export                                                    # ⑤ → frontend/public/data
npm --prefix frontend install && npm --prefix frontend run dev                 # http://localhost:5173
```

- 스키마 초기화: `mariadb -u root -p < core/db/schema.sql` (DB `asan_bis` 생성, 다시 실행해도 안전)
- 의존성: `pip install -r requirements.txt`(파이프라인), `pip install -r requirements-analysis.txt`(analysis)
- 크롤러: `--out`을 생략하면 `data/snapshots/<오늘>/raw.json`(이미 있으면 `<오늘>_2`, `_3` …). 실패한 노선은 순회 뒤 60초 기다려 한 번 더 받고(`--retry-wait`), 그래도 남으면 `meta.errors`에 남긴다. 나중에 `--retry <raw.json>`으로 실패 노선만 다시 받아 그 파일을 갱신한다.
- 수동 갱신(월중 시간표 변경 등)도 전체 수집부터 다시 돌린다. 바뀐 것이 없으면 `data_hash`가 같아 적재가 건너뛰어진다.
- `load_graph` 옵션: `--dry-run`, `--no-activate`(넣기만, 테스트용), `--keep 3`(보관 버전 수, 활성 포함), `--force`. 정리는 `current_version` 기준이라 활성 버전은 지워지지 않는다.
- 크롤러는 시청 서버를 호출한다. 기본 `--delay 1.0`을 줄이지 않는다. 전체 수집은 약 583회 호출, 10분 남짓 걸린다. 테스트는 기존 스냅샷으로 한다.

## 파이프라인 규칙

1. ① 수집 → ② 보정 → ③ 빌드·검증 → ④ 적재·전환 → ⑤ 웹용 데이터 생성. 월 1일 무렵 정기 실행 + 수동 실행.
2. **단계 사이는 파일로 주고받는다.** 각 단계는 앞 단계 파일만 읽고 결과 파일을 쓴다. 어느 단계부터든 다시 돌릴 수 있어야 한다. ②~⑤는 `raw.json`(원본)을 수정하지 않는다(① 수집의 `--retry`만 실패 노선을 채워 넣는다).
3. **변경 감지**: 노선별 `geom_hash`(선형·정류소) / `timetable_hash` / `info_hash`, 전체 `data_hash`. 활성 버전과 같으면 적재하지 않는다.
4. **버전**: 그래프·노선·정류소·시간표 테이블은 모두 `version_id`를 가진다. 새 버전을 넣고 `current_version` 한 행만 바꿔 전환한다. 조회는 활성 버전만 보이는 `v_*` 뷰로 한다.
5. ③의 `meta.issues` 중 `수집 실패`(①의 `meta.errors`), `경로 끊김`, `해시 충돌`은 치명적이다(③ exit 2, ④ 적재 거부). 접두어 목록은 `build_city_graph.FATAL_PREFIXES` 하나로 관리한다. `선형·정류소 없음(제외)`, `정류소가 선형에서 멂`, `선형 누락 의심`, `시간표 원본 ord 중복`은 경고다.

## 데이터·설계 불변식 (어기면 버그)

- **키는 `route_id`**이고 한 방향을 뜻한다. 상행·하행은 다른 `route_id`. `route_name`은 표시용이며 중복된다.
- **천안 노선 제외는 `route_type.strip() == "13"`**으로 한다. 900·910번은 `route_id`가 285로 시작하지만 아산 노선이다. 대상 291건.
- **`route_direction`은 신뢰하지 않는다**(615, 660, 303, 304 오등록). 방향은 도심 허브 기준으로 판정해야 한다(미구현).
- **선형은 반드시 `fix_shape` 보정 후 쓴다.** 원본에 일부 링크 꼭짓점이 역순으로 들어 있어 가짜 루프가 생긴다(900·910·2000번 이순신고 부근 1.0km 등). 크롤러 정렬 `(route_ord, ord)`은 맞고 원본 문제다.
- **같은 도로의 노선은 좌표가 소수점까지 같다.** 도로 그래프는 근접 매칭이 아니라 좌표 일치 + 30m 미만 무정차 구간 합치기로 만든다. 상·하행은 좌표가 겹치지 않으므로 유향 그래프 하나에 다 넣는다.
- **`node_id`·`segment_id`는 좌표 해시(63bit)**라 재빌드해도 안 바뀐 것은 ID가 같다. 순번 ID로 바꾸지 않는다.
- **관리자 입력(`route_group_override` 등)은 버전과 무관하게 `route_id`·`stop_id`에만 연결**한다. `segment_id`에 연결하지 않는다.
- **시간표의 API `ord`는 유일하지 않다**(82-1 중복 행, 88-1 두 묶음 혼재). 빌더가 같은 행을 줄이고 `seq`를 매기며 원본은 `src_ord`.
- `busRouteDetailList1`의 마지막 원소는 정류소가 아니라 요약 레코드다(`stop_ord` 빈 값).
- `stop_id`는 방향별로 다르다. 예외: 단방향 루프(소화마을)는 상·하행이 같은 `stop_id`.
- API 응답은 전부 문자열이고 끝 공백이 있다. trim 한다.
- 실시간 위치 API는 `lng`에 `lat`이 들어오는 결함이 있다. 위치는 GPS가 아니라 최근 `stop_id`로 잡는다.
- 계열 기본값: 100~999 → 백의 자리, 10~99 → 십의 자리, 그 외(한 자리, 1000번대, 순환, 심야) → `X`. **기본값일 뿐**이며 20번대(송악 방면) 같은 예외는 관리자 테이블로 덮어쓴다.
- 공영버스(101)는 시내 구간을 무정차 통과한다. "구간은 지나되 정류소 목록에 없음"으로 표현된다.

## 코딩 규칙

- **Windows와 Ubuntu 양쪽에서 동작**해야 한다. 파일은 항상 `encoding="utf-8"`로 연다(Windows 기본은 cp949). 경로는 `pathlib`/`config.py`의 상수를 쓴다. 소켓 경로 등 OS 전용 값은 `.env`로 뺀다.
- DB 접속은 `pymysql.connect(**core.config.db_params())`만 쓴다. 접속 정보를 코드에 쓰지 않는다. 배포 서버는 `.env` 대신 systemd `EnvironmentFile=/etc/asanbis.env`.
- MariaDB 공간 데이터는 WKT `POINT(lng lat)` 순서(x=경도), `ST_GeomFromText(..., 4326)`. 컬럼에 `REF_SYSTEM_ID`를 쓰지 않는다(10.11에서 문법 오류).
- 파이프라인은 표준 라이브러리 + `pymysql`, `python-dotenv`만 쓴다. 의존성을 늘리기 전에 사용자에게 묻는다.
- 웹 데이터(⑤)의 좌표는 `core.geo.xy` 평면 m 정수이고, 63bit ID는 JS 숫자 범위를 넘으므로 파일 안 참조는 배열 인덱스로 한다. 프론트엔드는 SVG y축을 뒤집어 쓰고, 겹치는 노선은 진행 방향 오른쪽으로 평행선을 쌓는다(상·하행이 갈라져 보임).
- 프론트엔드 의존성은 react, react-dom, vite, @vitejs/plugin-react(버전 고정)만 쓴다. 늘리기 전에 사용자에게 묻는다.
- 그래프 탐색은 DB(재귀 CTE)가 아니라 Python 메모리에서 한다. 데이터가 작다(구간 1,633개, 정류소 2,271개).
- 기하 함수(거리·투영·dedupe·평면 좌표)는 `core.geo`만 쓴다(경도 환산 cos(36.78°) 한 기준). 계열 기본값·색은 `core.groups`만 쓴다(`GROUPS`는 `schema.sql`의 `route_group_def` 초기값과 같게 유지). 새 코드에 다시 정의하지 않는다.
- 결과를 바꾸는 수정 후에는 기존 스냅샷으로 ③을 다시 돌려 요약 수치(구간 1,633 / 노드 1,055 / 정류소 2,271 / 노선-정류소 11,475, 경로 끊김 0)와 비교한다. 바뀌면 이유를 설명한다.

## 현재 상태 (2026-11-18)

- 완료: `--series all` 수집(291건), 선형 보정, 시 전체 유향 그래프, MariaDB 스키마·적재·버전 전환·변경 감지, `.env` 기반 설정, 무방향 노선망 SVG, `core.geo`·`core.groups` 통합, 수집 실패 재시도·적재 차단.
- Windows MariaDB에서 ②~④ 시험 완료(기존 스냅샷 `data/snapshots/20261117/`, 옛 파일명 `asan_series_all.json`·`asan_series_all_fixed.json`·`city_graph.json`).

## 다음 작업 (우선순위 순)

1. `core/cli.py`(`crawl | fix | build | load | export | run-all`).
2. 가닥: 관리자 테이블 `route_strand`(route_id → 가닥 이름)로 수동 지정 → export·뷰어에서 권역 노선 목록을 가닥별로 묶기. 자동 판정은 보류.
3. 웹 뷰어 다듬기: 무방향 도로축을 쓴 개요 보기, 정류소 검색, 노선도(트리) 표기. 배포 시 nginx가 `frontend/dist` 정적 서빙.
4. 무방향 도로축 층(`corridor`, `corridor_segment`)과 겹침 구간 색 순서 계산.
5. `deploy/`: systemd service + timer(월 1회, `Persistent=true`), nginx.
6. FastAPI: 기동 시 활성 그래프 메모리 적재, 검색(정류소 `LIKE`), 실시간 위치(SSE, 캐시 5~10초, 보고 있는 노선만 폴링), 도착정보.
7. 미해결 분석: 방향 판정, 서비스 구간(방면) 단위 정의, 손님 노선, 가닥 세분화, 공영 무정차 표현, 선형 누락 노선(650, 700·702, 41-1·46-1·46-2·47) 처리. 자세한 내용은 `docs/PROJECT_CONTEXT.md` 10장.
