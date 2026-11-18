-- Smart Asan BIS 스키마 (MariaDB 10.11)
-- 그래프·노선·정류소·시간표는 버전(version_id)별로 통째로 쌓고, current_version 한 행만 바꿔 활성 버전을 전환한다.
-- 관리자 입력(계열 덮어쓰기 등)은 버전과 무관하게 route_id 등 시청 데이터 키에만 연결한다.
-- 좌표: lat/lng DOUBLE 컬럼 + 공간 컬럼(POINT/LINESTRING, x=경도 y=위도, 적재 시 SRID 4326).

CREATE DATABASE IF NOT EXISTS asan_bis CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE asan_bis;

-- ---------- 버전 ----------
CREATE TABLE IF NOT EXISTS graph_version (
  version_id        INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  source_crawled_at VARCHAR(32),
  graph_hash        CHAR(40) NOT NULL,          -- 선형·정류소 기준 그래프 해시
  data_hash         CHAR(40) NOT NULL,          -- 그래프 + 시간표 + 노선정보 전체 해시
  status            ENUM('loading','ready','active','retired','failed') NOT NULL DEFAULT 'loading',
  route_count       INT, segment_count INT, stop_count INT,
  meta              LONGTEXT CHECK (JSON_VALID(meta))   -- 선형 보정 내역, 검증 결과
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS current_version (
  id         TINYINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  version_id INT UNSIGNED NOT NULL,
  switched_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  FOREIGN KEY (version_id) REFERENCES graph_version(version_id)
) ENGINE=InnoDB;

-- ---------- 노선 ----------
CREATE TABLE IF NOT EXISTS route (
  version_id      INT UNSIGNED NOT NULL,
  route_id        VARCHAR(12) NOT NULL,          -- 한 방향 = 한 route_id
  route_name      VARCHAR(20) NOT NULL,          -- 표시용, 키 아님
  route_type      VARCHAR(4)  NOT NULL,          -- 101 공영 / 102 시내 / 103 순환
  route_direction VARCHAR(2),                    -- 원본 값(신뢰 불가)
  company_name    VARCHAR(40),
  st_stop_name    VARCHAR(60), ed_stop_name VARCHAR(60),
  trips_weekday   SMALLINT NOT NULL DEFAULT 0,
  trips_total     SMALLINT NOT NULL DEFAULT 0,
  group_default   VARCHAR(4) NOT NULL,           -- 번호 규칙 기본 계열 (1~9, X)
  geom_hash       CHAR(40) NOT NULL,
  timetable_hash  CHAR(40) NOT NULL,
  info_hash       CHAR(40) NOT NULL,
  PRIMARY KEY (version_id, route_id),
  KEY idx_route_name (version_id, route_name),
  KEY idx_route_group (version_id, group_default),
  FOREIGN KEY (version_id) REFERENCES graph_version(version_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS route_timetable (
  version_id INT UNSIGNED NOT NULL,
  route_id   VARCHAR(12) NOT NULL,
  seq        SMALLINT NOT NULL,                  -- 빌더가 매긴 순번 (API ord는 유일하지 않음)
  src_ord    SMALLINT,                           -- API 원본 ord
  departure  CHAR(4) NOT NULL,                   -- HHMM (자정 넘김 주의)
  weekday    BOOLEAN NOT NULL, saturday BOOLEAN NOT NULL,
  holiday    BOOLEAN NOT NULL, vacation BOOLEAN NOT NULL,
  PRIMARY KEY (version_id, route_id, seq),
  FOREIGN KEY (version_id, route_id) REFERENCES route(version_id, route_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ---------- 정류소 ----------
CREATE TABLE IF NOT EXISTS stop (
  version_id INT UNSIGNED NOT NULL,
  stop_id    VARCHAR(12) NOT NULL,               -- 방향별로 다름(맞은편은 다른 ID)
  service_id VARCHAR(10),                        -- 정류소 단축번호
  name       VARCHAR(60) NOT NULL,
  lat DOUBLE NOT NULL, lng DOUBLE NOT NULL,
  location   POINT NOT NULL,
  PRIMARY KEY (version_id, stop_id),
  KEY idx_stop_name (version_id, name),
  SPATIAL INDEX sp_stop (location),
  FOREIGN KEY (version_id) REFERENCES graph_version(version_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ---------- 도로 그래프 (유향) ----------
CREATE TABLE IF NOT EXISTS node (
  version_id INT UNSIGNED NOT NULL,
  node_id    BIGINT UNSIGNED NOT NULL,           -- 좌표 해시: 재빌드해도 같은 점이면 같은 ID
  lat DOUBLE NOT NULL, lng DOUBLE NOT NULL,
  in_deg TINYINT UNSIGNED NOT NULL, out_deg TINYINT UNSIGNED NOT NULL,
  PRIMARY KEY (version_id, node_id),
  FOREIGN KEY (version_id) REFERENCES graph_version(version_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS segment (
  version_id    INT UNSIGNED NOT NULL,
  segment_id    BIGINT UNSIGNED NOT NULL,        -- 점열 해시: 안 바뀐 구간은 같은 ID
  from_node     BIGINT UNSIGNED NOT NULL,
  to_node       BIGINT UNSIGNED NOT NULL,
  length_m      FLOAT NOT NULL,
  trips_weekday SMALLINT UNSIGNED NOT NULL,      -- 지나는 노선의 평일 운행 합
  geom          LINESTRING NOT NULL,
  PRIMARY KEY (version_id, segment_id),
  KEY idx_seg_from (version_id, from_node),
  KEY idx_seg_to (version_id, to_node),
  SPATIAL INDEX sp_segment (geom),
  FOREIGN KEY (version_id, from_node) REFERENCES node(version_id, node_id) ON DELETE CASCADE,
  FOREIGN KEY (version_id, to_node)   REFERENCES node(version_id, node_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 노선이 지나는 구간 순서 (노선·권역 필터의 핵심)
CREATE TABLE IF NOT EXISTS route_segment (
  version_id INT UNSIGNED NOT NULL,
  route_id   VARCHAR(12) NOT NULL,
  seq        SMALLINT UNSIGNED NOT NULL,
  segment_id BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (version_id, route_id, seq),
  KEY idx_rs_segment (version_id, segment_id),
  FOREIGN KEY (version_id, route_id)   REFERENCES route(version_id, route_id) ON DELETE CASCADE,
  FOREIGN KEY (version_id, segment_id) REFERENCES segment(version_id, segment_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 노선의 정차 정류소 순서 + 구간 위 위치
CREATE TABLE IF NOT EXISTS route_stop (
  version_id  INT UNSIGNED NOT NULL,
  route_id    VARCHAR(12) NOT NULL,
  ord         SMALLINT UNSIGNED NOT NULL,
  stop_id     VARCHAR(12) NOT NULL,
  segment_id  BIGINT UNSIGNED NOT NULL,
  offset_m    FLOAT NOT NULL,                    -- 구간 시작점부터 거리
  snap_dist_m FLOAT NOT NULL,                    -- 정류소 ~ 선형 거리 (100m 초과 = 선형 누락 의심)
  PRIMARY KEY (version_id, route_id, ord),
  KEY idx_rstop_stop (version_id, stop_id),
  KEY idx_rstop_segment (version_id, segment_id),
  FOREIGN KEY (version_id, route_id)   REFERENCES route(version_id, route_id) ON DELETE CASCADE,
  FOREIGN KEY (version_id, stop_id)    REFERENCES stop(version_id, stop_id) ON DELETE CASCADE,
  FOREIGN KEY (version_id, segment_id) REFERENCES segment(version_id, segment_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ---------- 관리자 영역 (버전 무관) ----------
CREATE TABLE IF NOT EXISTS route_group_def (
  group_code VARCHAR(4) PRIMARY KEY,             -- 1~9, X, 또는 관리자가 만든 권역 코드
  label      VARCHAR(40) NOT NULL,
  color      CHAR(7) NOT NULL,
  sort_order TINYINT NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS route_group_override (
  route_id   VARCHAR(12) PRIMARY KEY,
  group_code VARCHAR(4) NOT NULL,
  note       VARCHAR(200),
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  FOREIGN KEY (group_code) REFERENCES route_group_def(group_code)
) ENGINE=InnoDB;

INSERT IGNORE INTO route_group_def VALUES
 ('1','100번대 송악','#008300',1), ('2','200번대 성환','#e87ba4',2),
 ('3','300번대 신창·선장','#1baf7a',3), ('4','400번대 신창·도고','#2a78d6',4),
 ('5','500번대 평택','#eda100',5), ('6','600번대 염치·영인·인주','#6250d6',6),
 ('7','700번대 탕정','#eb6834',7), ('8','800번대 배방','#e34948',8),
 ('9','900번대 천안','#8a5a2b',9), ('X','기타','#8c8c8c',10);

-- ---------- 조회용 뷰: 항상 활성 버전만 ----------
CREATE OR REPLACE VIEW v_route AS
SELECT r.*, COALESCE(o.group_code, r.group_default) AS group_code
FROM route r
JOIN current_version c ON c.version_id = r.version_id
LEFT JOIN route_group_override o ON o.route_id = r.route_id;

CREATE OR REPLACE VIEW v_route_segment AS
SELECT rs.route_id, rs.seq, rs.segment_id
FROM route_segment rs JOIN current_version c ON c.version_id = rs.version_id;

CREATE OR REPLACE VIEW v_segment AS
SELECT s.segment_id, s.from_node, s.to_node, s.length_m, s.trips_weekday, s.geom
FROM segment s JOIN current_version c ON c.version_id = s.version_id;

CREATE OR REPLACE VIEW v_stop AS
SELECT st.stop_id, st.service_id, st.name, st.lat, st.lng, st.location
FROM stop st JOIN current_version c ON c.version_id = st.version_id;

CREATE OR REPLACE VIEW v_route_stop AS
SELECT rs.route_id, rs.ord, rs.stop_id, rs.segment_id, rs.offset_m, rs.snap_dist_m
FROM route_stop rs JOIN current_version c ON c.version_id = rs.version_id;

CREATE OR REPLACE VIEW v_node AS
SELECT n.node_id, n.lat, n.lng, n.in_deg, n.out_deg
FROM node n JOIN current_version c ON c.version_id = n.version_id;

CREATE OR REPLACE VIEW v_route_timetable AS
SELECT t.route_id, t.seq, t.src_ord, t.departure, t.weekday, t.saturday, t.holiday, t.vacation
FROM route_timetable t JOIN current_version c ON c.version_id = t.version_id;
