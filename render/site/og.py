# og.py · composite the oracle pair over the site field (#060606) at 1200x630
# for the og:image. Part of `make render-oracles`; never hand-edited output.
from PIL import Image

FIELD = (6, 6, 6, 255)
W, H = 1200, 630

hero = Image.open("web/assets/site/oracles-pair@3x.png").convert("RGBA")
canvas = Image.new("RGBA", (W, H), FIELD)
scale = min((W * 0.86) / hero.width, (H * 0.80) / hero.height)
hero = hero.resize((int(hero.width * scale), int(hero.height * scale)), Image.LANCZOS)
canvas.alpha_composite(hero, ((W - hero.width) // 2, (H - hero.height) // 2 - 10))
canvas.convert("RGB").save("web/assets/site/og-image.png", optimize=True)
print("og-image.png 1200x630 composited")
