#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D06 Global Forest Watch (GFW) 官方数据 API 采集器

站点/API: https://data-api.globalforestwatch.org  (ReDoc / OpenAPI)
匿名可访问:
  - 目录:  /datasets, /dataset/{d}, /dataset/{d}/{v}, /fields, /stats
  - 取数:  /dataset/{d}/{v}/download/csv?sql=...   <-- 匿名可用(支持 WHERE/GROUP BY)
  - 需Key: /dataset/{d}/{v}/query[/csv], /analysis/zonal  (403)
故本采集器**无需申请 API Key**, 全程走 download/csv。

采集的数据集(地理/统计指标长表, 非争议事件表):
  1. fao_forest_extent  v2020      —— 各国森林面积等(按年)
  2. fao_forest_change  v2020      —— 各国毁林/再造林速率(年段, ha/年)
  3. gadm__glad__iso_weekly_alerts —— 各国 GLAD 森林扰动警报(按年-周, SQL聚合)

输出长表(保留区域编码/年份/周/指标/单位/统计口径/数据版本):
  source_id, dataset, dataset_version, region_level, region_code, region_name,
  year, period, indicator, value, unit, statistical_calibre, source_url, collected_at

用法:
  python d06_spider.py --only fao_forest_extent --out output/_d06_sample.csv
  python d06_spider.py --out output/d06_gfw.csv
"""
import argparse
import csv
import io
import os
import warnings
from datetime import datetime

import requests

warnings.filterwarnings("ignore")

API = "https://data-api.globalforestwatch.org"
CACHE = r"c:\Users\Administrator\Desktop\pdfs\d06_tmp"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

FIELDS = ["source_id", "dataset", "dataset_version", "region_level", "region_code",
          "region_name", "year", "period", "indicator", "value", "unit",
          "statistical_calibre", "source_url", "collected_at"]

DATASETS = [
    {"dataset": "fao_forest_extent", "version": "v2020",
     "id_cols": ["iso", "country", "year", "desk_study"], "year_col": "year",
     "value_cols": ["primary (ha)", "naturally regenerating forest (ha)",
                    "planted forest (ha)", "forest (ha)", "non-forest (ha)",
                    "total land area (ha)"],
     "level": "iso", "unit": "ha",
     "calibre": "FAO Global Forest Resources Assessment; 森林面积等统计(按年)"},
    {"dataset": "fao_forest_change", "version": "v2020",
     "id_cols": ["iso", "country", "year", "desk_study"], "year_col": "year",
     "value_cols": ["reforestation (ha per year)", "forest expansion (ha per year)",
                    "deforestation (ha per year)"],
     "level": "iso", "unit": "ha/year",
     "calibre": "FAO FRA; 年段毁林/再造林速率(ha/年)"},
    {"dataset": "gadm__glad__iso_weekly_alerts", "version": "latest",
     "id_cols": ["iso"], "year_col": "alert__year", "period_col": "alert__week",
     "value_cols": ["alert__count", "alert_area__ha",
                    "whrc_aboveground_co2_emissions__Mg"],
     "units": {"alert__count": "count", "alert_area__ha": "ha",
               "whrc_aboveground_co2_emissions__Mg": "Mg"},
     "level": "iso",
     "sql_template": ("SELECT iso, alert__year, alert__week, {aggs} "
                      "FROM gadm__glad__iso_weekly_alerts "
                      "GROUP BY iso, alert__year, alert__week"),
     "calibre": "GLAD forest alerts; 国家级周度汇总(对全部过滤维度求和)"},
]


def sget(url, params=None, retries=3, timeout=300):
    import time
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            print(f"    HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f"    {type(e).__name__}: {e}")
        time.sleep(2 * (i + 1))
    return None


def resolve_version(ds):
    if ds["version"] != "latest":
        return ds["version"]
    r = sget(f"{API}/dataset/{ds['dataset']}")
    if not r:
        return "latest"
    return sorted(r.json()["data"]["versions"])[-1]


def build_sql(ds):
    if ds.get("sql_template"):
        aggs = ", ".join(f'SUM("{c}") AS "{c}"' for c in ds["value_cols"])
        return ds["sql_template"].format(aggs=aggs)
    return f"SELECT * FROM {ds['dataset']}"


def download_csv(ds, version):
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"{ds['dataset']}__{version}.csv")
    if os.path.exists(p):
        return open(p, encoding="utf-8").read()
    r = sget(f"{API}/dataset/{ds['dataset']}/{version}/download/csv",
             params={"sql": build_sql(ds)})
    if not r:
        return ""
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(r.text)
    return r.text


def num(v):
    v = (v or "").strip()
    if v == "":
        return ""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else f"{f:.6g}"
    except Exception:
        return v


def melt(ds, version, csv_text, now):
    if not csv_text:
        return []
    out = []
    src = f"{API}/dataset/{ds['dataset']}/{version}"
    units = ds.get("units", {})
    for r in csv.DictReader(io.StringIO(csv_text)):
        iso = r.get("iso", "")
        if ds["level"] == "iso":
            code, name = iso, r.get("country", "")
        else:
            parts = [r.get(c) for c in ds["id_cols"] if r.get(c)]
            code, name = "|".join(parts), "|".join(parts)
        year = r.get(ds.get("year_col", "year"), "")
        period = r.get(ds.get("period_col"), "") if ds.get("period_col") else ""
        for col in ds["value_cols"]:
            val = num(r.get(col, ""))
            if val == "":
                continue
            out.append({
                "source_id": "D06", "dataset": ds["dataset"],
                "dataset_version": version, "region_level": ds["level"],
                "region_code": code, "region_name": name, "year": year,
                "period": period, "indicator": col, "value": val,
                "unit": units.get(col, ds.get("unit", "")),
                "statistical_calibre": ds["calibre"], "source_url": src,
                "collected_at": now,
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    ap.add_argument("--out",
                    default=r"c:\Users\Administrator\Desktop\pdfs\output\d06_gfw.csv")
    a = ap.parse_args()

    now = datetime.now().isoformat(timespec="seconds")
    all_rows = []
    for ds in DATASETS:
        if a.only and ds["dataset"] != a.only:
            continue
        ver = resolve_version(ds)
        print(f"[{ds['dataset']}] version={ver} 下载 download/csv ...")
        txt = download_csv(ds, ver)
        rows = melt(ds, ver, txt, now)
        n_src = len(txt.splitlines()) - 1 if txt else 0
        print(f"   -> {len(rows)} 行 (原始 {n_src} 条)")
        all_rows += rows

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(all_rows)
    print(f"\n完成: {len(all_rows)} 行 -> {a.out}")


if __name__ == "__main__":
    main()