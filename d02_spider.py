#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D02 全国排污许可证管理信息平台(公开端) 采集器  v2

站点: https://permit.mee.gov.cn/perxxgkinfo/
要点(实测):
  * 全为服务端渲染 HTML, 无需浏览器。
  * 反爬: 请求过密会返回 520B 的"错误页"(文本含"错误页"/"请您访问 permit.mee.gov.cn")。
    应对: ① 先 GET 主入口建立会话 Cookie; ② delay>=1.8s; ③ 识别错误页 -> 退避重试,
    多次失败则安全中断(留待冷却后续跑)。
  * 翻页: 须携带且链式传递隐藏域 tempReportKey(每次响应返回新值), 同一 Session 顺序翻页;
    深翻页上限 maxJump=3000 -> 大表按城市/省份分片。

采集内容:
  A. 环境合规事件(公告类, 列表即含要素) -> output/d02_permits_events.csv
     许可撤销 / 许可注销 / 限期整改 / 申请前公示
  B. 详情增强(仅撤销+申请前公示, 量小) -> output/d02_permit_details.csv
     经登记簿按许可证编号定位 dataid -> 详情页: 许可版本历史(发展)、大气/废水污染物、执行标准
  (许可登记簿全量 38 万条为可选, 见 --only registry)

用法:
  python -u d02_spider.py --only revoke --max-pages 5 --out output/_s.csv
  python -u d02_spider.py --only events
  python -u d02_spider.py --only details
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

B = "https://permit.mee.gov.cn"
ENTRY = "/permitExt/defaults/default-index!getInformation.action"
TMP = r"c:\Users\Administrator\Desktop\pdfs\d02_tmp"
XKGG = "/perxxgkinfo/syssb/xkgg/"
XXGK = "/perxxgkinfo/syssb/xxgk/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
           "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
           "Accept-Language": "zh-CN,zh;q=0.9",
           "Referer": B + ENTRY}

EVENT_FIELDS = ["source_id", "company_name", "event_date", "publication_date", "esg_dimension",
                "event_type", "event_description", "penalty_amount", "case_status", "source_url",
                "collected_at", "license_no", "province", "city", "industry", "reason",
                "list_type", "detail_url"]
DETAIL_FIELDS = ["source_id", "license_no", "company_name", "industry", "province", "city",
                 "address", "issuing_authority", "issue_versions", "air_pollutants",
                 "air_standards", "water_pollutants", "water_standards", "detail_url", "collected_at"]

LISTS = [
    ("revoke",   XKGG + "xkgg!xkgg_cxggList.action", "许可撤销公告", "排污许可撤销", "撤销", 6, 5, None),
    ("cancel",   XKGG + "xkgg!xkgg_zxggList.action", "许可注销公告", "排污许可注销", "注销", 6, 5, None),
    ("rectify",  XKGG + "xkgg!xxgkXqzgList.action",  "限期整改",     "限期整改",     "整改中", 6, None, 7),
    ("preapply", XXGK + "xxgk!sqqlist.action",       "申请前公示",   "排污许可申请前公示", "公示中", 5, None, 6),
]


