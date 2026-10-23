def srgb(c):
    c = c / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

def lum(h):
    h = h.lstrip('#')
    r, g, b = (int(h[i:i+2], 16) for i in (0, 2, 4))
    return 0.2126*srgb(r) + 0.7152*srgb(g) + 0.0722*srgb(b)

def ratio(fg, bg):
    l1, l2 = lum(fg), lum(bg)
    if l1 < l2: l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)

def blend(fg, alpha, bg):
    h1, h2 = fg.lstrip('#'), bg.lstrip('#')
    o = []
    for i in (0, 2, 4):
        a, b = int(h1[i:i+2], 16), int(h2[i:i+2], 16)
        o.append(round(a * alpha + b * (1 - alpha)))
    return '#%02X%02X%02X' % tuple(o)

CARD = '#1C1C1E'

print("=== GLOBAL .tag as-is over new card #1C1C1E ===")
# .tag.green: bg rgba(16,185,129,.12) color #34d399
g_bg = blend('#10B981', 0.12, CARD)
print(f"  .tag.green bg {g_bg}  label #34d399 -> {ratio('#34d399', g_bg):.2f}:1  {'PASS' if ratio('#34d399',g_bg)>=4.5 else 'FAIL'}")
# .tag.amber: bg rgba(245,158,11,.12) color #fbbf24
a_bg = blend('#F59E0B', 0.12, CARD)
print(f"  .tag.amber bg {a_bg}  label #fbbf24 -> {ratio('#fbbf24', a_bg):.2f}:1  {'PASS' if ratio('#fbbf24',a_bg)>=4.5 else 'FAIL'}")
# .tag (neutral): bg rgba(255,255,255,.04) color var(--muted) #7b8ba8
n_bg = blend('#FFFFFF', 0.04, CARD)
print(f"  .tag       bg {n_bg}  label #7b8ba8 -> {ratio('#7b8ba8', n_bg):.2f}:1  {'PASS' if ratio('#7b8ba8',n_bg)>=4.5 else 'FAIL'}")

print()
print("=== APPLE-style tag overrides over card ===")
for name, tint, txt in [
    ('green',  '#30D158', '#30D158'),
    ('amber',  '#FF9F0A', '#FF9F0A'),
    ('red',    '#FF453A', '#FF6961'),
    ('blue',   '#0A84FF', '#5AA9FF'),
    ('neutral','#FFFFFF', '#AEAEB2'),
]:
    bg = blend(tint, 0.15, CARD)
    r = ratio(txt, bg)
    print(f"  .tag.{name:8s} bg {bg}  label {txt} -> {r:5.2f}:1  {'PASS' if r>=4.5 else 'FAIL'}")

print()
print("=== RobotIcon 'boxed' variant bg — check it reads on new card ===")
print("  (icon color inherits; boxed variant uses its own bg — leaving untouched)")
