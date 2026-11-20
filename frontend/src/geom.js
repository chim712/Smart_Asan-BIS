// 화면 기하 도우미 (좌표는 SVG 월드 좌표, m 단위)

// 점열을 왼쪽 법선 방향으로 d만큼 평행 이동한다(꼭짓점은 양쪽 법선 평균, 날카로운 모서리는 제한).
export function offsetPolyline(pts, d) {
  if (!d) return pts
  const n = pts.length
  const normals = []
  for (let k = 0; k < n - 1; k++) {
    const dx = pts[k + 1][0] - pts[k][0]
    const dy = pts[k + 1][1] - pts[k][1]
    const L = Math.hypot(dx, dy) || 1
    normals.push([-dy / L, dx / L])
  }
  if (!normals.length) return pts
  const out = []
  for (let k = 0; k < n; k++) {
    const a = normals[Math.max(0, k - 1)]
    const b = normals[Math.min(normals.length - 1, k)]
    let nx = a[0] + b[0]
    let ny = a[1] + b[1]
    const L = Math.hypot(nx, ny)
    if (L < 1e-6) {
      nx = b[0]
      ny = b[1]
    } else {
      nx /= L
      ny /= L
    }
    const cos = nx * b[0] + ny * b[1]
    const scale = d / Math.max(cos, 0.5)
    out.push([pts[k][0] + nx * scale, pts[k][1] + ny * scale])
  }
  return out
}

export function pathD(pts) {
  return 'M' + pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join('L')
}

// 점열 길이 비율 t(0~1) 위치의 점과 진행 방향 단위 벡터
export function pointAt(pts, t) {
  const lens = []
  let total = 0
  for (let k = 0; k < pts.length - 1; k++) {
    const L = Math.hypot(pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1])
    lens.push(L)
    total += L
  }
  let target = total * t
  for (let k = 0; k < lens.length; k++) {
    if (target <= lens[k] || k === lens.length - 1) {
      const r = lens[k] ? Math.min(1, target / lens[k]) : 0
      const [ax, ay] = pts[k]
      const [bx, by] = pts[k + 1]
      const L = lens[k] || 1
      return { x: ax + (bx - ax) * r, y: ay + (by - ay) * r, ux: (bx - ax) / L, uy: (by - ay) / L, total }
    }
    target -= lens[k]
  }
  return { x: pts[0][0], y: pts[0][1], ux: 1, uy: 0, total: 0 }
}

// 겹치는 여러 줄(lane)의 오프셋(px). 유향 그래프라 진행 방향 오른쪽으로 차례로 쌓는다(우측 통행).
// offsetPolyline의 양수 d는 SVG(y 아래) 좌표에서 진행 방향 오른쪽이다. 상·하행 구간이 서로 갈라져 보인다.
export function laneOffsets(widths, start = 1.5, gap = 0.5) {
  let acc = start
  return widths.map((w) => {
    const c = acc + w / 2
    acc += w + gap
    return c
  })
}
