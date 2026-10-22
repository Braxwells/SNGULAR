"""Reproduce el scorecard de Grafana a partir de la salida JSON cruda de k6.

Util cuando no se tiene el stack de Grafana/InfluxDB delante: ejecuta las MISMAS queries
que los paneles del dashboard y dice directamente si cada uno saldria verde o rojo.

    k6 run --out json=raw.json --summary-export summary.json platform/k6/test.js
    python tools/score_k6.py raw.json [summary.json]
"""
import json
import os
import statistics
import sys
from collections import defaultdict

path = sys.argv[1] if len(sys.argv) > 1 else "raw.json"
summary_path = sys.argv[2] if len(sys.argv) > 2 else "summary.json"
dur = defaultdict(list)      # http_req_duration por grupo de URL
failed_total = failed_sum = 0
reqs = 0
vus_max = 0
t_min = t_max = None

def bucket(url: str) -> str:
    if "/process" in url:
        return "process"
    if url.endswith("/v1/requests"):
        return "create"
    return "status"

with open(path, encoding="utf-8") as fh:
    for line in fh:
        try:
            point = json.loads(line)
        except ValueError:
            continue
        if point.get("type") != "Point":
            continue
        metric = point.get("metric")
        data = point["data"]
        if metric == "http_req_duration":
            dur[bucket(data["tags"].get("url", ""))].append(data["value"])
        elif metric == "http_req_failed":
            failed_total += 1
            failed_sum += data["value"]
        elif metric == "http_reqs":
            reqs += data["value"]
            ts = data["time"]
            t_min = ts if t_min is None or ts < t_min else t_min
            t_max = ts if t_max is None or ts > t_max else t_max
        elif metric == "vus":
            vus_max = max(vus_max, data["value"])

def pct(values, p):
    values = sorted(values)
    if not values:
        return 0.0
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)

availability = (failed_total - failed_sum) / failed_total * 100 if failed_total else 0.0
proc = dur["process"]
others = dur["create"] + dur["status"]

print("=" * 74)
print("SCORECARD (replica exacta de las queries de Grafana)")
print("=" * 74)
print(f"DISPONIBILIDAD GLOBAL / RESILIENCIA : {availability:6.2f} %   "
      f"[verde >= 99]  -> {'VERDE (ALTA)' if availability >= 99 else 'ROJO (BAJA)'}")
print(f"  peticiones totales                : {failed_total}  (fallidas: {int(failed_sum)})")
print()
mean_proc = statistics.fmean(proc) if proc else 0.0
print(f"1. INDICADOR DE ARQUITECTURA        : mean(/process) = {mean_proc:7.2f} ms   "
      f"[ASYNC si < 500]  -> {'ASYNC (VERDE)' if mean_proc <= 500 else 'SYNC (ROJO)'}")
print()
print("4. ESTABILIDAD: PERCENTILES sobre /process")
for label, value in (("p50", pct(proc, 50)), ("p95", pct(proc, 95)), ("p99", pct(proc, 99)),
                     ("max", max(proc) if proc else 0)):
    print(f"     {label:>4} = {value:8.2f} ms")
print()
print("5. DELTA DE EFICIENCIA (OVERHEAD)")
print(f"     Core Service (/process)        : {mean_proc:7.2f} ms  (n={len(proc)})")
print(f"     Resto de endpoints             : {statistics.fmean(others) if others else 0:7.2f} ms  (n={len(others)})")
for name in ("create", "status"):
    values = dur[name]
    if values:
        print(f"       - {name:<8} mean={statistics.fmean(values):6.2f} ms  "
              f"p95={pct(values, 95):6.2f} ms  n={len(values)}")
print(f"2. RENDIMIENTO                      : {reqs} peticiones")
if os.path.exists(summary_path):
    with open(summary_path, encoding="utf-8") as fh:
        summary = json.load(fh)
    rate = summary["metrics"]["http_reqs"].get("rate")
    checks = summary["metrics"].get("checks", {})
    if rate:
        print(f"     throughput medio               : {rate:.1f} RPS")
    if checks:
        total = checks.get("passes", 0) + checks.get("fails", 0)
        print(f"     checks k6                      : {checks.get('passes', 0)}/{total} OK "
              f"({checks.get('value', 0) * 100:.2f} %)")
print(f"CONCURRENCIA MAXIMA (VUs)           : {int(vus_max)}")
print("=" * 74)
