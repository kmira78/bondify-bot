"""
Bond calculator: YTM, Macaulay/Modified duration, PVBP, Convexity.
Methodology: Cbonds Bond Calculator (HelpCalculatorRus.pdf).
Day count: Actual/365F (Russian standard).

Conventions:
- Nominal N0 = 100 (percent).
- Price P is "clean price" in % of nominal (e.g. 98.5).
- Coupon rate C% is annual %, paid C% * N_outstanding * Tc/365 per coupon.
- Amortization schedule: dict {coupon_index_1based: percent_of_initial_nominal_repaid}.
  Sum of percents must be 100. Last repayment is at maturity (final coupon).
  If empty -> bullet bond, full nominal repaid with last coupon.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

B = 365  # Actual/365F


@dataclass
class CashFlow:
    """One scheduled cash flow."""
    idx: int                 # coupon number, 1-based
    pay_date: date           # payment date
    days_from_settle: int    # ti - t0 in days
    coupon: float            # coupon amount (% of initial nominal)
    amort: float             # principal repaid (% of initial nominal)
    n_outstanding_before: float  # outstanding nominal at start of this period (% of initial)


@dataclass
class BondSpec:
    coupon_rate: float                      # annual %, e.g. 12.5
    coupon_period_days: int                 # Tc, e.g. 182
    n_coupons: int                          # total number of coupons
    settle_date: date                       # t0 (today by default)
    first_coupon_date: date                 # date of coupon #1 (>= settle_date)
    amort_schedule: dict[int, float] = field(default_factory=dict)
    nominal: float = 100.0                  # initial nominal, %

    def build_cashflows(self) -> list[CashFlow]:
        # Validate amortization
        if self.amort_schedule:
            total = sum(self.amort_schedule.values())
            if abs(total - 100.0) > 1e-6:
                raise ValueError(f"График амортизации должен суммироваться в 100%, получено {total}")
            for k in self.amort_schedule:
                if not (1 <= k <= self.n_coupons):
                    raise ValueError(f"Купон #{k} вне диапазона 1..{self.n_coupons}")
        # If no schedule -> all 100% at last coupon
        sched = dict(self.amort_schedule) if self.amort_schedule else {self.n_coupons: 100.0}

        flows: list[CashFlow] = []
        outstanding = self.nominal
        for i in range(1, self.n_coupons + 1):
            pay_date = self.first_coupon_date + timedelta(days=self.coupon_period_days * (i - 1))
            days = (pay_date - self.settle_date).days
            coupon = outstanding * self.coupon_rate / 100.0 * self.coupon_period_days / B
            amort = sched.get(i, 0.0)
            flows.append(CashFlow(
                idx=i, pay_date=pay_date, days_from_settle=days,
                coupon=coupon, amort=amort,
                n_outstanding_before=outstanding,
            ))
            outstanding -= amort
        return flows


def accrued_interest(spec: BondSpec, flows: list[CashFlow]) -> float:
    """НКД on settle date: A = C_next * (t0 - t_prev) / Tc."""
    next_flow = flows[0]
    prev_date = next_flow.pay_date - timedelta(days=spec.coupon_period_days)
    days_in_period = (next_flow.pay_date - prev_date).days
    days_accrued = (spec.settle_date - prev_date).days
    if days_in_period <= 0:
        return 0.0
    return next_flow.coupon * days_accrued / days_in_period


def price_from_ytm(spec: BondSpec, flows: list[CashFlow], ytm: float) -> float:
    dirty = 0.0
    for f in flows:
        cf = f.coupon + f.amort
        if cf == 0:
            continue
        t = f.days_from_settle / B
        dirty += cf / (1.0 + ytm) ** t
    return dirty


def ytm_from_price(spec: BondSpec, flows: list[CashFlow], clean_price: float,
                   tol: float = 1e-10, max_iter: int = 200) -> float:
    accrued = accrued_interest(spec, flows)
    target_dirty = clean_price + accrued
    lo, hi = -0.9999, 10.0
    f_lo = price_from_ytm(spec, flows, lo) - target_dirty
    f_hi = price_from_ytm(spec, flows, hi) - target_dirty
    if f_lo * f_hi > 0:
        raise ValueError("Не удалось найти YTM в диапазоне -99.99% .. 1000%")
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        f_mid = price_from_ytm(spec, flows, mid) - target_dirty
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
            f_hi = f_mid
        else:
            lo = mid
            f_lo = f_mid
    return (lo + hi) / 2.0


def macaulay_duration(spec: BondSpec, flows: list[CashFlow], ytm: float) -> tuple[float, float]:
    dirty = price_from_ytm(spec, flows, ytm)
    if dirty <= 0:
        return 0.0, 0.0
    weighted_days = 0.0
    for f in flows:
        cf = f.coupon + f.amort
        if cf == 0:
            continue
        t = f.days_from_settle / B
        pv = cf / (1.0 + ytm) ** t
        weighted_days += f.days_from_settle * pv
    d_days = weighted_days / dirty
    return d_days, d_days / B


def modified_duration(d_years: float, ytm: float) -> float:
    return d_years / (1.0 + ytm)


def pvbp(dirty_price: float, mod_dur: float) -> float:
    return dirty_price * mod_dur * 0.0001


def convexity(spec: BondSpec, flows: list[CashFlow], ytm: float) -> float:
    dirty = price_from_ytm(spec, flows, ytm)
    if dirty <= 0:
        return 0.0
    s = 0.0
    for f in flows:
        cf = f.coupon + f.amort
        if cf == 0:
            continue
        t_days = f.days_from_settle
        t = t_days / B
        s += (t_days * (t_days + B)) / (B * B) * cf / (1.0 + ytm) ** (t + 2)
    return s / dirty


@dataclass
class CalcResult:
    clean_price: float
    accrued: float
    dirty_price: float
    ytm: float
    duration_days: float
    duration_years: float
    mod_duration: float
    pvbp: float
    convexity: float
    flows: list[CashFlow]


def calc_from_price(spec: BondSpec, clean_price: float) -> CalcResult:
    flows = spec.build_cashflows()
    accrued = accrued_interest(spec, flows)
    ytm = ytm_from_price(spec, flows, clean_price)
    return _build_result(spec, flows, clean_price, accrued, ytm)


def calc_from_ytm(spec: BondSpec, ytm: float) -> CalcResult:
    flows = spec.build_cashflows()
    accrued = accrued_interest(spec, flows)
    dirty = price_from_ytm(spec, flows, ytm)
    clean = dirty - accrued
    return _build_result(spec, flows, clean, accrued, ytm)


def _build_result(spec: BondSpec, flows: list[CashFlow],
                  clean: float, accrued: float, ytm: float) -> CalcResult:
    dirty = clean + accrued
    d_days, d_years = macaulay_duration(spec, flows, ytm)
    md = modified_duration(d_years, ytm)
    return CalcResult(
        clean_price=clean,
        accrued=accrued,
        dirty_price=dirty,
        ytm=ytm,
        duration_days=d_days,
        duration_years=d_years,
        mod_duration=md,
        pvbp=pvbp(dirty, md),
        convexity=convexity(spec, flows, ytm),
        flows=flows,
    )
