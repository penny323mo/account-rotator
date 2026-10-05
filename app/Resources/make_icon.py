"""Generate the Account Rotator mark (Antigravity arch behind, Codex cloud in front, Claude spark on top) for web + app."""
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CX, CY, SCALE = 38.6, 40.0, 1.45         # Codex cloud centre and size (1.0 = original small cloud)
CORE, R, r = 11.2 * SCALE, 9 * SCALE, 7 * SCALE


def cloud(extra):
    return f'<circle cx="{CX}" cy="{CY}" r="{CORE + extra:.2f}"/>' + ''.join(
        f'<circle cx="{CX + R * math.cos(math.radians(a)):.2f}" cy="{CY + R * math.sin(math.radians(a)):.2f}" r="{r + extra:.2f}"/>'
        for a in range(0, 360, 45))


def pt(x, y):  # ">_" glyph designed around (44.5, 43.5) in the original cloud
    return f'{CX + (x - 44.5) * SCALE * 1.15:.2f} {CY + (y - 43.5) * SCALE * 1.15:.2f}'


# Claude: the cream starburst of the Claude app icon - slim petal rays of uneven length with clear gaps (narrow at
# the centre, a little wider and round tipped outward) around a small solid core. In front, reaching over the top of
# the cloud and the arch: arch <- cloud <- star, all three overlapping.
SX, SY, SR = 46.0, 15.4, 19.0
PETALS = [(-6, 1.0), (24, .74), (52, .9), (80, .66), (106, 1.0), (134, .7), (160, .94),
          (190, .62), (214, .98), (242, .7), (268, .9), (298, .66), (330, .96)]


def _petal(angle, k):
    length, b, t = SR * k, 0.95, 1.75  # base / tip half width
    far = length - t
    return (f'<path transform="translate({SX} {SY}) rotate({angle})" '
            f'd="M0 {-b}L{far:.2f} {-t}A{t} {t} 0 0 1 {far:.2f} {t}L0 {b}Z"/>')


SPARK = ('<g fill="#b5502f" stroke="#b5502f" stroke-width="1.5" stroke-linejoin="round" opacity=".6">' + ''.join(_petal(a, k) for a, k in PETALS) + f'<circle cx="{SX}" cy="{SY}" r="2.9"/></g>'
         '<g fill="#fbfaf7">' + ''.join(_petal(a, k) for a, k in PETALS) + f'<circle cx="{SX}" cy="{SY}" r="2.9"/></g>')

ARCH = "M3 53.5C10 49 13 7 24.5 6.5 36 7 39 49 46 53.5 48 55 47 58.5 44 58 35.5 56.5 31.5 28 24.5 28 17.5 28 13.5 56.5 5 58 2 58.5 1 55 3 53.5Z"

# Solid mark: Antigravity arch behind, Codex cloud in front with a white keyline, Claude spark on top.
MARK_DEFS = f'''<defs>
<linearGradient id="ag" gradientUnits="userSpaceOnUse" x1="30" y1="56" x2="20" y2="6"><stop stop-color="#2f6ff0"/><stop offset=".5" stop-color="#3d8bf5"/><stop offset=".68" stop-color="#3fae5c"/><stop offset=".8" stop-color="#f6c02c"/><stop offset=".9" stop-color="#f4822a"/><stop offset="1" stop-color="#e8453c"/></linearGradient>
<radialGradient id="ah" gradientUnits="userSpaceOnUse" cx="36" cy="14" r="16"><stop stop-color="#b457e8" stop-opacity=".55"/><stop offset="1" stop-color="#b457e8" stop-opacity="0"/></radialGradient>
<linearGradient id="cx" gradientUnits="userSpaceOnUse" x1="{CX - 14}" y1="{CY - 16}" x2="{CX + 12}" y2="{CY + 18}"><stop stop-color="#a99bf5"/><stop offset=".45" stop-color="#6a73ff"/><stop offset="1" stop-color="#3f3df0"/></linearGradient>
<radialGradient id="hl" gradientUnits="userSpaceOnUse" cx="{CX - 7}" cy="{CY - 10}" r="{17 * SCALE:.1f}"><stop stop-color="#fff" stop-opacity=".42"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>
<clipPath id="cl">{cloud(0)}</clipPath>
<clipPath id="al"><path d="{ARCH}"/></clipPath>
<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#d68f72"/><stop offset="1" stop-color="#d7835e"/></linearGradient>
<linearGradient id="bgs" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#fff" stop-opacity=".1"/><stop offset=".4" stop-color="#fff" stop-opacity="0"/></linearGradient>
</defs>'''
FIT = 0.86  # the three marks sit a little smaller inside the orange tile
MARK_BODY = f'''<g transform="translate(32 32) scale({FIT}) translate(-32 -32)">
<path d="{ARCH}" fill="#fff" stroke="#fff" stroke-width="3.4" stroke-linejoin="round"/>
<g clip-path="url(#al)"><rect width="64" height="64" fill="url(#ag)"/><rect width="64" height="64" fill="url(#ah)"/></g>
<g fill="#fff">{cloud(2.2)}</g>
<g clip-path="url(#cl)"><rect width="64" height="64" fill="url(#cx)"/><rect width="64" height="64" fill="url(#hl)"/></g>
<path d="M{pt(37.5, 38.5)}L{pt(42.1, 43.1)}L{pt(37.5, 47.7)}" fill="none" stroke="#fff" stroke-width="{2.8 * SCALE:.2f}" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M{pt(45.5, 48.3)}L{pt(51.2, 48.3)}" stroke="#fff" stroke-width="{2.8 * SCALE:.2f}" stroke-linecap="round"/>
{SPARK}
</g>'''

