"""Gera Brand/shutlite.ico a partir do símbolo (Brand/shutlite-symbol-dark.svg), sem precisar de cairo.

O desenho é refeito com Pillow nas mesmas medidas do SVG (viewBox 32x32), sobre o fundo escuro da marca,
para o ícone aparecer bem no Explorer claro e no escuro. Rodar só quando o símbolo mudar:

    python -m pip install pillow
    python packaging/make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "Brand" / "shutlite.ico"

BG = (11, 11, 12, 255)        # #0B0B0C, fundo do tema escuro
STROKE = (242, 241, 238, 255)  # #F2F1EE, traço do símbolo para fundo preto
ACCENT = (78, 123, 251, 255)   # #4E7BFB, o quadrado
SIZES = [16, 24, 32, 48, 64, 128, 256]


def render(px: int) -> Image.Image:
    k = 8  # desenha 8x maior e reduz: bordas suaves
    s = px * k / 32  # unidades do viewBox -> pixels
    img = Image.new("RGBA", (px * k, px * k), BG)
    d = ImageDraw.Draw(img)
    w = max(1, round(1.75 * s))

    def box(cx, cy, r):
        return [(cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s]

    # arco: de (27.53, 12.25) a (21.69, 5.30) no sentido horário, arco grande (≈ -18° até 298°)
    d.arc(box(16, 16, 12.12 + 1.75 / 2), start=-18, end=298, fill=STROKE, width=w)
    d.ellipse(box(16, 16, 6.65 + 1.75 / 2), outline=STROKE, width=w)
    d.rectangle([23.85 * s, 6.65 * s, (23.85 + 2.85) * s, (6.65 + 2.85) * s], fill=ACCENT)
    return img.resize((px, px), Image.LANCZOS)


def main() -> None:
    images = [render(px) for px in SIZES]
    images[-1].save(OUT, format="ICO", sizes=[(px, px) for px in SIZES], append_images=images[:-1])
    print(f"gerado: {OUT}")


if __name__ == "__main__":
    main()
