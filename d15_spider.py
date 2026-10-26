#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D15 Green NCAP 车辆环境评估采集器

站点: https://www.greenncap.com  (robots: Allow /)
两个公开、服务端渲染的入口(无需浏览器/API Key):
  1) 星级评估表 : https://www.greenncap.com/assessments/
        - 每车: 可持续性星级(0-5)/清洁空气指数/能效指数/温室气体指数(0-10)/平均分%/
          续驶与能耗/冬季性能/充电能力(Driving Experience 星级) + 车型/年份/类别/动力/变速
  2) 欧洲 LCA 结果 : https://www.greenncap.com/european-lca-results/
        - 每车: 全生命周期温室气体排放(平均/最优/最差, 吨 CO2-eq)、
          一次能源需求(平均/最优/最差, MWh) + LCA Fact Sheet PDF 链接

输出长表(车辆环境评估指标表, 非争议事件表):
  source_id, dataset, dataset_version, region_level, region_code, region_name,
  year, period, indicator, value, unit, statistical_calibre, source_url, collected_at,
  vehicle_id, make, model, variant, vehicle_class, engine, gear, powertrain,
  award, verified, detail_url, fact_sheet_url

用法:
  python d15_spider.py --out output/d15_greenncap.csv
"""
import argparse
import csv
import os
import re
import time
import warnings
from datetime import datetime

import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")
requests.packages.urllib3.disable_warnings()

ASSESS = "https://www.greenncap.com/assessments/"
LCA = "https://www.greenncap.com/european-lca-results/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

FIELDS = ["source_id", "dataset", "dataset_version", "region_level", "region_code",
          "region_name", "year", "period", "indicator", "value", "unit",
          "statistical_calibre", "source_url", "collected_at",
          "vehicle_id", "make", "model", "variant", "vehicle_class", "engine",
          "gear", "powertrain", "award", "verified", "detail_url", "fact_sheet_url"]


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def fetch(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=60, verify=False)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    return ""


def slug_of(href):
    m = re.search(r"/([^/]+)/?$", href or "")
    return m.group(1) if m else ""


def cell_sort(td):
    return td.get("data-sort-value") if td is not None else None


def parse_ratings(html, now):
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("tr[data-make]")
    out = []
    for r in rows:
        href = r.get("data-href") or ""
        vid = slug_of(href) or r.get("data-id") or ""
        car = r.select_one("td.car")
        variant = ""
        if car:
            cn = car.select_one(".car-name")
            cv = car.select_one(".car-variant")
            variant = norm(cv.get_text(" ", strip=True)) if cv else ""
        base = {
            "vehicle_id": vid, "make": r.get("data-make") or "",
            "model": r.get("data-model") or "", "variant": variant,
            "vehicle_class": (r.select_one("td.class").get_text(strip=True)
                              if r.select_one("td.class") else ""),
            "engine": r.get("data-engine") or "", "gear": r.get("data-gear") or "",
            "powertrain": r.get("data-engine") or "",
            "award": r.get("data-award") or "", "verified": r.get("data-verified") or "",
            "detail_url": href, "fact_sheet_url": "",
        }
        yr = r.get("data-year") or ""
        inds = [
            ("sustainability_rating", r.select_one("td.rating"), "stars (0-5)"),
            ("clean_air_index", r.select_one("td.clean"), "index (0-10)"),
            ("energy_efficiency_index", r.select_one("td.energy"), "index (0-10)"),
            ("greenhouse_gases_index", r.select_one("td.greenhouse"), "index (0-10)"),
            ("average_score", r.select_one("td.average"), "%"),
            ("dx_consumption_range", r.select_one("td.consumption-range"), "stars"),
            ("dx_cold_winter_performance", r.select_one("td.cold-winter-performance"), "stars"),
            ("dx_charging_capability", r.select_one("td.charging-capability"), "stars"),
        ]
        for ind, td, unit in inds:
            v = cell_sort(td)
            if v in (None, "", "N/A"):
                continue
            out.append(dict(base, source_id="D15",
                            dataset="Green NCAP assessment",
                            dataset_version=yr, region_level="EU",
                            region_code="EU", region_name="Europe",
                            year=yr, period="", indicator=ind, value=v, unit=unit,
                            statistical_calibre="Green NCAP 星级评估(清洁空气/能效/温室气体指数)",
                            source_url=ASSESS, collected_at=now))
    return out


def parse_lca(html, now):
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("tr[data-make]")
    out = []
    for r in rows:
        vid = r.get("id") or ""
        car = r.select_one("td.car")
        variant = ""
        if car:
            cv = car.select_one(".car-variant")
            variant = norm(cv.get_text(" ", strip=True)) if cv else ""
        pdf = r.find("a", href=re.compile(r"\.pdf", re.I))
        pdf_url = pdf["href"] if pdf else ""
        tds = r.find_all("td")
        car_td = r.select_one("td.car") or (tds[0] if tds else None)
        nums = [td for td in tds if td is not car_td and td.get("data-sort-value") is not None]
        base = {
            "vehicle_id": vid, "make": r.get("data-make") or "",
            "model": r.get("data-model") or "", "variant": variant,
            "vehicle_class": r.get("data-class") or "",
            "engine": r.get("data-engine") or "", "gear": r.get("data-gear") or "",
            "powertrain": r.get("data-engine") or "",
            "award": r.get("data-award") or "", "verified": r.get("data-verified") or "",
            "detail_url": ASSESS, "fact_sheet_url": pdf_url,
        }
        yr = r.get("data-year") or ""
        names = [("lca_ghg_average", "tons CO2-eq"), ("lca_ghg_best", "tons CO2-eq"),
                 ("lca_ghg_worst", "tons CO2-eq"),
                 ("lca_primary_energy_average", "MWh"), ("lca_primary_energy_best", "MWh"),
                 ("lca_primary_energy_worst", "MWh")]
        for (ind, unit), td in zip(names, nums):
            v = td.get("data-sort-value")
            if v in (None, ""):
                continue
            out.append(dict(base, source_id="D15",
                            dataset="Green NCAP European LCA",
                            dataset_version=yr, region_level="EU",
                            region_code="EU", region_name="Europe",
                            year=yr, period="", indicator=ind, value=v, unit=unit,
                            statistical_calibre="Green NCAP 欧洲全生命周期评估(净零排放情景)",
                            source_url=LCA, collected_at=now))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=r"c:\Users\Administrator\Desktop\pdfs\output\d15_greenncap.csv")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")

    print("抓取星级评估表 ...")
    rows_r = parse_ratings(fetch(ASSESS), now)
    print(f"  -> {len(rows_r)} 条指标行")
    print("抓取欧洲 LCA 结果 ...")
    rows_l = parse_lca(fetch(LCA), now)
    print(f"  -> {len(rows_l)} 条指标行")

    all_rows = rows_r + rows_l
    with open(a.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(all_rows)
    vids = {r["vehicle_id"] for r in all_rows}
    print(f"\n完成: {len(all_rows)} 行 | 车辆 {len(vids)} 款 -> {a.out}")


if __name__ == "__main__":
    main()