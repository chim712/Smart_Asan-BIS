// ⑤ export 결과(public/data)를 받아 화면용 색인을 만든다.
// 좌표: export는 평면 m(x=동, y=북)이다. SVG는 y가 아래로 커지므로 여기서 y를 한 번 뒤집는다.

const BASE = `${import.meta.env.BASE_URL}data/`

async function getJson(name) {
  const res = await fetch(BASE + name)
  if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`)
  return res.json()
}

// '45-1' < '450', '순환5' 같은 비숫자는 뒤로
export function compareRouteName(a, b) {
  const pa = /^(\d+)(?:-(\d+))?/.exec(a)
  const pb = /^(\d+)(?:-(\d+))?/.exec(b)
  if (pa && pb) {
    return (+pa[1] - +pb[1]) || ((+pa[2] || 0) - (+pb[2] || 0)) || a.localeCompare(b, 'ko')
  }
  if (pa) return -1
  if (pb) return 1
  return a.localeCompare(b, 'ko')
}

function pathD(pts) {
  return 'M' + pts.map(([x, y]) => `${x},${y}`).join('L')
}

export async function loadData() {
  const manifest = await getJson(`manifest.json?t=${Date.now()}`)
  const [network, routes, stops] = await Promise.all([
    getJson(manifest.files.network),
    getJson(manifest.files.routes),
    getJson(manifest.files.stops),
  ])

  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  const segments = network.segments.map((s, i) => {
    const pts = s.pts.map(([x, y]) => [x, -y])
    for (const [x, y] of pts) {
      if (x < minX) minX = x
      if (x > maxX) maxX = x
      if (y < minY) minY = y
      if (y > maxY) maxY = y
    }
    return { ...s, i, pts, d: pathD(pts) }
  })
  const nodes = network.nodes.map((n) => ({ ...n, y: -n.y }))
  const stopList = stops.map((s) => ({ ...s, y: -s.y }))

  const groups = manifest.groups
  const groupByCode = Object.fromEntries(groups.map((g, k) => [g.code, { ...g, order: k }]))

  const routeList = routes.map((r, i) => ({
    ...r,
    i,
    length: r.segs.reduce((acc, si) => acc + segments[si].len, 0),
  }))
  const byName = new Map()
  for (const r of routeList) {
    if (!byName.has(r.name)) byName.set(r.name, [])
    byName.get(r.name).push(r.i)
  }
  const names = [...byName.keys()].sort(compareRouteName)

  return {
    manifest,
    segments,
    nodes,
    stops: stopList,
    routes: routeList,
    groups,
    groupByCode,
    byName,
    names,
    bbox: { minX, minY, maxX, maxY },
    maxTrips: Math.max(...segments.map((s) => s.trips)),
  }
}