def txt(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def is_blocked(html):
    return (not html) or ("错误页" in html) or (len(html) < 1500)


def tk_of(html):
    m = re.search(r'name="tempReportKey"\s+value="([^"]+)"', html)
    return m.group(1) if m else None


class Spider:
    def __init__(self, delay=1.8, retries=3, timeout=45):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.tk = None
        self.blocked = False
        self._last = 0.0
        self._warmed = False

    def _sleep(self):
        w = self.delay - (time.time() - self._last)
        if w > 0:
            time.sleep(w)
        self._last = time.time()

    def warmup(self):
        if self._warmed:
            return
        try:
            self.s.get(B + ENTRY, timeout=self.timeout, verify=False)
        except Exception:
            pass
        self._warmed = True

    def _try_get(self, path):
        self._sleep()
        try:
            r = self.s.get(B + path, timeout=self.timeout, verify=False)
            return r.text
        except Exception:
            return ""

    def _try_post(self, path, data):
        self._sleep()
        try:
            r = self.s.post(B + path, data=data, timeout=self.timeout, verify=False)
            return r.text
        except Exception:
            return ""

    def page(self, path, page_no, extra=None, backoff=90, max_block=3):
        """返回 (html, total_pages). 反爬错误页 -> 退避重试; 仍失败则 blocked=True 且返回 ('',None)"""
        for attempt in range(max_block):
            self.warmup()
            if not self.tk:
                h = self._try_get(path)
                if is_blocked(h):
                    print(f"    !! 疑似反爬(错误页), 第{attempt+1}次退避 {backoff}s ...")
                    time.sleep(backoff)
                    self.tk = None
                    continue
                self.tk = tk_of(h) or self.tk
                if page_no == 1 and not extra:
                    return h, self.total_pages(h)
            data = {"page.pageNo": str(page_no), "page.orderBy": "", "page.order": "",
                    "tempReportKey": self.tk, "treadcode": ""}
            if extra:
                data.update(extra)
            html = self._try_post(path, data)
            if not is_blocked(html):
                got = re.search(r'id="pageNo"[^>]*value="(\d+)"', html)
                if got and int(got.group(1)) == page_no:
                    self.tk = tk_of(html) or self.tk
                    return html, self.total_pages(html)
                self.tk = None  # key 失效
            print(f"    !! 疑似反爬(错误页), 第{attempt+1}次退避 {backoff}s ...")
            time.sleep(backoff)
            self.tk = None
        self.blocked = True
        return "", None

    @staticmethod
    def total_pages(html):
        m = re.search(r"共\s*(\d+)\s*页", html)
        return int(m.group(1)) if m else 1


def parse_list(html, detail_idx):
    soup = BeautifulSoup(html, "lxml")
    out = []
    for tr in soup.find_all("tr"):
        if "tbhead" in " ".join(tr.get("class") or []):
            continue
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        det = ""
        if detail_idx is not None and detail_idx < len(tds):
            a = tds[detail_idx].find("a", href=True)
            det = a["href"] if a else ""
        out.append(([txt(t) for t in tds], det))
    return out


def crawl_flat(sp, key, path, label, etype, status, date_idx, reason_idx, detail_idx, max_pages=None):
    os.makedirs(TMP, exist_ok=True)
    done_f = os.path.join(TMP, f"{key}.done")
    csv_f = os.path.join(TMP, f"{key}.csv")
    if os.path.exists(done_f):
        with open(csv_f, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    html, total = sp.page(path, 1)
    if sp.blocked:
        print(f"  [{key}] 首页即被拦截, 跳过(稍后续跑)")
        return []
    total = total or 1
    lim = min(total, max_pages) if max_pages else total
    print(f"  [{key}] {label}: 共 {total} 页, 本次 {lim} 页")
    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    header = not os.path.exists(csv_f)
    f = open(csv_f, "a", encoding="utf-8-sig", newline="")
    w = csv.DictWriter(f, fieldnames=EVENT_FIELDS)
    if header:
        w.writeheader()
        f.flush()
    seen = set()
    for p in range(1, lim + 1):
        if p > 1:
            html, _ = sp.page(path, p)
        if sp.blocked or not html:
            print(f"    p{p} 中断(反爬), 已存 {len(rows)} 条, 留待续跑")
            break
        for cells, det in parse_list(html, detail_idx):
            date = cells[date_idx] if date_idx < len(cells) else ""
            reason = cells[reason_idx] if (reason_idx is not None and reason_idx < len(cells)) else ""
            row = {
                "source_id": "D02", "company_name": cells[3] if len(cells) > 3 else "",
                "event_date": date, "publication_date": date, "esg_dimension": "E",
                "event_type": etype, "event_description": (reason or label)[:500],
                "penalty_amount": "", "case_status": status, "source_url": B + path,
                "collected_at": now, "license_no": cells[2] if len(cells) > 2 else "",
                "province": cells[0] if len(cells) > 0 else "", "city": cells[1] if len(cells) > 1 else "",
                "industry": cells[4] if len(cells) > 4 else "", "reason": reason, "list_type": label,
                "detail_url": (B + det if det.startswith("/") else det),
            }
            k = (row["license_no"], row["company_name"], row["event_date"], row["reason"])
            if k in seen:
                continue
            seen.add(k)
            w.writerow(row)
            rows.append(row)
        f.flush()
        if p % 20 == 0 or p == lim:
            print(f"    p{p}/{lim} 累计 {len(rows)} 条")
    f.close()
    if not sp.blocked and lim == total:
        open(done_f, "w").write("done")
    return rows


def get_regions(sp, parent):
    for _ in range(3):
        sp._sleep()
        try:
            r = sp.s.post(B + XKGG + "xkgg!getRegions.action", data={"parentCode": parent},
                          timeout=45, verify=False)
            if r.status_code == 200 and "regions" in r.text:
                return r.json().get("regions", [])
        except Exception:
            pass
        time.sleep(2)
    return []


def crawl_registry(sp, max_pages=None):
    os.makedirs(TMP, exist_ok=True)
    done_f = os.path.join(TMP, "registry_done.txt")
    csv_f = os.path.join(TMP, "registry.csv")
    done = set(open(done_f, encoding="utf-8").read().split()) if os.path.exists(done_f) else set()
    path = XKGG + "xkgg!licenseInformation.action"
    provs = get_regions(sp, "000000000000")
    print(f"  登记簿: {len(provs)} 省/直辖市")
    f = open(csv_f, "a", encoding="utf-8-sig", newline="")
    w = csv.DictWriter(f, fieldnames=REG_FIELDS)
    if not os.path.exists(csv_f) or os.path.getsize(csv_f) == 0:
        w.writeheader()
    n = 0
    for pv in provs:
        pc, pn = pv["regioncode"], pv["regionname"]
        for ct in (get_regions(sp, pc) or [{"regioncode": "", "regionname": pn}]):
            cc, cn = ct["regioncode"], ct["regionname"]
            if f"{pc}-{cc}" in done:
                continue
            extra = {"province": pc, "city": cc, "management": "", "registerentername": "",
                     "xkznum": "", "treadname": "", "publishtime": ""}
            html, total = sp.page(path, 1, extra)
            if sp.blocked or not html:
                print("  登记簿被拦截, 中断(city=%s)" % cn)
                f.close()
                return n
            total = total or 1
            lim = min(total, max_pages) if max_pages else total
            now = datetime.now().isoformat(timespec="seconds")
            for p in range(1, lim + 1):
                if p > 1:
                    html, _ = sp.page(path, p, extra)
                    if sp.blocked or not html:
                        break
                for tr in BeautifulSoup(html, "lxml").find_all("tr"):
                    if "tbhead" in " ".join(tr.get("class") or []):
                        continue
                    tds = tr.find_all("td")
                    if len(tds) < 8:
                        continue
                    c = [txt(t) for t in tds]
                    a = tds[-1].find("a", href=True)
                    w.writerow({"source_id": "D02", "dataset": "排污许可登记簿", "province": c[0],
                                "city": c[1], "license_no": c[2], "company_name": c[3], "industry": c[4],
                                "management_type": c[7] if len(c) > 7 else "", "valid_period": c[5],
                                "issue_date": c[6],
                                "detail_url": (B + a["href"] if a and a["href"].startswith("/") else (a["href"] if a else "")),
                                "collected_at": now})
                    n += 1
            f.flush()
            open(done_f, "a", encoding="utf-8").write(f"{pc}-{cc}\n")
            print(f"    {pn}/{cn}: {lim} 页, 累计 {n} 条")
    f.close()
    return n


REG_FIELDS = ["source_id", "dataset", "province", "city", "license_no", "company_name",
              "industry", "management_type", "valid_period", "issue_date", "detail_url", "collected_at"]


def lookup_detail_url(sp, license_no):
    """经登记簿按许可证编号检索, 返回详情链接"""
    html, _ = sp.page(XKGG + "xkgg!licenseInformation.action", 1,
                      extra={"province": "", "city": "", "management": "",
                             "registerentername": "", "xkznum": license_no,
                             "treadname": "", "publishtime": ""})
    if not html:
        return ""
    for tr in BeautifulSoup(html, "lxml").find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        if txt(tds[2]) == license_no:
            a = tds[-1].find("a", href=True)
            return a["href"] if a else ""
    return ""


def parse_detail(html):
    soup = BeautifulSoup(html, "lxml")
    body = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    out = {"address": "", "issuing_authority": "", "issue_versions": "",
           "air_pollutants": "", "air_standards": "", "water_pollutants": "", "water_standards": ""}
    m = re.search(r"生产经营场所地址[:：]\s*([^ ]+?)(?:行业类别|所在地区|$)", body)
    if m:
        out["address"] = m.group(1)
    m = re.search(r"发证机关[:：]\s*(\S+)", body)
    if m:
        out["issuing_authority"] = m.group(1)
    # 版本历史: 逐行 "编号 业务类型 版本 办结日期 期限起 至 期限止"
    vers = []
    for tr in soup.find_all("tr"):
        tds = [txt(t) for t in tr.find_all("td")]
        if len(tds) >= 5 and re.match(r"^\d+$", tds[2] if len(tds) > 2 else ""):
            vers.append(" | ".join(tds))
    out["issue_versions"] = " ;; ".join(vers)
    m = re.search(r"大气主要污染物种类[:：]\s*(.+?)大气污染物排放规律", body)
    if m:
        out["air_pollutants"] = m.group(1)[:400]
    m = re.search(r"大气污染物排放执行标准[:：]\s*(.+?)(?:废水主要污染物|$)", body)
    if m:
        out["air_standards"] = m.group(1)[:400]
    m = re.search(r"废水主要污染物种类[:：]\s*(.+?)废水污染物排放规律", body)
    if m:
        out["water_pollutants"] = m.group(1)[:400]
    m = re.search(r"废水污染物排放执行标准[:：]\s*(.+?)(?:排污权使用|$)", body)
    if m:
        out["water_standards"] = m.group(1)[:400]
    return out


def crawl_details(sp, events_csv, out_csv, max_items=None):
    if not os.path.exists(events_csv):
        print("未见事件表:", events_csv)
        return 0
    rows = list(csv.DictReader(open(events_csv, encoding="utf-8-sig")))
    targets = [r for r in rows if r.get("list_type") in ("许可撤销公告", "申请前公示")]
    if max_items:
        targets = targets[:max_items]
    done = set()
    if os.path.exists(out_csv):
        done = {r["license_no"] + "|" + r["company_name"]
                for r in csv.DictReader(open(out_csv, encoding="utf-8-sig"))}
    header = not os.path.exists(out_csv)
    f = open(out_csv, "a", encoding="utf-8-sig", newline="")
    w = csv.DictWriter(f, fieldnames=DETAIL_FIELDS)
    if header:
        w.writeheader()
    n = 0
    for i, r in enumerate(targets, 1):
        key = r["license_no"] + "|" + r["company_name"]
        if key in done:
            continue
        url = r.get("detail_url") or lookup_detail_url(sp, r["license_no"])
        if not url or not url.startswith(B):
            continue
        html, _ = sp.page(url[len(B):], 1)
        if not html:
            continue
        d = parse_detail(html)
        w.writerow({"source_id": "D02", "license_no": r["license_no"], "company_name": r["company_name"],
                    "industry": r.get("industry", ""), "province": r.get("province", ""),
                    "city": r.get("city", ""), "detail_url": url,
                    "collected_at": datetime.now().isoformat(timespec="seconds"), **d})
        f.flush()
        n += 1
        if i % 20 == 0:
            print(f"    详情 {i}/{len(targets)} 新增 {n}")
        if sp.blocked:
            print("    详情采集被拦截, 中断")
            break
    f.close()
    return n


def write_csv(path, fields, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="all",
                    choices=["all", "events", "registry", "details", "revoke", "cancel", "rectify", "preapply"])
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--max-items", type=int, default=None)
    ap.add_argument("--delay", type=float, default=1.8)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    sp = Spider(delay=a.delay)
    if a.only in ("revoke", "cancel", "rectify", "preapply"):
        L = [x for x in LISTS if x[0] == a.only][0]
        rows = crawl_flat(sp, *L, max_pages=a.max_pages)
        write_csv(a.out or f"output/d02_{a.only}.csv", EVENT_FIELDS, rows)
        print(f"完成 {len(rows)} 条 -> {a.out}")
        return

    if a.only in ("all", "events"):
        allrows = []
        print("== A. 环境合规事件(公告类) ==")
        for L in LISTS:
            allrows += crawl_flat(sp, *L, max_pages=a.max_pages)
            if sp.blocked:
                print("  反爬中断, 稍后可用 --only events 续跑(各列表按 d02_tmp/*.done 续跑)")
                break
        if not sp.blocked:
            write_csv(a.out or "output/d02_permits_events.csv", EVENT_FIELDS, allrows)
            print(f"事件表: {len(allrows)} 条 -> {a.out or 'output/d02_permits_events.csv'}")
        return

    if a.only == "details":
        n = crawl_details(sp, "output/d02_permits_events.csv",
                          a.out or "output/d02_permit_details.csv", a.max_items)
        print(f"详情: {n} 条 -> {a.out or 'output/d02_permit_details.csv'}")
        return

    if a.only == "registry":
        n = crawl_registry(sp, a.max_pages)
        print(f"登记簿: {n} 条")


if __name__ == "__main__":
    main()