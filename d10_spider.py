#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
D10 - 国家市场监督管理总局缺陷产品召回技术中心 采集器
站点: https://www.samrdprc.org.cn

用途: 采集汽车召回 / 消费品召回 / 特种设备召回的公告与新闻，
      解析为统一的 ESG 争议事件字段结构，输出 CSV。

用法:
  python d10_spider.py --columns qczh_ann --max-pages 1 --limit-details 5   # 小样本验证
  python d10_spider.py                                                     # 全量采集
  python d10_spider.py --columns qczh_ann,xfp --delay 1.0
"""
import argparse
import csv
import json
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

BASE = "https://www.samrdprc.org.cn"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# 栏目配置: list=列表目录, category=产品类别, kind 由页面表格自动判定
COLUMNS = {
    "qczh_ann":  {"name": "汽车-国内-公告", "path": "/qczh/qczhgg1/",   "category": "汽车"},
    "qczh_news": {"name": "汽车-国内-新闻", "path": "/qczh/gnzhqc/",    "category": "汽车"},
    "xfp":       {"name": "消费品-国内",    "path": "/xfpzh/xfpgnzh/",  "category": "消费品"},
    "tzsb":      {"name": "特种设备-公告",  "path": "/tzsb/",           "category": "特种设备"},
}

DETAIL_RE = re.compile(r"/\d{6}/t\d{8}_\d+\.html")

# 统一输出字段
FIELDS = [
    "source_id", "company_name", "event_date", "publication_date",
    "esg_dimension", "event_type", "event_description", "penalty_amount",
    "case_status", "source_url", "collected_at",
    # D10 扩展字段
    "recall_no", "category", "region", "brand_model", "recall_quantity",
    "vehicle_count", "manufacture_date_range", "vin_range",
    "defect_desc", "hazard", "remedy", "extra_json", "raw_text",
]


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def cn_date(y, m, d):
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


# 结构化表格字段名归一: 不同栏目(汽车公告/消费品公告)用词不同
KEY_ALIASES = {
    "company_name":    ["生产者名称", "企业名称", "召回负责机构"],
    "recall_no":       ["召回编号"],
    "recall_quantity": ["召回车辆总数量", "涉及数量", "召回数量"],
    "defect_desc":     ["缺陷描述", "存在的缺陷"],
    "hazard":          ["可能造成的后果", "可能导致的后果", "可能产生的后果"],
    "remedy":          ["召回维修措施", "具体召回措施"],
    "model":           ["型号/规格", "型号／规格"],
    "event_time":      ["召回实施时间", "召回进度安排"],
    "prod_time":       ["生产起止日期"],
}


def pick(fields, key):
    for alias in KEY_ALIASES.get(key, []):
        if fields.get(alias):
            return fields[alias]
    return ""


def cn_range_to_iso(text):
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日*至\s*"
                  r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text or "")
    if not m:
        return ""
    g = m.groups()
    return f"{cn_date(*g[:3])} 至 {cn_date(*g[3:])}"


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
        last_err = None
        for i in range(self.retries):
            try:
                r = self.s.get(url, timeout=self.timeout, verify=False)
                self._last = time.time()
                if r.status_code == 200:
                    r.encoding = r.apparent_encoding or "utf-8"
                    return r.text
                last_err = f"HTTP {r.status_code}"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (i + 1))
        raise RuntimeError(f"fetch failed: {url} -> {last_err}")

    # ---------- 列表 ----------
    def list_items(self, path, max_pages=None):
        """返回 [(title, detail_url, pub_date)]，按页顺序"""
        index_url = urljoin(BASE, path + "index.html")
        html = self.get(index_url)
        m = re.search(r"countPage\s*=\s*(\d+)", html)
        count_page = int(m.group(1)) if m else 1
        if max_pages:
            count_page = min(count_page, max_pages)
        items, seen = [], set()
        for p in range(count_page):
            url = index_url if p == 0 else urljoin(BASE, f"{path}index_{p}.html")
            page_html = html if p == 0 else self.get(url)
            for t, u, d in self._parse_list(page_html, url):
                if u not in seen:
                    seen.add(u)
                    items.append((t, u, d))
        return items, count_page

    @staticmethod
    def _parse_list(html, url):
        soup = BeautifulSoup(html, "lxml")
        out = []
        for li in soup.find_all("li"):
            a = li.find("a", href=True)
            if not a or not DETAIL_RE.search(a["href"]):
                continue
            if a["href"].startswith("../"):        # 跨栏目公告图标，跳过
                continue
            title = a.get_text(strip=True)
            if not title:
                continue
            span = li.find("span")
            date = ""
            if span:
                mm = re.search(r"\d{4}-\d{2}-\d{2}", span.get_text())
                date = mm.group(0) if mm else ""
            out.append((title, urljoin(url, a["href"]), date))
        return out

    # ---------- 详情解析 ----------
    @staticmethod
    def _content(soup):
        node = (soup.find("div", class_=re.compile(r"TRS_Editor", re.I))
                or soup.find("div", class_=re.compile(r"(content|detail|article|zoom)", re.I)))
        return node or soup.body or soup

    @staticmethod
    def _parse_table(content):
        """结构化公告表 -> (fields, vehicles)"""
        fields, vehicles = {}, []
        for tr in content.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            texts = [norm(td.get_text(" ", strip=True)) for td in tds]
            n = len(tds)
            if n == 5:  # 车型子表: 表头或数据行
                if texts[0] in ("车型系列", "车型名称"):
                    continue
                vehicles.append({
                    "series": texts[0], "model": texts[1], "qty": texts[2],
                    "prod_range": texts[3], "vin_range": texts[4],
                })
                continue
            # 键值行: 每 2 个单元格为一对 (2/4/6 单元格), 兼容一行两对
            if n % 2 == 0:
                for i in range(0, n, 2):
                    k, v = texts[i], texts[i + 1]
                    if k and k != "关闭":
                        fields[k] = v
            elif n == 3:
                if texts[0] and texts[0] != "关闭":
                    fields[texts[0]] = texts[1]
        return fields, vehicles

    @staticmethod
    def _parse_narrative(text, title):
        d = {}
        m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9（）()·]+?(?:股份有限公司|有限公司|集团|公司|厂))", text)
        d["company_name"] = m.group(1) if m else ""
        m = re.search(r"([SZT]\d{4}[A-Z]\d{3,5}[A-Z]?)", text)
        d["recall_no"] = m.group(1) if m else ""
        m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日至\s*"
                      r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
        if m:
            g = m.groups()
            d["manufacture_date_range"] = f"{cn_date(*g[:3])} 至 {cn_date(*g[3:])}"
        m = (re.search(r"涉及数量为\s*([\d,，]+)", text)
             or re.search(r"共计\s*([\d,，]+)\s*辆", text)
             or re.search(r"数量\s*([\d,，]+)\s*[双件口台个只张条]", text)
             or re.search(r"共计\s*([\d,，]+)", text))
        d["recall_quantity"] = m.group(1).replace("，", ",") if m else ""
        m = re.search(r"型号[/／]规格[:：]\s*([^，。；]+)", text)
        val = norm(m.group(1)) if m else ""
        while val and val[-1] in "）)" and (val.count("）") + val.count(")")) > (val.count("（") + val.count("(")):
            val = val[:-1].rstrip()
        d["brand_model"] = val
        m = re.search(r"【(.+?)】", title)
        d["region"] = m.group(1) if m else ""
        return d

    def parse_detail(self, col, title, url, pub_date):
        html = self.get(url)
        soup = BeautifulSoup(html, "lxml")
        content = self._content(soup)
        fields, vehicles = self._parse_table(content)
        raw_text = norm(content.get_text(" ", strip=True))
        cat = col["category"]
        row = {k: "" for k in FIELDS}
        row.update({
            "source_id": "D10",
            "category": cat,
            "publication_date": pub_date,
            "esg_dimension": "S",
            "event_type": f"产品召回（{cat}）",
            "penalty_amount": "",
            "case_status": "已实施召回",
            "source_url": url,
            "collected_at": datetime.now().isoformat(timespec="seconds"),
            "raw_text": raw_text[:5000],
        })

        if fields.get("生产者名称"):        # 结构化公告（汽车/消费品通用）
            row["company_name"] = pick(fields, "company_name")
            row["recall_no"] = pick(fields, "recall_no")
            if not row["recall_no"]:
                m = re.search(r"([SZT]\d{4}[A-Z]\d{3,5}[A-Z]?)", raw_text)
                row["recall_no"] = m.group(1) if m else ""
            row["recall_quantity"] = pick(fields, "recall_quantity")
            row["defect_desc"] = pick(fields, "defect_desc")
            row["hazard"] = pick(fields, "hazard")
            row["remedy"] = pick(fields, "remedy")
            et = pick(fields, "event_time")
            row["event_date"] = et.split("至")[0].strip() if et else pub_date
            if vehicles:
                row["brand_model"] = norm(f"{vehicles[0]['series']} {vehicles[0]['model']}")
                row["manufacture_date_range"] = vehicles[0]["prod_range"].replace("\n", " ")
                row["vin_range"] = vehicles[0]["vin_range"].replace("\n", " ")
            else:
                bm = [fields.get("品牌", ""),
                      fields.get("产品名称", ""), pick(fields, "model")]
                row["brand_model"] = norm(" ".join(p for p in bm if p))
                row["manufacture_date_range"] = cn_range_to_iso(pick(fields, "prod_time"))
            row["vehicle_count"] = len(vehicles)
            row["event_description"] = norm(" ".join(filter(None, [
                row["defect_desc"],
                ("可能造成的后果：" + row["hazard"]) if row["hazard"] else "",
            ]))) or raw_text[:800]
            row["extra_json"] = json.dumps(fields, ensure_ascii=False)
        else:                              # 叙述文本（新闻 / 消费品）
            nv = self._parse_narrative(raw_text, title)
            row.update({k: v for k, v in nv.items() if k in row})
            row["event_date"] = pub_date
            row["event_description"] = raw_text[:800]
            row["extra_json"] = ""
        return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--columns", default=",".join(COLUMNS),
                    help="逗号分隔, 可选: " + ",".join(COLUMNS))
    ap.add_argument("--max-pages", type=int, default=None, help="每个栏目最大页数(验证用)")
    ap.add_argument("--limit-details", type=int, default=None, help="每个栏目最多抓取的详情数")
    ap.add_argument("--delay", type=float, default=1.0, help="请求间隔秒")
    ap.add_argument("--out", default="output/d10_recalls.csv")
    args = ap.parse_args()

    cols = [c.strip() for c in args.columns.split(",") if c.strip() in COLUMNS]
    if not cols:
        print("无有效栏目"); sys.exit(1)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    sp = Spider(delay=args.delay)

    done = set()
    write_header = not os.path.exists(args.out)
    if not write_header:                       # 增量: 读取已采集的 URL
        with open(args.out, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                done.add(r.get("source_url", ""))

    total_new, errors = 0, 0
    with open(args.out, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        for key in cols:
            col = COLUMNS[key]
            print(f"\n=== 栏目 {col['name']} ({key}) ===")
            try:
                items, total_pages = sp.list_items(col["path"], args.max_pages)
            except Exception as e:
                print(f"  列表抓取失败: {e}"); errors += 1; continue
            print(f"  列表共 {total_pages} 页，解析条目 {len(items)}")
            if args.limit_details:
                items = items[:args.limit_details]
            for i, (title, url, date) in enumerate(items, 1):
                if url in done:
                    continue
                try:
                    row = sp.parse_detail(col, title, url, date)
                    w.writerow(row)
                    f.flush()
                    done.add(url)
                    total_new += 1
                    if i <= 3 or i % 50 == 0:
                        print(f"  [{i}/{len(items)}] {row['company_name'] or title[:30]} | {row['recall_no']} | {row['publication_date']}")
                except Exception as e:
                    errors += 1
                    print(f"  [ERR] {url} -> {e}")
            print(f"  栏目完成，累计新增 {total_new} 条")

    print(f"\n完成: 新增 {total_new} 条, 错误 {errors} 个 -> {args.out}")


if __name__ == "__main__":
    main()