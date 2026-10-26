#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D01 港交所披露易 (HKEXnews) 公告索引采集器

站点/接口: https://www1.hkexnews.hk/search/titleSearchServlet.do   (GET, JSON)
实测要点:
  * category/stockId/documentType/searchType/t1code/t2Gcode/t2code **必须传空串**(传 -1 会返回 0);
    lang=E; 日期 YYYYMMDD。
  * "全部文档"检索的日期跨度**上限 31 天**(超限 recordCnt=0), 故按月分窗。
  * rowRange 设为足够大即一次性返回该窗全部记录(实际返回 min(rowRange, recordCnt))。
  * 覆盖回溯至 1999-04(实测该月 182 条)。
  * robots.txt 返回 404(无爬虫限制文件)。
字段(12): NEWS_ID, DATE_TIME(dd/mm/yyyy HH:MM), STOCK_CODE, STOCK_NAME, TITLE,
          LONG_TEXT/SHORT_TEXT(文档类别), FILE_TYPE, FILE_INFO(大小),
          FILE_LINK(公告PDF路径), DOD_WEB_PATH。
  * 仅采集**公告索引(元数据)**, 不下载 PDF(港交所对文件转载/再发布有使用限制)。

输出(统一事件表风格 + 扩展列):
  source_id, company_name, event_date, publication_date, esg_dimension, event_type,
  event_description, penalty_amount, case_status, source_url, collected_at,
  news_id, stock_code, doc_category, file_type, file_size, pdf_url, dod_web_path

用法:
  python -u d01_spider.py --only 202509 --out output/_d01_202509.csv
  python -u d01_spider.py                       # 全历史(1999-04 至今), 月级缓存续跑 d01_tmp/
