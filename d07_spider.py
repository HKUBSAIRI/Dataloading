#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
D07 - 香港劳工处 (Labour Department) 新闻稿采集器
列表: https://www.labour.gov.hk/eng/major/content.php?year=YYYY&page=N  (年份 2000-2026, 每页~50)
详情: https://www.info.gov.hk/gia/general/YYYYMM/DD/P....htm

抽取企业劳动/安全事件要素（涉事雇主、违法类型、罚款、事故及日期），
映射为统一 ESG 争议事件字段，输出 CSV。

用法:
  python d07_spider.py --years 2026 --max-pages 1 --limit-details 8 --out output/_d07_sample.csv
  python d07_spider.py --out output/d07_labour.csv        # 全量 2000-2026
"""
import argparse
import csv
import os
import re
import time
import warnings
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")

LIST = "https://www.labour.gov.hk/eng/major/content.php"
CACHE = r"c:\Users\Administrator\Desktop\pdfs\d07_tmp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en,zh-HK;q=0.9,zh;q=0.8",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}

FIELDS = [
    "source_id", "company_name", "event_date", "publication_date",
    "esg_dimension", "event_type", "event_description", "penalty_amount",
    "case_status", "source_url", "collected_at",
    # D07 扩展
    "headline", "companies", "amounts", "court", "ordinance", "raw_text",
]

# 事件判定
EVENT_RE = re.compile(
    r"fined|convicted|conviction|prosecut|charged|guilty|sanction|penalt|"
    r"wage offence|illegal worker|summons|breach|contraven", re.I)
ACCIDENT_RE = re.compile(
    r"fatal|fatalit|work accident|industrial accident|injur|died|death|"
    r"collapse|struck|fell|trapped", re.I)

# 状态判定（顺序敏感）
STATUS_RULES = [
    (r"fined|convicted|found guilty|pleaded guilty|sentenced", "定罪/罚款"),
    (r"sanction(?:ed|s)?\b", "行政制裁"),
    (r"charged|prosecut|summons|faces charges", "检控"),
    (r"fatal|fatalit|work accident|industrial accident", "事故"),
]

# 公司名/法院名不宜用 re.I（否则 [A-Z] 会匹配小写，导致跨句吞词）
_STOP = {"the", "a", "an", "this", "that", "its", "his", "her", "their",
         "and", "of", "for", "&", "to", "in"}

ORD_RE = re.compile(
    r"(Occupational Safety and Health Ordinance|Factories and Industrial "
    r"Undertakings Ordinance|Employment Ordinance|Employees' Compensation "
    r"Ordinance|Minimum Wage Ordinance|Employment Agency Ordinance|"
    r"Labour Tribunal Ordinance|Trade Unions Ordinance|"
    r"Disability Discrimination Ordinance|Sex Discrimination Ordinance)",
    re.I)
COURT_RE = re.compile(
    r"\b((?:[A-Z][A-Za-z']+\s+){0,3}Magistrates'? Courts?|District Court|"
    r"High Court|Court of First Instance|Court of Appeal|Labour Tribunal)\b")
# 公司名: 由连续"首字母大写词/括号词"组成(允许 of/and/the/& 等连接词), 以公司后缀收尾
_TOKEN = r"(?:[A-Z][A-Za-z0-9&.'’/\-]*|\([A-Za-z0-9&.'’/\-]+\))"
_CONN = r"(?:of|and|the|for|&)"
COMPANY_RE = re.compile(
    rf"\b({_TOKEN}(?:\s+(?:{_TOKEN}|{_CONN})){{0,6}}\s+"
    r"(?:Company\s+Limited|Limited|Ltd\.?|Corporation|Incorporated|Group|plc|LLP))\b")
FINED_RE = re.compile(r"fined\s+(?:a total of\s+)?(?:HK)?\$\s?([\d,]+(?:\.\d+)?)", re.I)
MONEY_RE = re.compile(r"(?:HK)?\$\s?[\d,]+(?:\.\d+)?(?:\s*(?:million|billion))?", re.I)


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


_SUFFIX = re.compile(
    r"\s+(?:Company\s+Limited|Limited|Ltd\.?|Corporation|Incorporated|Group|plc|LLP)$",
    re.I)


def extract_companies(text):
    """抽取公司名并清洗：去重、剔除仅由停用词+后缀构成的结果（如 'The Incorporated'）"""
    out = []
    for m in COMPANY_RE.findall(text):
        m = norm(m)
        if len(m) <= 5:
            continue
        head = _SUFFIX.sub("", m)
        toks = [t for t in re.split(r"\s+", head) if t]
        if not any(t.lower().strip("()") not in _STOP for t in toks):
            continue
        if m not in out:
            out.append(m)
    return out


class Spider:
    def __init__(self, delay=0.8, retries=3, timeout=25):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self._last = 0.0

    def get(self, url, cache_key=None):
        if cache_key:
            p = os.path.join(CACHE, cache_key)
            if os.path.exists(p):
                return open(p, encoding="utf-8").read()
        for i in range(self.retries):
            wait = self.delay - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(url, timeout=self.timeout)
                self._last = time.time()
                if r.status_code == 200:
                    html = r.text
                    if cache_key:
                        os.makedirs(CACHE, exist_ok=True)
                        open(os.path.join(CACHE, cache_key), "w",
                             encoding="utf-8").write(html)
                    return html
            except Exception as e:
                print(f"    retry {i+1}: {str(e)[:60]}")
                time.sleep(1.5 * (i + 1))
        return ""


def parse_list(html, year):
    """解析一年一页的列表 -> [(date, title, url)]"""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for card in soup.select("div.content_card"):
        a = card.find("a", href=True)
        if not a:
            continue
        title = norm(a.get_text(" ", strip=True))
        href = a["href"].strip()
        if href.startswith("javascript"):
            continue
        url = href if href.startswith("http") else urljoin(LIST, href)
        m = re.search(r"(20\d\d-\d\d-\d\d)", card.get_text(" ", strip=True))
        date = m.group(1) if m else ""
        if title and (date or url):
            out.append((date, title, url))
    return out


def max_page(html):
    pages = [int(x) for x in re.findall(r"changePage\((\d+)\)", html)]
    return max(pages) if pages else 1


def classify(title, text):
    blob = (title + " " + text[:400])
    if ACCIDENT_RE.search(title) and not EVENT_RE.search(title):
        return "工伤/安全事故", "事故"
    status = "公告"
    for pat, st in STATUS_RULES:
        if re.search(pat, blob, re.I):
            status = st
            break
    if status == "公告" and EVENT_RE.search(blob):
        status = "检控"
    if re.search(r"fined|convicted|guilty|sanction", blob, re.I):
        etype = "劳动违法（雇佣/安全法例检控）"
    elif ACCIDENT_RE.search(blob):
        etype = "工伤/安全事故"
    elif re.search(r"prosecut|charged|summons|illegal worker|wage offence",
                   blob, re.I):
        etype = "劳动违法（雇佣/安全法例检控）"
    else:
        etype = "公告/其他"
    return etype, status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2000-2026")
    ap.add_argument("--max-pages", type=int, default=0, help="每年最多页数(0=全部)")
    ap.add_argument("--limit-details", type=int, default=0, help="详情抓取上限(0=全部事件类)")
    ap.add_argument("--delay", type=float, default=0.8)
    ap.add_argument("--out", default=r"c:\Users\Administrator\Desktop\pdfs\output\d07_labour.csv")
    a = ap.parse_args()

    if "-" in a.years:
        y0, y1 = a.years.split("-")
        years = list(range(int(y0), int(y1) + 1))
    else:
        years = [int(x) for x in a.years.split(",")]

    sp = Spider(delay=a.delay)
    items, seen = [], set()
    for year in years:
        html = sp.get(f"{LIST}?year={year}&page=1", cache_key=f"list_{year}_1.html")
        if not html:
            print(f"  {year}: 列表首页失败")
            continue
        mp = max_page(html)
        if a.max_pages:
            mp = min(mp, a.max_pages)
        cnt = 0
        for page in range(1, mp + 1):
            h = html if page == 1 else sp.get(
                f"{LIST}?year={year}&page={page}", cache_key=f"list_{year}_{page}.html")
            rows = parse_list(h, year)
            for date, title, url in rows:
                if url in seen:
                    continue
                seen.add(url)
                items.append({"year": year, "date": date, "title": title, "url": url})
                cnt += 1
        print(f"  {year}: {cnt} 条 (共 {mp} 页)")

    print(f"列表合计 {len(items)} 条，开始分类/详情富化 ...")
    now = datetime.now().isoformat(timespec="seconds")
    out_rows, done_det = [], 0
    for it in items:
        is_event = bool(EVENT_RE.search(it["title"]) or ACCIDENT_RE.search(it["title"]))
        text, court, ordn = "", "", ""
        companies, amounts = [], []
        if is_event and (a.limit_details == 0 or done_det < a.limit_details):
            key = re.sub(r"[^\w]", "_", it["url"])[-80:] + ".html"
            h = sp.get(it["url"], cache_key="det_" + key)
            done_det += 1
            if h:
                soup = BeautifulSoup(h, "lxml")
                for t in soup(["script", "style"]):
                    t.decompose()
                text = norm(soup.get_text(" ", strip=True))
                cm = COURT_RE.search(text)
                court = cm.group(1) if cm else ""
                om = ORD_RE.search(text)
                ordn = om.group(1) if om else ""
                companies = extract_companies(text)
                amounts = list(dict.fromkeys(MONEY_RE.findall(text)))
        etype, status = classify(it["title"], text)
        fm = FINED_RE.search(text)
        penalty = ("HK$" + fm.group(1)) if fm else (amounts[0] if amounts and status == "定罪/罚款" else "")
        out_rows.append({
            "source_id": "D07",
            "company_name": "; ".join(companies[:3]),
            "event_date": it["date"],
            "publication_date": it["date"],
            "esg_dimension": "S",
            "event_type": etype,
            "event_description": it["title"],
            "penalty_amount": penalty,
            "case_status": status,
            "source_url": it["url"],
            "collected_at": now,
            "headline": it["title"],
            "companies": "; ".join(companies),
            "amounts": "; ".join(amounts),
            "court": court,
            "ordinance": ordn,
            "raw_text": text[:4000],
        })
        if len(out_rows) % 200 == 0:
            print(f"    ... {len(out_rows)}/{len(items)}")

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out_rows)
    ev = sum(1 for r in out_rows if r["event_type"] != "公告/其他")
    print(f"完成: {len(out_rows)} 条（事件类 {ev}）-> {a.out}")


if __name__ == "__main__":
    main()