"""Chart generation for bondify bot."""
import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

DARK_BG    = "#0f1117"
DARK_PANEL = "#1a1d27"
ACCENT     = "#4ade80"
ACCENT2    = "#60a5fa"
ACCENT3    = "#f87171"
TEXT       = "#e2e8f0"
GRID       = "#2d3148"


def _dark_fig(w=12, h=7):
    fig, ax = plt.subplots(figsize=(w, h))
    fig.patch.set_facecolor(DARK_BG)
    ax.set_facecolor(DARK_PANEL)
    for spine in ax.spines.values():
        spine.set_edgecolor(GRID)
    ax.tick_params(colors=TEXT, labelsize=9)
    ax.xaxis.label.set_color(TEXT)
    ax.yaxis.label.set_color(TEXT)
    ax.grid(True, color=GRID, linewidth=0.6, alpha=0.7)
    return fig, ax


def _to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def generate_ofz_curve(
    bonds: list[dict],
    zcyc_points: list,
) -> bytes:
    """OFZ fixed-coupon yield curve with КБД overlay."""
    from zcyc import zcyc_yield

    pts = [(b["duration_years"], float(b["yield"]), b["name"])
           for b in bonds if b.get("duration_years") and b.get("yield")]
    if not pts:
        raise ValueError("Нет данных ОФЗ с вычисленной дюрацией")

    pts.sort(key=lambda p: p[0])
    x     = [p[0] for p in pts]
    y     = [p[1] for p in pts]
    names = [p[2] for p in pts]

    fig, ax = _dark_fig()

    ax.scatter(x, y, color=ACCENT, s=80, zorder=5,
               edgecolors=DARK_BG, linewidths=0.7,
               label=f"ОФЗ фикс. купон ({len(pts)})")

    if zcyc_points:
        x_lo = min(zcyc_points[0][0], min(x))
        x_hi = max(zcyc_points[-1][0], max(x))
        xk = np.linspace(x_lo, x_hi, 400)
        pairs = [(t, zcyc_yield(t, zcyc_points)) for t in xk]
        pairs = [(t, v) for t, v in pairs if v is not None]
        if pairs:
            xk2, yk2 = zip(*pairs)
            ax.plot(xk2, yk2, color=ACCENT3, linewidth=2.2, zorder=6, label="КБД MOEX")
        kx = [p[0] for p in zcyc_points]
        ky = [p[1] for p in zcyc_points]
        ax.scatter(kx, ky, color=ACCENT3, s=35, zorder=7,
                   edgecolors=DARK_BG, linewidths=0.5)

    for xi, yi, name in zip(x, y, names):
        short = name.replace("ОФЗ-ПД ", "").replace("ОФЗ ", "").strip()
        ax.annotate(short, (xi, yi), color=TEXT, fontsize=6.5,
                    xytext=(4, 3), textcoords="offset points")

    ax.set_xlabel("Дюрация Маколея, лет", fontsize=11)
    ax.set_ylabel("Доходность к погашению, %", fontsize=11)
    ax.set_title("ОФЗ фиксированный купон — кривая доходности и КБД",
                 color=TEXT, fontsize=13, fontweight="bold", pad=14)
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f%%"))
    ax.legend(facecolor=DARK_PANEL, edgecolor=GRID, labelcolor=TEXT, fontsize=9)

    return _to_png(fig)
