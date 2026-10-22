import math, os
from PIL import Image, ImageDraw, ImageFont
OUT = "/home/claude/sdplugin/com.gravitymedia.datimer.sdPlugin/imgs"
os.makedirs(OUT + "/actions", exist_ok=True); os.makedirs(OUT + "/keys", exist_ok=True)
BG, CYAN, WHITE = (10, 13, 17, 255), (0, 229, 240, 255), (255, 255, 255, 255)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

def glyph(name, d, S, col):
    """Draw a symbol into a box of size S (already supersampled)."""
    w = max(2, int(S * 0.09)); c = S / 2
    if name in ("clock", "category", "plugin"):
        r = S * 0.34; cy = c + S * 0.04
        d.ellipse([c - r, cy - r, c + r, cy + r], outline=col, width=w)
        d.rectangle([c - S * 0.07, cy - r - S * 0.13, c + S * 0.07, cy - r - S * 0.04], fill=col)
        d.line([c, cy, c, cy - r * 0.62], fill=col, width=w)
        d.line([c, cy, c + r * 0.5, cy + r * 0.25], fill=col, width=w)
    elif name == "allstart":
        d.polygon([(S * .28, S * .2), (S * .28, S * .8), (S * .8, S * .5)], fill=col)
    elif name == "allstop":
        d.rounded_rectangle([S * .24, S * .24, S * .76, S * .76], radius=S * .06, fill=col)
    elif name in ("alltoggle", "segtoggle", "progtoggle"):
        d.polygon([(S * .12, S * .22), (S * .12, S * .78), (S * .5, S * .5)], fill=col)
        d.rectangle([S * .58, S * .24, S * .68, S * .76], fill=col)
        d.rectangle([S * .76, S * .24, S * .86, S * .76], fill=col)
        if name != "alltoggle":
            f = ImageFont.truetype(FONT, int(S * .26))
            d.text((S * .5, S * .97), "S" if name == "segtoggle" else "P", font=f, fill=col, anchor="md")
    elif name == "next":
        d.polygon([(S * .18, S * .2), (S * .18, S * .8), (S * .62, S * .5)], fill=col)
        d.rectangle([S * .68, S * .2, S * .8, S * .8], fill=col)
    elif name == "mark":
        d.line([S * .28, S * .15, S * .28, S * .88], fill=col, width=w)
        d.polygon([(S * .3, S * .16), (S * .8, S * .3), (S * .3, S * .5)], fill=col)
    elif name == "reset":
        r = S * .3
        d.arc([c - r, c - r, c + r, c + r], 40, 330, fill=col, width=w)
        a = math.radians(40); x, y = c + r * math.cos(a), c + r * math.sin(a)
        d.polygon([(x - S * .14, y - S * .02), (x + S * .1, y - S * .1), (x + S * .04, y + S * .16)], fill=col)
    elif name == "undo":
        r = S * .3
        d.arc([c - r, c - r, c + r, c + r], 210, 140 + 360, fill=col, width=w)
        a = math.radians(210); x, y = c + r * math.cos(a), c + r * math.sin(a)
        d.polygon([(x + S * .14, y - S * .02), (x - S * .1, y - S * .1), (x - S * .04, y + S * .16)], fill=col)
    elif name == "display":
        d.rounded_rectangle([S * .14, S * .2, S * .86, S * .66], radius=S * .04, outline=col, width=w)
        d.line([c, S * .66, c, S * .8], fill=col, width=w)
        d.line([S * .32, S * .82, S * .68, S * .82], fill=col, width=w)

def icon(name, size, col=WHITE, bg=None, scale=4):
    S = size * scale
    im = Image.new("RGBA", (S, S), bg or (0, 0, 0, 0))
    glyph(name, ImageDraw.Draw(im), S, col)
    return im.resize((size, size), Image.LANCZOS)

def save2(img_fn, path):
    img_fn(1).save(path + ".png"); img_fn(2).save(path + "@2x.png")

names = ["clock", "alltoggle", "allstart", "allstop", "segtoggle", "progtoggle", "next", "mark", "reset", "display", "undo"]
for n in names:
    save2(lambda k, n=n: icon(n, 20 * k), f"{OUT}/actions/{n}")
save2(lambda k: icon("category", 28 * k), f"{OUT}/category")

LABELS = {"clock": "CLOCK", "alltoggle": "ALL", "allstart": "START", "allstop": "STOP",
          "segtoggle": "SEGMENT", "progtoggle": "PROGRAM", "next": "NEXT", "mark": "MARK",
          "reset": "RESET", "display": "DISPLAY", "undo": "UNDO"}
def keyimg(n, size):
    S = size * 4
    im = Image.new("RGBA", (S, S), BG); d = ImageDraw.Draw(im)
    g = icon(n, int(S * .5), CYAN, scale=1)
    im.alpha_composite(g, (int(S * .25), int(S * .12)))
    f = ImageFont.truetype(FONT, int(S * .15))
    d.text((S / 2, S * .86), LABELS[n], font=f, fill=WHITE, anchor="mm")
    return im.resize((size, size), Image.LANCZOS)
for n in names:
    save2(lambda k, n=n: keyimg(n, 72 * k), f"{OUT}/keys/{n}")

def plugin_icon(size):
    S = size * 4
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=S * .2, fill=BG)
    g = icon("plugin", int(S * .74), CYAN, scale=1)
    im.alpha_composite(g, (int(S * .13), int(S * .1)))
    return im.resize((size, size), Image.LANCZOS)
plugin_icon(256).save(f"{OUT}/plugin.png"); plugin_icon(512).save(f"{OUT}/plugin@2x.png")
print("icons done")
