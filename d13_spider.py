#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
D13 - 香港廉政公署 (ICAC) 新闻稿采集器
站点: https://www.icac.org.hk/en/p/press/

采集 ICAC 新闻稿列表与详情，抽取企业腐败/贿赂事件要素，
映射为统一 ESG 争议事件字段，输出 CSV。

用法:
  python d13_spider.py --max-pages 2 --limit-details 6 --out output/_d13_sample.csv
  python d13_spider.py --out output/d13_icac.csv
"""
import argparse
import csv
import os
import re
import sys
import time
import warnings
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")

BASE = "https://www.icac.org.hk/en/p/press/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en,zh-HK;q=0.9,zh;q=0.8",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}
ITEM_RE = re.compile(r"index_id_(\d+)\.html")

FIELDS = [
    "source_id", "company_name", "event_date", "publication_date",
    "esg_dimension", "event_type", "event_description", "penalty_amount",
    "case_status", "source_url", "collected_at",
    # D13 扩展
    "press_id", "headline", "companies", "amounts", "status_keywords", "raw_text",
]

STATUS_RULES = [
    (r"sentenced|jailed|imprison|gaol", "判刑"),
    (r"convicted|found guilty|pleaded guilty|guilty of", "定罪/认罪"),
    (r"acquitted|cleared of|not guilty", "无罪"),
    (r"charged|charge[d]? (?:a|the)|faces a count|faces charges", "起诉"),
]


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


class Spider:
    def __init__(self, delay=1.0, retries=3, timeout=25):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self._last = 0.0

    def get(self, url):
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        err = None
        for i in range(self.retries):
            try:
                r = self.s.get(url, timeout=self.timeout, verify=False)
                self._last = time.time()
                if r.status_code == 200:
                    r.encoding = r.apparent_encoding or "utf-8"
                    return r.text
                err = f"HTTP {r.status_code}"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (i + 1))
        raise RuntimeError(f"fetch failed: {url} -> {err}")

    def list_pages_max(self):
        html = self.get(BASE + "index.html")
        ns = [int(m) for m in re.findall(r"index_p(\d+)\.html", html)]
        return max(ns) if ns else 1

    def list_items(self, page, max_pages=None):
        html = None
        if page == 1:
            url = BASE + "index.html"
        else:
            url = BASE + f"index_p{page}.html"
        html = html or self.get(url)
        soup = BeautifulSoup(html, "lxml")
        out, seen = [], set()
        for a in soup.find_all("a", href=True):
            m = ITEM_RE.search(a["href"])
            if not m:
                continue
            full = urljoin(url, a["href"])
            if full in seen:
                continue
            seen.add(full)
            li = a.find_parent(["li", "div", "tr"])
            text = norm(li.get_text(" ", strip=True)) if li else norm(a.get_text(" ", strip=True))
            dm = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
            date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}" if dm else ""
            headline = norm(a.get_text(" ", strip=True))
            out.append({"id": m.group(1), "url": full, "date": date, "headline": headline})
        return out

    @staticmethod
    def _extract(text):
        # 企业名: 连续首字母大写词组 + 公司后缀 (避免吞入前面小写连接词)
        companies = sorted(set(re.findall(
            r"((?:[A-Z][\w&.'\-]*\s+){1,5}"
            r"(?:Limited|Ltd|Corporation|Company|Holdings|Group|Securities|Bank|"
            r"Enterprise|Developers|LLP|Inc|plc))", text)))
        amounts = sorted(set(re.findall(
            r"(?:HK)?\$\s?[\d,]+(?:\.\d+)?(?:\s?(?:million|billion))?", text)))
        status = "公告"
        for pat, label in STATUS_RULES:
            if re.search(pat, text, re.I):
                status = label
                break
        kw = [label for pat, label in STATUS_RULES if re.search(pat, text, re.I)]
        fine = re.search(r"fine[ds]?\s*(?:of\s*)?((?:HK)?\$\s?[\d,]+(?:\.\d+)?(?:\s?million)?)", text, re.I)
        return companies, amounts, status, kw, (fine.group(1) if fine else "")

    def parse_detail(self, item):
        html = self.get(item["url"])
        soup = BeautifulSoup(html, "lxml")
        h = soup.find(["h1", "h2"])
        headline = norm(h.get_text(" ", strip=True)) if h else item["headline"]
        body = (soup.find("div", class_=re.compile(r"(content|news|press|detail|main)", re.I))
                or soup.body or soup)
        raw = norm(body.get_text(" ", strip=True))
        companies, amounts, status, kw, fine = self._extract(raw)
        row = {k: "" for k in FIELDS}
        row.update({
            "source_id": "D13",
            "company_name": companies[0] if companies else "",
            "event_date": item["date"],
            "publication_date": item["date"],
            "esg_dimension": "G",
            "event_type": "腐败/贿赂案件",
            "event_description": headline,
            "penalty_amount": fine,
            "case_status": status,
            "source_url": item["url"],
            "collected_at": datetime.now().isoformat(timespec="seconds"),
            "press_id": item["id"],
            "headline": headline,
            "companies": " | ".join(companies),
            "amounts": " | ".join(amounts),
            "status_keywords": " | ".join(kw),
            "raw_text": raw[:6000],
        })
        return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--limit-details", type=int, default=None)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--out", default="output/d13_icac.csv")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    sp = Spider(delay=args.delay)
    last = sp.list_pages_max()
    pages = range(1, last + 1)
    if args.max_pages:
        pages = range(1, min(last, args.max_pages) + 1)
    print(f"分页共 {last} 页，本次采集 {list(pages)}")

    done = set()
    header = not os.path.exists(args.out)
    if not header:
        with open(args.out, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                done.add(r.get("source_url", ""))

    items = []
    for p in pages:
        got = sp.list_items(p)
        print(f"  第{p}页: {len(got)} 条")
        items += got
    if args.limit_details:
        items = items[:args.limit_details]

    n, err = 0, 0
    with open(args.out, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if header:
            w.writeheader()
        for i, it in enumerate(items, 1):
            if it["url"] in done:
                continue
            try:
                row = sp.parse_detail(it)
                w.writerow(row)
                f.flush()
                done.add(it["url"])
                n += 1
                if i <= 5 or i % 25 == 0:
                    print(f"  [{i}/{len(items)}] {row['publication_date']} | {row['case_status']:6s} | {row['headline'][:50]}")
            except Exception as e:
                err += 1
                print(f"  [ERR] {it['url']} -> {e}")

    print(f"\n完成: 新增 {n} 条, 错误 {err} -> {args.out}")


if __name__ == "__main__":
    main()