"""
import argparse
import calendar
import csv
import html
import json
import os
import re
import time
import warnings
from datetime import datetime

import requests

warnings.filterwarnings("ignore")
requests.packages.urllib3.disable_warnings()

SERVLET = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
PAGE = "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en"
TMP = r"c:\Users\Administrator\Desktop\pdfs\d01_tmp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": PAGE,
}
FIELDS = ["source_id", "company_name", "event_date", "publication_date", "esg_dimension",
          "event_type", "event_description", "penalty_amount", "case_status", "source_url",
          "collected_at", "news_id", "stock_code", "doc_category", "file_type", "file_size",
          "pdf_url", "dod_web_path"]

E_KW = ("environment", "climate", "carbon", "emission", "sustainab", "esg", "green",
        "pollution", "waste", "energy", "water")
S_KW = ("employee", "labour", "labor", "social", "community", "health", "safety",
        "human rights", "staff", "workforce")


def clean(s):
    s = html.unescape(s or "")
    s = re.sub(r"<br\s*/?>", " / ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def dimension(category, title):
    t = (category + " " + title).lower()
    if any(k in t for k in E_KW):
        return "E"
    if any(k in t for k in S_KW):
        return "S"
    return "G"


def parse_dt(dt):
    """'02/09/2025 22:55' -> '2025-09-02'"""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", (dt or "").strip())
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


class Spider:
    def __init__(self, delay=1.2, retries=3, timeout=300):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self._last = 0.0

    def _sleep(self):
        w = self.delay - (time.time() - self._last)
        if w > 0:
            time.sleep(w)
        self._last = time.time()

    def warmup(self):
        self._sleep()
        try:
            self.s.get(PAGE, timeout=60, verify=False)
        except Exception:
            pass

    def query(self, f, t, row_range=1000000):
        params = {"sortDir": "0", "sortByOptions": "DateTime", "category": "",
                  "market": "SEHK", "stockId": "", "documentType": "",
                  "fromDate": f, "toDate": t, "title": "", "searchType": "",
                  "t1code": "", "t2Gcode": "", "t2code": "",
                  "rowRange": str(row_range), "lang": "E"}
        for i in range(self.retries):
            self._sleep()
            try:
                r = self.s.get(SERVLET, params=params, timeout=self.timeout, verify=False)
                d = r.json()
                res = d.get("result", "[]")
                items = json.loads(res) if res.strip().startswith("[") else []
                return int(d.get("recordCnt") or 0), items
            except Exception as e:
                print(f"    {f}-{t} 请求异常(第{i+1}次): {type(e).__name__}: {e}")
            time.sleep(3 * (i + 1))
        return None, []


def fetch_window(sp, f, t):
    """取一个日期窗(<=31天)的全部记录; 返回 list[dict] 或 None(失败)。"""
    rc, items = sp.query(f, t)
    if rc is None:
        return None
    if rc > len(items):                    # 单次未取全 -> 按天拆分
        out = []
        for day in range(int(f[6:8]), int(t[6:8]) + 1):
            d = f[:6] + f"{day:02d}"
            rc2, it2 = sp.query(d, d)
            if rc2 is None:
                return None
            if rc2 > len(it2):
                print(f"      !! 单日 {d} 仍超限 {len(it2)}/{rc2}")
            out += it2
        return out
    return items


def month_windows(y0, m0, y1, m1, cap_today=None):
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        last = calendar.monthrange(y, m)[1]
        f = f"{y:04d}{m:02d}01"
        t = f"{y:04d}{m:02d}{last:02d}"
        if cap_today and (y, m) == (y1, m1) and cap_today < t:
            t = cap_today
        yield f, t
        m += 1
        if m > 12:
            m, y = 1, y + 1


def to_row(it, now):
    code = clean(it.get("STOCK_CODE", ""))
    name = clean(it.get("STOCK_NAME", ""))
    title = clean(it.get("TITLE", ""))
    cat = clean(it.get("LONG_TEXT", "") or it.get("SHORT_TEXT", ""))
    link = (it.get("FILE_LINK", "") or "").strip()
    dod = (it.get("DOD_WEB_PATH", "") or "").strip()
    pdf = ("https://www1.hkexnews.hk" + link) if link.startswith("/") else link
    dt = parse_dt(it.get("DATE_TIME", ""))
    return {
        "source_id": "D01", "company_name": name, "event_date": dt,
        "publication_date": dt, "esg_dimension": dimension(cat, title),
        "event_type": cat[:300], "event_description": title, "penalty_amount": "",
        "case_status": "", "source_url": pdf or dod, "collected_at": now,
        "news_id": it.get("NEWS_ID", ""), "stock_code": code, "doc_category": cat[:300],
        "file_type": it.get("FILE_TYPE", ""), "file_size": it.get("FILE_INFO", ""),
        "pdf_url": pdf, "dod_web_path": dod,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="单月, 如 202509")
    ap.add_argument("--from", dest="frm", default=None, help="起始月 YYYYMM")
    ap.add_argument("--to", dest="to", default=None, help="结束月 YYYYMM")
    ap.add_argument("--out", default=r"c:\Users\Administrator\Desktop\pdfs\output\d01_hkexnews.csv")
    a = ap.parse_args()

    now_m = datetime.now()
    if a.only:
        y, m = int(a.only[:4]), int(a.only[4:6])
        windows = list(month_windows(y, m, y, m))
    else:
        sy, sm = (int(a.frm[:4]), int(a.frm[4:6])) if a.frm else (1999, 4)
        ey, em = (int(a.to[:4]), int(a.to[4:6])) if a.to else (now_m.year, now_m.month)
        cap = now_m.strftime("%Y%m%d")
        windows = list(month_windows(sy, sm, ey, em, cap_today=cap))

    sp = Spider()
    sp.warmup()
    os.makedirs(TMP, exist_ok=True)
    print(f"窗口数: {len(windows)}  ({windows[0][0]} ~ {windows[-1][1]})")

    total = 0
    for i, (f, t) in enumerate(windows, 1):
        cf = os.path.join(TMP, f"{f}_{t}.json")
        if os.path.exists(cf):
            items = json.load(open(cf, encoding="utf-8"))
        else:
            items = fetch_window(sp, f, t)
            if items is None:
                print(f"  [{f}-{t}] 中断, 留待续跑 (已完成 {i-1}/{len(windows)})")
                break
            json.dump(items, open(cf, "w", encoding="utf-8"))
        total += len(items)
        print(f"  [{i}/{len(windows)}] {f}-{t}: {len(items)} 条 (累计 {total})")

    # 汇总输出
    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    for f, t in windows:
        cf = os.path.join(TMP, f"{f}_{t}.json")
        if not os.path.exists(cf):
            continue
        for it in json.load(open(cf, encoding="utf-8")):
            rows.append(to_row(it, now))
    if a.only:
        out = a.out if a.out != r"c:\Users\Administrator\Desktop\pdfs\output\d01_hkexnews.csv" \
            else os.path.join(os.path.dirname(a.out), f"_d01_{a.only}.csv")
    else:
        out = a.out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"\n完成: {len(rows)} 行 -> {out}  (缓存窗口 {len(os.listdir(TMP))} 个)")


if __name__ == "__main__":
    main()