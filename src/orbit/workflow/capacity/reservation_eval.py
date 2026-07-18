"""Offline evaluation for optional dynamic Reservation estimators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

@dataclass(frozen=True)
class ReservationEstimateCase:
    static_upper_bound:int;estimated:int;actual:int
@dataclass(frozen=True)
class ReservationEstimateReport:
    samples:int;mean_absolute_error:float;under_reservation_rate:float;over_reservation_rate:float;safe:bool

def bounded_estimate(static_upper_bound:int,dynamic_estimate:int)->int:
    if static_upper_bound<0 or dynamic_estimate<0:raise ValueError("estimates must be non-negative")
    return min(static_upper_bound,dynamic_estimate)

def evaluate_estimator(cases:Iterable[ReservationEstimateCase],*,maximum_under_rate:float=.05)->ReservationEstimateReport:
    values=tuple(cases)
    if not values:raise ValueError("estimator evaluation needs cases")
    bounded=tuple((bounded_estimate(item.static_upper_bound,item.estimated),item.actual,item.static_upper_bound) for item in values)
    mae=sum(abs(estimate-actual) for estimate,actual,_ in bounded)/len(bounded);under=sum(estimate<actual for estimate,actual,_ in bounded)/len(bounded);over=sum(estimate>actual for estimate,actual,_ in bounded)/len(bounded)
    return ReservationEstimateReport(len(values),mae,under,over,under<=maximum_under_rate and all(estimate<=upper for estimate,_,upper in bounded))
