#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D11 香港私隐专员公署 (PCPD) 调查报告采集器

入口(列表): https://www.pcpd.org.hk/english/enforcement/commissioners_findings/
           inspection_reports/inspect_report.html   —— Commissioner's Findings 表格
报告(PDF):  .../commissioners_findings/files/<file>.pdf

站点对 python/curl 返回 JS 挑战页(EO_Bot)，requests 无法直取（含 PDF），
故统一用本机 Chrome(selenium) 取列表并下载 PDF，再用 PyMuPDF 解析正文。
robots.txt 亦返回挑战页，未见有效规则。

抽取: 数据泄露/隐私事件、调查对象、调查结论、整改要求，
映射为统一 ESG 争议事件字段输出 CSV。

用法:
  python d11_spider.py --limit 3 --out output/_d11_sample.csv
  python d11_spider.py --out output/d11_pcpd.csv
"""
import argparse
import csv
import io
import os
import re
import time
import warnings
from datetime import datetime
from urllib.parse import urljoin

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")

HOST = "https://www.pcpd.org.hk"
FILEBASE = HOST + "/english/enforcement/commissioners_findings/files/"
LIST = (HOST + "/english/enforcement/commissioners_findings/"
        "inspection_reports/inspect_report.html")
CACHE = r"c:\Users\Administrator\Desktop\pdfs\d11_tmp"

FIELDS = [
    "source_id", "company_name", "event_date", "publication_date",
    "esg_dimension", "event_type", "event_description", "penalty_amount",
    "case_status", "source_url", "collected_at",
    # D11 扩展
    "report_no", "report_category", "organisation", "incident_type",
    "findings", "enforcement", "raw_text",
]

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

INCIDENT_RULES = [
    (r"ransomware", "勒索软件攻击"),
    (r"data breach|data security incident|security incident", "数据泄露事件"),
    (r"wrongful disclosure|improper disclosure|blind ad", "不当披露个人资料"),
    (r"unauthorised (?:access|scraping)|hacker|intrusion", "未授权访问/爬取"),
    (r"cctv|surveillance", "闭路电视/监控"),
    (r"collection|retention|use and storage", "不当收集/保留/使用个人资料"),
    (r"inspection|personal data system", "个人资料系统巡查"),
]
ENFORCE_RE = re.compile(
    r"(enforcement notice|direction (?:to|requiring)|notice requiring|"
    r"ordered to|recommend\w+ to|rectif\w+|cease to|erase|destroy)", re.I)
CONTRAVENE_RE = re.compile(
    r"(contraven\w+|breach\w*|failed to|non-complian\w*)", re.I)
# 调查对象(机构名): 连续首字母大写词 + 机构后缀
ORG_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&.,'’()\-]*(?:\s+(?:of|and|the|for|&|"
    r"[A-Z][A-Za-z0-9&.'’()\-]*)){0,10}\s+"
    r"(?:Company\s+Limited|Limited|Ltd\.?|Corporation|Association|Club|Bank|"
    r"Council|University|School|Department|Hospital|Institute|College|"
    r"Commission|Authority|Trust|Fund))\b")
_ORG_PREFIX = re.compile(
    r"^(?:a\s+)?(?:data breach incident reported by|data breach incident of|"
    r"ransomware attack on(?: the (?:information systems|servers) of)?|"
    r"personal data system of|the operation of|complaints by|the)\s+", re.I)


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def clean_org(s):
    return norm(_ORG_PREFIX.sub("", norm(s))).strip(" .,")


def find_org(text):
    """从正文抽取调查对象机构名(去前缀噪声)；无则返回空。"""
    m = ORG_RE.search(text)
    return clean_org(m.group(1)) if m else ""


def make_driver(dl=None):
    opts = Options()
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--headless=new")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if dl:
        os.makedirs(dl, exist_ok=True)
        opts.add_experimental_option("prefs", {
            "download.default_directory": dl,
            "download.prompt_for_download": False,
            "plugins.always_open_pdf_externally": True,
            "profile.default_content_setting_values.automatic_downloads": 1,
        })
    drv = webdriver.Chrome(options=opts)
    drv.set_page_load_timeout(60)
    return drv


def parse_date(s):
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s)
    if not m:
        return ""
    mon = MONTHS.get(m.group(2).capitalize(), 0)
    return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}" if mon else ""


def parse_listing(html):
    """解析 Findings 表格 -> [{name,url,date,title,report_no,category}]"""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for table in soup.find_all("table"):
        if "Issue Date" not in table.get_text():
            continue
        for tr in table.find_all("tr"):
            a = tr.find("a", href=True)
            if not a or "commissioners_findings/files/" not in a["href"]:
                continue
            name = a["href"].split("/")[-1].split("?")[0]
            if name in seen:
                continue
            seen.add(name)
            title = re.sub(r"This link will open in a new window\s*", "",
                           norm(a.get_text(" ", strip=True)))
            cells = [norm(c.get_text(" ", strip=True)) for c in tr.find_all("td")]
            date = next((parse_date(c) for c in cells if parse_date(c)), "")
            report_no = next((c for c in cells
                              if re.match(r"R\d{2}\s*[-–]?\s*\d+", c.strip())), "")
            category = cells[-1] if cells else ""
            out.append({"name": name, "url": FILEBASE + name, "date": date,
                        "title": title, "report_no": report_no,
                        "category": category})
    return out


def fetch_pdf(drv, url, name, dl, delay=0.5, tries=2):
    """用 Chrome 下载 PDF 到 dl，返回字节；已在缓存则直接读。"""
    cached = os.path.join(CACHE, "pdfs", name)
    if os.path.exists(cached):
        try:
            return open(cached, "rb").read()
        except Exception:
            pass
    for f in os.listdir(dl):
        try:
            os.remove(os.path.join(dl, f))
        except Exception:
            pass
    for i in range(tries):
        try:
            drv.get(url)
            break
        except Exception:
            time.sleep(2 * (i + 1))
    else:
        return b""
    for _ in range(40):
        time.sleep(delay)
        fs = [f for f in os.listdir(dl)
              if f.lower().endswith(".pdf") and not f.endswith(".crdownload")]
        if fs:
            p = os.path.join(dl, fs[0])
            for _ in range(20):
                if open(p, "rb").read()[:4] == b"%PDF":
                    break
                time.sleep(0.3)
            data = open(p, "rb").read()
            os.makedirs(os.path.join(CACHE, "pdfs"), exist_ok=True)
            open(cached, "wb").write(data)
            return data
    return b""


def pdf_text(data):
    try:
        import fitz
        doc = fitz.open(stream=io.BytesIO(data), filetype="pdf")
        return norm(" ".join(doc[i].get_text() for i in range(doc.page_count)))
    except Exception:
        return ""


def extract(text):
    """返回 (incident_type, findings, enforcement, case_status)"""
    inc = next((lab for pat, lab in INCIDENT_RULES if re.search(pat, text, re.I)),
               "个人资料违规")
    sentences = re.split(r"(?<=[.;])\s+", text)
    findings = ""
    for s in sentences:
        if re.search(r"contraven|breach|failed to|not complied|not taken all practicable",
                     s, re.I):
            findings = norm(s)[:300]
            break
    enf = ""
    for s in sentences:
        if ENFORCE_RE.search(s) and re.search(r"notice|direction|order|requir|rectif",
                                              s, re.I):
            enf = norm(s)[:300]
            break
    if re.search(r"contraven|breach|failed to|non-complian|not taken all practicable",
                 text, re.I):
        status = "调查报告已公布（认定违规）"
    else:
        status = "调查报告已公布"
    return inc, findings, enf, status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0=全部")
    ap.add_argument("--delay", type=float, default=0.8)
    ap.add_argument("--out", default=r"c:\Users\Administrator\Desktop\pdfs\output\d11_pcpd.csv")
    a = ap.parse_args()

    dl = os.path.join(CACHE, "dl")
    os.makedirs(dl, exist_ok=True)
    drv = make_driver(dl)
    try:
        drv.get(LIST)
        time.sleep(3.0)
        html = drv.page_source
        items = parse_listing(html)
        if a.limit:
            items = items[:a.limit]
        print(f"报告共 {len(items)} 份，开始下载/解析 PDF ...")

        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for i, it in enumerate(items, 1):
            data = fetch_pdf(drv, it["url"], it["name"], dl, delay=a.delay)
            text = pdf_text(data) if data[:4] == b"%PDF" else ""
            inc, findings, enf, status = extract(text)
            org = find_org(text)
            rows.append({
                "source_id": "D11",
                "company_name": org,
                "event_date": it["date"],
                "publication_date": it["date"],
                "esg_dimension": "S",
                "event_type": f"数据隐私违规（{inc}）",
                "event_description": it["title"],
                "penalty_amount": "",
                "case_status": status,
                "source_url": it["url"],
                "collected_at": now,
                "report_no": it["report_no"],
                "report_category": it["category"],
                "organisation": org,
                "incident_type": inc,
                "findings": findings,
                "enforcement": enf,
                "raw_text": text[:4000],
            })
            print(f"  [{i}/{len(items)}] {it['date']} | {inc:12s} | "
                  f"{(org or it['title'])[:45]} | pdf={len(data)}B")
    finally:
        drv.quit()

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"\n完成: {len(rows)} 条 -> {a.out}")


if __name__ == "__main__":
    main()