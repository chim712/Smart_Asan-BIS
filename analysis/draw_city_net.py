"""시 전체 노선망 SVG. 사용: python -m analysis.draw_city_net <city_net.json> <out.svg>"""
import json,math,sys
from core.groups import GROUPS
with open(sys.argv[1], encoding='utf-8') as fp:
    d=json.load(fp)
V=d['V']; segs=d['segments']; out=sys.argv[2]
COL={g:(color,label) for g,(label,color) in GROUPS.items()}
COL['X']=(COL['X'][0],COL['X'][1]+' (1000번대·한 자리·순환·심야)')
ORDER=['1','2','3','4','5','6','7','8','9','X']
W=2.0   # 계열 1개 선 굵기(px)
MAXK=9  # 이 수를 넘으면 검정 단일선
def rdp(P,eps):
    if len(P)<3: return P
    a,b=P[0],P[-1];dx,dy=b[0]-a[0],b[1]-a[1];L=math.hypot(dx,dy) or 1
    i,dm=0,0
    for k in range(1,len(P)-1):
        dd=abs((P[k][0]-a[0])*dy-(P[k][1]-a[1])*dx)/L
        if dd>dm:i,dm=k,dd
    if dm>eps: return rdp(P[:i+1],eps)[:-1]+rdp(P[i:],eps)
    return [a,b]
xs=[p[0] for p in V];ys=[p[1] for p in V]
x0,x1,y0,y1=min(xs),max(xs),min(ys),max(ys)
PAD=40; S=2200/(x1-x0)   # px per m
Wd=2200+2*PAD+460; Ht=(y1-y0)*S+2*PAD
tx=lambda p:((p[0]-x0)*S+PAD,(y1-p[1])*S+PAD)
def offset(P,o):
    if o==0: return P
    n=len(P);R=[]
    for i in range(n):
        a=P[max(0,i-1)];b=P[min(n-1,i+1)]
        dx,dy=b[0]-a[0],b[1]-a[1];L=math.hypot(dx,dy) or 1
        R.append((P[i][0]-dy/L*o,P[i][1]+dx/L*o))
    return R
def pstr(P): return 'M'+' L'.join(f'{x:.1f},{y:.1f}' for x,y in P)
layers={'base':[],'multi':[],'black':[]}
nblack=0
for sg in segs:
    P=[tx(V[i]) for i in sg['nodes']]
    P=rdp(P,0.6)
    # 오프셋 방향을 세그먼트 방향과 무관하게 고정(서→동 / 남→북 기준)
    if (P[-1][0],-P[-1][1])<(P[0][0],-P[0][1]): P=P[::-1]
    g=[x for x in ORDER if x in sg['groups']];k=len(g)
    if k>MAXK:
        nblack+=1
        layers['black'].append(f'<path d="{pstr(P)}" stroke="#1a1a1a" stroke-width="{W*MAXK:.1f}"/>');continue
    for j,gg in enumerate(g):
        o=(j-(k-1)/2)*W
        layers['multi' if k>1 else 'base'].append(f'<path d="{pstr(offset(P,o))}" stroke="{COL[gg][0]}" stroke-width="{W+0.25:.2f}"/>')
leg=[]
lx=2200+2*PAD+10; ly=PAD+30
leg.append(f'<text x="{lx}" y="{ly}" font-size="22" font-weight="700" fill="#0b0b0b">아산시 버스 노선망</text>')
leg.append(f'<text x="{lx}" y="{ly+24}" font-size="13" fill="#52514e">계열(백 단위)별 색상 · 공영 두 자리 수는 같은 방면 계열에 포함</text>')
for i,gg in enumerate(ORDER):
    y=ly+60+i*28
    leg.append(f'<line x1="{lx}" y1="{y}" x2="{lx+34}" y2="{y}" stroke="{COL[gg][0]}" stroke-width="5" stroke-linecap="round"/><text x="{lx+44}" y="{y+5}" font-size="15" fill="#0b0b0b">{COL[gg][1]}</text>')
y=ly+60+len(ORDER)*28
leg.append(f'<line x1="{lx}" y1="{y}" x2="{lx+34}" y2="{y}" stroke="#1a1a1a" stroke-width="5" stroke-linecap="round"/><text x="{lx+44}" y="{y+5}" font-size="15" fill="#0b0b0b">{MAXK+1}개 계열 이상 공통 구간</text>')
leg.append(f'<text x="{lx}" y="{y+40}" font-size="12" fill="#52514e">겹치는 구간은 계열 수만큼 나란히 그림</text>')
# 축척
km=5000*S; leg.append(f'<line x1="{lx}" y1="{y+80}" x2="{lx+km:.1f}" y2="{y+80}" stroke="#52514e" stroke-width="2"/><text x="{lx}" y="{y+98}" font-size="12" fill="#52514e">5 km</text>')
svg=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {Wd:.0f} {Ht:.0f}" width="{Wd:.0f}" height="{Ht:.0f}" font-family="Noto Sans KR, Apple SD Gothic Neo, sans-serif">',
     f'<rect width="100%" height="100%" fill="#fcfcfb"/>',
     '<g fill="none" stroke-linecap="round" stroke-linejoin="round">',*layers['base'],*layers['multi'],*layers['black'],'</g>',*leg,'</svg>']
with open(out,'w',encoding='utf-8') as fp:
    fp.write('\n'.join(svg))
print('segments',len(segs),'black',nblack,'size',round(Wd),round(Ht))