# Liquid-glass finish over the whole tile (no filters: renderers disagree on them): a curved gloss across the top, a
# bright edge top left and bottom right, and a little refracted shade at the bottom.
GLASS64 = '''<defs>
<clipPath id="tc"><rect width="64" height="64" rx="14.5"/></clipPath>
<linearGradient id="gl" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#fff" stop-opacity=".4"/><stop offset=".6" stop-color="#fff" stop-opacity=".07"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
<linearGradient id="rm" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#fff" stop-opacity=".95"/><stop offset=".3" stop-color="#fff" stop-opacity=".18"/><stop offset=".7" stop-color="#fff" stop-opacity=".08"/><stop offset="1" stop-color="#fff" stop-opacity=".7"/></linearGradient>
<linearGradient id="bs" x1="0" y1="0" x2="0" y2="1"><stop offset=".7" stop-color="#7a2c12" stop-opacity="0"/><stop offset="1" stop-color="#7a2c12" stop-opacity=".13"/></linearGradient>
</defs>
<g clip-path="url(#tc)">
<path d="M0 0H64V27C50 21.5 17 21.5 0 29Z" fill="url(#gl)"/>
<rect width="64" height="64" fill="url(#bs)"/>
</g>
<rect x=".6" y=".6" width="62.8" height="62.8" rx="13.9" fill="none" stroke="url(#rm)" stroke-width="1.2"/>'''

BG = '<rect width="64" height="64" rx="14.5" fill="url(#bg)"/><rect width="64" height="32" rx="14.5" fill="url(#bgs)"/>'
MARK = MARK_DEFS + MARK_BODY
(ROOT / 'web/icon.svg').write_text(
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">\n<title>Account Rotator</title>\n{MARK_DEFS}\n{BG}\n{MARK_BODY}\n{GLASS64}\n</svg>\n')
# soft drop shadow from stacked translucent rects (renderers differ on feGaussianBlur: Quick Look draws a hard edge)
SHADOW = ''.join(f'<rect x="{100 - k * 3}" y="{112 - k * 3}" width="{824 + k * 6}" height="{824 + k * 6}" rx="{185 + k * 3}" fill="#5a2312" opacity=".02"/>'
                 for k in range(1, 9))
(ROOT / 'app/Resources/AppIcon.svg').write_text(f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
<defs>
<linearGradient id="tile" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#d68f72"/><stop offset="1" stop-color="#d7835e"/></linearGradient>
<linearGradient id="sheen" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#ffffff" stop-opacity=".14"/><stop offset=".35" stop-color="#ffffff" stop-opacity="0"/></linearGradient>
<linearGradient id="tileRim" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#ffffff"/><stop offset=".45" stop-color="#ffffff" stop-opacity=".25"/><stop offset=".8" stop-color="#8ea3c8" stop-opacity=".25"/><stop offset="1" stop-color="#7d93bd" stop-opacity=".7"/></linearGradient>
<clipPath id="tileClip"><rect x="100" y="100" width="824" height="824" rx="185"/></clipPath>
</defs>
{SHADOW}
<rect x="100" y="100" width="824" height="824" rx="185" fill="url(#tile)"/>
<g clip-path="url(#tileClip)"><ellipse cx="400" cy="120" rx="520" ry="230" fill="url(#sheen)"/></g>
<rect x="104" y="104" width="816" height="816" rx="181" fill="none" stroke="url(#tileRim)" stroke-width="8"/>
<g transform="translate(173 173) scale(10.6)">{MARK}</g>
<g transform="translate(100 100) scale(12.875)">{GLASS64}</g>
</svg>
''')
