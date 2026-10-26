#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D16 香港政府统计处 (Census & Statistics Department) 统计指标采集器

官方入口(全部匿名可用, 无需 API Key):
  1) 统计表清单      : https://data.gov.hk/en-data/api/3/action/package_search  (CKAN)
                       -> 过滤名称 hk-censtatd-tablechart-<表格号>
  2) 表格元数据      : https://www.censtatd.gov.hk/data/table_<id>_comp.json   (sv/sp/cv 结构)
                       https://www.censtatd.gov.hk/data/en/table_<id>_lang.json (标签/单位)
  3) 官方统计 API    : POST https://www.censtatd.gov.hk/api/post.php
                       form: query = {"id","lang","period":{start,end},"sv":{...},"cv":{...}}
                       返回 dataSet: [{freq, period, sv, svDesc, figure, sd_value, <cv>, <cv>Desc...}]

输出长表(宏观背景指标表, 非争议事件表):
  source_id, dataset, dataset_version, region_level, region_code, region_name,
  year, period, indicator, value, unit, statistical_calibre, source_url, collected_at,
  table_id, table_title, freq, sv_code, sv_desc, measure, dimensions, sd_value

用法:
  python d16_spider.py --limit 5 --out output/_d16_sample.csv
  python d16_spider.py --out output/d16_censtatd.csv
