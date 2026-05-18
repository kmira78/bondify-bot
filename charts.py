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
ACCENT4    = "#facc15"   # previous-session КБД (yellow-amber)
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
    bonds: list[dict],          # each bond must have "duration_years", "yield", "name"
    zcyc_points: list,          # list of (period_years, yield_pct) from zcyc.fetch_zcyc()
    zcyc_prev_points: list | None = None,  # previous session КБД (optional)
    zcyc_prev_date: str = "",   # YYYY-MM-DD label for previous session
) -> bytes:
    """
    OFZ fixed-coupon yield curve.
    X-axis = Macaulay duration (years), Y-axis = YTM (%).
    Overlays the KBD (КБД) zero-coupon curve from MOEX.
    """
    from zcyc import zcyc_yield

    pts = [(b["duration_years"], float(b["yield"]), b["name"])
           for b in bonds if b.get("duration_years") and b.get("yield")]
    if not pts:
        raise ValueError("Нет данных ОФЗ с вычисленной дюрацией")

    pts.sort(key=lambda p: p[0])
    x  = [p[0] for p in pts]
    y  = [p[1] for p in pts]
    names = [p[2] for p in pts]

    fig, ax = _dark_fig()

    # OFZ scatter
    ax.scatter(x, y, color=ACCENT, s=80, zorder=5,
               edgecolors=DARK_BG, linewidths=0.7,
               label=f"ОФЗ фикс. купон ({len(pts)})")

    # Previous-session КБД overlay (dashed, behind current)
    if zcyc_prev_points:
        x_lo = min(zcyc_prev_points[0][0], min(x))
        x_hi = max(zcyc_prev_points[-1][0], max(x))
        xp = np.linspace(x_lo, x_hi, 400)
        pairs_p = [(t, zcyc_yield(t, zcyc_prev_points)) for t in xp]
        pairs_p = [(t, v) for t, v in pairs_p if v is not None]
        if pairs_p:
            xp2, yp2 = zip(*pairs_p)
            prev_label = f"КБД пред. сессия ({zcyc_prev_date})" if zcyc_prev_date else "КБД пред. сессия"
            ax.plot(xp2, yp2, color=ACCENT4, linewidth=1.6, linestyle="--",
                    zorder=5, alpha=0.75, label=prev_label)

    # KBD overlay (current session)
    if zcyc_points:
        x_lo = min(zcyc_points[0][0], min(x))
        x_hi = max(zcyc_points[-1][0], max(x))
        xk = np.linspace(x_lo, x_hi, 400)
        pairs = [(t, zcyc_yield(t, zcyc_points)) for t in xk]
        pairs = [(t, v) for t, v in pairs if v is not None]
        if pairs:
            xk2, yk2 = zip(*pairs)
            ax.plot(xk2, yk2, color=ACCENT3, linewidth=2.2, zorder=6, label="КБД MOEX (тек.)")
        # actual KBД dots at standard terms
        kx = [p[0] for p in zcyc_points]
        ky = [p[1] for p in zcyc_points]
        ax.scatter(kx, ky, color=ACCENT3, s=35, zorder=7,
                   edgecolors=DARK_BG, linewidths=0.5)

    # Bond name annotations
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
