"""Reproducible single-machine latency/throughput capacity harness."""

from __future__ import annotations

from dataclasses import dataclass
import platform
import statistics
import time
from typing import Callable

@dataclass(frozen=True)
class SLO:
    p95_ms:float;p99_ms:float;minimum_throughput:float
@dataclass(frozen=True)
class CapacityReport:
    workload:str;samples:int;p50_ms:float;p95_ms:float;p99_ms:float;throughput_per_second:float;passed:bool;hardware:str

class CapacityHarness:
    @staticmethod
    def run(workload:str,operation:Callable[[],None],*,samples:int,slo:SLO)->CapacityReport:
        if samples<2:raise ValueError("capacity harness requires at least two samples")
        values=[];started=time.perf_counter()
        for _ in range(samples):
            before=time.perf_counter();operation();values.append((time.perf_counter()-before)*1000)
        duration=time.perf_counter()-started;ordered=sorted(values)
        quantile=lambda p:ordered[min(len(ordered)-1,max(0,int(len(ordered)*p)-1))]
        p50,p95,p99=statistics.median(ordered),quantile(.95),quantile(.99);throughput=samples/max(duration,1e-9)
        return CapacityReport(workload,samples,p50,p95,p99,throughput,p95<=slo.p95_ms and p99<=slo.p99_ms and throughput>=slo.minimum_throughput,f"{platform.system()} {platform.machine()} Python {platform.python_version()}")