"""
import argparse
import csv
import json
import os
import re
import time
import warnings
from datetime import datetime

import requests

warnings.filterwarnings("ignore")
requests.packages.urllib3.disable_warnings()

CACHE = r"c:\Users\Administrator\Desktop\pdfs\d16_tmp"
API = "https://www.censtatd.gov.hk/api/post.php"
COMP = "https://www.censtatd.gov.hk/data/table_{id}_comp.json"
LANG = "https://www.censtatd.gov.hk/data/en/table_{id}_lang.json"
CKAN = "https://data.gov.hk/en-data/api/3/action/package_search"
TABLE_URL = "https://www.censtatd.gov.hk/en/web_table.html?id={id}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

FIELDS = ["source_id", "dataset", "dataset_version", "region_level", "region_code",
          "region_name", "year", "period", "indicator", "value", "unit",
          "statistical_calibre", "source_url", "collected_at",
          "table_id", "table_title", "freq", "sv_code", "sv_desc", "measure",
          "dimensions", "sd_value"]

TIME_CVS = {"CCYY", "Q", "M", "H", "Y", "D"}
END = {"Y": "2030", "Q": "203012", "M": "203012", "H": "203012"}


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


class Spider:
    def __init__(self, delay=0.6, retries=3, timeout=60):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self._last = 0.0

    def _pace(self):
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)

    def get_json(self, url):
        self._pace()
        for i in range(self.retries):
            try:
                r = self.s.get(url, timeout=self.timeout, verify=False)
                self._last = time.time()
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 404:
                    return None
            except Exception:
                pass
            time.sleep(1.5 * (i + 1))
        return None

    def post_api(self, query):
        self._pace()
        body = {"query": json.dumps(query, ensure_ascii=False)}
        for i in range(self.retries):
            try:
                r = self.s.post(API, data=body, timeout=self.timeout, verify=False)
                self._last = time.time()
                if r.status_code == 200:
                    return r.json()
            except Exception:
                pass
            time.sleep(1.5 * (i + 1))
        return None

    # ---- 表清单 ----
    def list_table_ids(self):
        ids, start = [], 0
        while True:
            self._pace()
            r = self.s.get(CKAN, params={"q": "*:*", "rows": 500, "start": start},
                           timeout=self.timeout, verify=False)
            self._last = time.time()
            j = r.json()["result"]
            res = j["results"]
            if not res:
                break
            for p in res:
                m = re.match(r"hk-censtatd-tablechart-(\d{3}-\d{5})$", p["name"])
                if m:
                    ids.append(m.group(1))
            start += len(res)
            if start >= j["count"]:
                break
        return sorted(set(ids))


def build_cv(lang):
    cv, descs = {}, {}
    for k, v in (lang.get("cv_list") or {}).items():
        if k in TIME_CVS:
            continue
        codes = []
        for g in (v.get("ccg_list") or {}).values():
            codes += list((g.get("cc_list") or {}).keys())
        if codes:
            cv[k] = codes
            descs[k] = norm(v.get("def_class_desc") or k)
    return cv, descs


def period_attempts(comp, since):
    ccgs = set((comp.get("table_component_ccg_list") or {}).keys())
    out = []
    if "CCYY" in ccgs:
        out.append({"start": since, "end": END["Y"]})
    if "Q" in ccgs:
        out.append({"start": since + "03", "end": END["Q"]})
    if "M" in ccgs or "H" in ccgs:
        out.append({"start": since + "01", "end": END["H"]})
    if not out:
        out.append({"start": since, "end": END["Y"]})
    # 去重
    seen, uniq = set(), []
    for p in out:
        key = (p["start"], p["end"])
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def dims_of(rec, cv_descs):
    d = {}
    for k in cv_descs:
        v = rec.get(k)
        if v not in ("", None):
            d[k] = {"code": v, "desc": norm(rec.get(k + "Desc", ""))}
    return json.dumps(d, ensure_ascii=False) if d else ""


def melt(tid, comp, lang, records, now):
    sv_desc_map = {k: norm(v.get("def_stat_desc") or k)
                   for k, v in (lang.get("sv_list") or {}).items()}
    _, cv_descs = build_cv(lang)
    title = norm(lang.get("tb_title") or "")
    ver = comp.get("last_modified_date") or ""
    src = TABLE_URL.format(id=tid)
    rows = []
    for rec in records:
        fig = rec.get("figure")
        if fig in ("", None):
            continue
        per = str(rec.get("period") or "")
        sv = rec.get("sv") or ""
        rows.append({
            "source_id": "D16",
            "dataset": f"censtatd_web_table_{tid}",
            "dataset_version": ver,
            "region_level": "HKG", "region_code": "HK", "region_name": "Hong Kong",
            "year": per[:4], "period": per,
            "indicator": sv_desc_map.get(sv, sv),
            "value": fig,
            "unit": norm(rec.get("svDesc") or ""),
            "statistical_calibre": title,
            "source_url": src, "collected_at": now,
            "table_id": tid, "table_title": title,
            "freq": rec.get("freq") or "",
            "sv_code": sv, "sv_desc": sv_desc_map.get(sv, sv),
            "measure": norm(rec.get("svDesc") or ""),
            "dimensions": dims_of(rec, cv_descs),
            "sd_value": rec.get("sd_value") or "",
        })
    return rows


def fetch_table(sp, tid, now):
    os.makedirs(CACHE, exist_ok=True)
    cp = os.path.join(CACHE, f"table_{tid}.json")
    if os.path.exists(cp):
        try:
            obj = json.load(open(cp, encoding="utf-8"))
            return melt(tid, obj["comp"], obj["lang"], obj["records"], now)
        except Exception:
            pass
    comp = sp.get_json(COMP.format(id=tid))
    lang = sp.get_json(LANG.format(id=tid))
    if not comp or not lang:
        json.dump({"comp": comp or {}, "lang": lang or {}, "records": []},
                  open(cp, "w", encoding="utf-8"), ensure_ascii=False)
        return []
    sv = {}
    for it in comp.get("table_component_list") or []:
        sv.setdefault(it["stat_var"], []).append(it["stat_pres"])
    if not sv:
        json.dump({"comp": comp, "lang": lang, "records": []},
                  open(cp, "w", encoding="utf-8"), ensure_ascii=False)
        return []
    cv, _ = build_cv(lang)
    since = str(comp.get("since") or "1961")
    since = re.sub(r"\D", "", since)[:4] or "1961"

    records, seen = [], set()
    for period in period_attempts(comp, since):
        j = sp.post_api({"id": tid, "lang": "en", "period": period,
                         "sv": sv, "cv": cv})
        if not j:
            continue
        for rec in j.get("dataSet", []) or []:
            key = (rec.get("period"), rec.get("sv"), rec.get("svDesc"),
                   rec.get("freq"), dims_of(rec, build_cv(lang)[1]))
            if key in seen:
                continue
            seen.add(key)
            records.append(rec)
    json.dump({"comp": comp, "lang": lang, "records": records},
              open(cp, "w", encoding="utf-8"), ensure_ascii=False)
    return melt(tid, comp, lang, records, now)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--delay", type=float, default=0.6)
    ap.add_argument("--out", default=r"c:\Users\Administrator\Desktop\pdfs\output\d16_censtatd.csv")
    a = ap.parse_args()

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    sp = Spider(delay=a.delay)
    now = datetime.now().isoformat(timespec="seconds")

    print("获取 C&SD 统计表清单 (data.gov.hk CKAN) ...")
    ids = sp.list_table_ids()
    print(f"  共 {len(ids)} 个统计表")
    if a.limit:
        ids = ids[:a.limit]

    all_rows, ok, empty, err = [], 0, 0, 0
    for i, tid in enumerate(ids, 1):
        try:
            rows = fetch_table(sp, tid, now)
        except Exception as e:
            rows = []
            err += 1
            print(f"  [{i}/{len(ids)}] {tid} ERR {type(e).__name__}: {e}")
        if rows:
            ok += 1
            all_rows += rows
            if i <= 5 or i % 10 == 0:
                print(f"  [{i}/{len(ids)}] {tid}: +{len(rows)} 行 (累计 {len(all_rows)})")
        else:
            empty += 1
            if i <= 5:
                print(f"  [{i}/{len(ids)}] {tid}: 0 行")

    with open(a.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(all_rows)
    print(f"\n完成: {len(all_rows)} 行 | 有数据表 {ok} | 空表 {empty} | 出错 {err} -> {a.out}")


if __name__ == "__main__":
    main()