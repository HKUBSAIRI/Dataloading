# -*- coding: utf-8 -*-
"""D14 香港竞争事务委员会（竞争事务审裁处案件）采集器 + 判案书富化

入口：   https://www.compcomm.hk/en/enforcement/enforcement/competition_tribunal.html
判案书页：{BASE}/judgments_cteaX_YYYY.html  —— 仅含裁决日期 + legalref 判案书链接
判案书正文：https://legalref.judiciary.hk/lrs/common/ju/ju_body.jsp?DIS=<id>...
        正文含案号、裁决结论与罚款金额，故据此补齐 case_status 与 penalty_amount。

该站 compcomm.hk / legalref.judiciary.hk 对本机 python/curl 的 TLS 指纹握手会被重置，
故统一使用本机 Chrome(selenium) 取页。robots.txt：站点未提供。

产出：output/d14_compcomm.csv
"""
import os
import re
import csv
import time
import datetime as dt

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import UnexpectedAlertPresentException
from bs4 import BeautifulSoup

URL = "https://www.compcomm.hk/en/enforcement/enforcement/competition_tribunal.html"
BASE = "https://www.compcomm.hk/en/enforcement/enforcement/"
LEDREF = ("https://legalref.judiciary.hk/lrs/common/ju/ju_body.jsp"
          "?DIS={dis}&AH=&QS=&FN=&currpage=T&loc=")
OUT = r"c:\Users\Administrator\Desktop\pdfs\output\d14_compcomm.csv"
CACHE = r"c:\Users\Administrator\Desktop\pdfs\d14_tmp"

FIELDS = ["source_id", "company_name", "event_date", "publication_date",
          "esg_dimension", "event_type", "event_description", "penalty_amount",
          "case_status", "source_url", "collected_at",
          "case_no", "respondents", "companies", "notice_url", "judgment_url",
          "judgment_page_url", "judgment_dates", "judgment_dis", "raw_text"]

COMPANY_RE = re.compile(
    r"\b(?:Limited|Ltd\.?|Company|Corporation|Holdings|Group|plc|GmbH|LLP|Inc\.?)\b"
    r"|\bCo\.?(?=\s|$|\)|,)", re.I)

# 和解(by consent/合作和解)判定：仅用明确的“同意处置”措辞，避免把程序性 by consent 误判
CONSENT_RE = re.compile(
    r"kam kwong procedure|consent summons|made by consent|orders?\s+by consent|"
    r"application by consent|dispose of the proceedings by consent|declarations?\s+(?:are\s+|sought\s+)?by consent",
    re.I)
# 罚款金额抽取
MONEY = re.compile(r"(?:HK\s*)?\$\s*[\d,]{3,}(?:\.\d+)?", re.I)
CUE_ORDER = re.compile(
    r"pay to the government a pecuniary penalty|"
    r"pecuniary penalty (?:of|in the (?:sum|amount) of)\s*(?:HK)?\$|"
    r"penalt\w+ (?:of|in the (?:sum|amount) of)\s*(?:HK)?\$|"
    r"penalt\w+ of (?:HK)?\$[\d,]+ (?:be )?imposed|"
    r"penalt\w+ imposed", re.I)
CUE_IS = re.compile(r"penalt\w+[^.]{0,60}\bis (?:therefore\s+)?(?:HK)?\$", re.I)
# 强判项措辞：明确“向政府缴付罚款”或“罚款$X被施加”，命中即认定为真实判项
CUE_STRONG = re.compile(
    r"pay to the government a pecuniary penalty|"
    r"penalt\w+ of (?:HK)?\$[\d,]+ (?:be )?imposed", re.I)
NEG = re.compile(r"propos|recommend|maximum|statutory cap|uplift|turnover|range|figure|"
                 r"sanctioned|base amount|multiplier|"
                 r"sough?t to (?:set aside|increase|raise|impose)|increase[sd]? (?:the )?penalt", re.I)
# 金额就近排除：分期款 / 讼费 / 区间 / 计算表中间值，均非最终罚款判项
LOCAL_EXCL = re.compile(
    r"figure|between|range|installment|instalment|costs|totaling|totalling|aggregate"
    r"|total(?:ly)? of", re.I)

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def split_respondents(text):
    parts = re.split(r",|\band\b", text)
    return [p.strip() for p in parts if p.strip(" .,")]


def parse_date(s):
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", s)
    if not m:
        return ""
    return f"{m.group(3)}-{MONTHS.get(m.group(2).title(), 0):02d}-{int(m.group(1)):02d}"


def make_driver():
    opts = Options()
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--headless=new")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    drv = webdriver.Chrome(options=opts)
    drv.set_page_load_timeout(60)
    return drv


def fetch(drv, url, tries=3):
    for i in range(tries):
        try:
            drv.get(url)
            time.sleep(1.2)
            try:
                drv.switch_to.alert.accept()
            except Exception:
                pass
            return drv.page_source
        except UnexpectedAlertPresentException:
            try:
                drv.switch_to.alert.accept()
            except Exception:
                pass
        except Exception as e:
            print(f"    retry {i + 1}: {str(e)[:60]}")
            time.sleep(3 * (i + 1))
    return ""


def html_to_text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def parse_cases(html):
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for t in soup.find_all("table"):
        for tr in t.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            date_txt = tds[0].get_text(" ", strip=True)
            case_no = tds[1].get_text(" ", strip=True)
            if not case_no or "Case No" in case_no:
                continue
            links = tds[2].find_all("a", href=True)
            notice_url, judgment_url = "", ""
            for a in links:
                href = a["href"]
                if "judgments_" in href:
                    judgment_url = href
                elif "Rule%2019" in href or "rule 19" in a.get_text().lower():
                    notice_url = href
            txt = tds[2].get_text(" ", strip=True)
            for a in links:
                txt = txt.replace(a.get_text(" ", strip=True), " ")
            txt = re.sub(r"\s+", " ", txt).strip()
            respondents = re.sub(r"^Competition Commission v\.?\s*", "", txt).strip()
            companies = [s for s in split_respondents(respondents) if COMPANY_RE.search(s)]
            rows.append({"case_no": case_no, "event_date": parse_date(date_txt),
                         "respondents": respondents, "companies": companies,
                         "notice_url": notice_url, "judgment_url": judgment_url, "desc": txt})
    return rows


def norm_judge_url(u):
    return (BASE + os.path.basename(u.strip().replace("\\", "/"))) if u else ""


def get_judgment_items(drv, judge_url, case_key):
    """返回 [(iso_date, dis, text), ...]；带磁盘缓存。"""
    if not judge_url:
        return []
    pfile = os.path.join(CACHE, "pages", case_key + ".html")
    if os.path.exists(pfile):
        html = open(pfile, encoding="utf-8").read()
    else:
        html = fetch(drv, judge_url)
        os.makedirs(os.path.dirname(pfile), exist_ok=True)
        open(pfile, "w", encoding="utf-8").write(html)
    pairs = re.findall(r'href="[^"]*DIS=(\d+)[^"]*"[^>]*>\s*(\d{2})/(\d{2})/(\d{4})', html)
    items = []
    for dis, dd, mm, yy in pairs:
        tfile = os.path.join(CACHE, "texts", dis + ".txt")
        if os.path.exists(tfile):
            txt = open(tfile, encoding="utf-8").read()
        else:
            txt = html_to_text(fetch(drv, LEDREF.format(dis=dis)))
            os.makedirs(os.path.dirname(tfile), exist_ok=True)
            open(tfile, "w", encoding="utf-8").write(txt)
        items.append((f"{yy}-{mm}-{dd}", dis, txt))
    return items


def classify_status(items):
    if not items:
        return "审理中（已入禀竞争事务审裁处）"
    blob = " ".join(t for _, _, t in items)
    if CONSENT_RE.search(blob):
        return "和解（由双方同意处置 / 合作和解，by consent）"
    return "已裁决"


def canon_amount(a):
    num = re.sub(r"[^\d.]", "", a)
    if not num:
        return ""
    if "." in num:
        s = f"{float(num):,.2f}".rstrip("0").rstrip(".")
    else:
        s = f"{int(num):,}"
    return "HK$" + s


def extract_penalty(items):
    """从审裁处判案书（非上诉庭）的处罚判项中抽取罚款金额。

    判项认定：命中 CUE_STRONG（明确判项措辞）即采纳；否则须命中 CUE_ORDER/CUE_IS
    且不含 NEG。金额层面再就近排除分期款、讼费、区间与计算表中间值。
    """
    amounts = []
    for _, _, txt in items:
        if re.search(r"\bCACV\b|\bHKCA\b", txt[:600]):   # 跳过上诉庭判决
            continue
        for m in re.finditer(r"[^.]{0,220}penalt[^.]{0,220}", txt, re.I):
            s = m.group(0)
            strong = bool(CUE_STRONG.search(s))
            if not strong and not ((CUE_ORDER.search(s) or CUE_IS.search(s))
                                   and not NEG.search(s)):
                continue
            for mm in MONEY.finditer(s):
                ctx = s[max(0, mm.start() - 40):mm.end() + 40]
                if LOCAL_EXCL.search(ctx):
                    continue
                a = canon_amount(mm.group(0))
                if a and a not in amounts:
                    amounts.append(a)
    return amounts


def main():
    os.makedirs(CACHE, exist_ok=True)
    drv = make_driver()
    try:
        time.sleep(2)
        html = fetch(drv, URL)
        cases = parse_cases(html)
        print(f"解析案件 {len(cases)} 个")
        if not cases:
            print("未解析到案件，终止（不覆盖已有 CSV）")
            return

        enriched = []
        for r in cases:
            jurl = norm_judge_url(r["judgment_url"])
            items = get_judgment_items(drv, jurl, r["case_no"].replace("/", "_"))
            status = classify_status(items)
            penalty = extract_penalty(items)
            r.update(judgment_page_url=jurl,
                     judgment_dates="; ".join(d for d, _, _ in items),
                     judgment_dis="; ".join(dis for _, dis, _ in items),
                     case_status=status,
                     penalty_amount="; ".join(penalty))
            enriched.append(r)
            print(f"  {r['case_no']:11s} | {status[:14]} | {len(items)} 判案书 | "
                  f"罚款 {r['penalty_amount'][:60] or '—'}")
    finally:
        drv.quit()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    now = dt.datetime.now().isoformat(timespec="seconds")
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in enriched:
            companies = r["companies"]
            w.writerow({
                "source_id": "D14",
                "company_name": "; ".join(companies) or r["respondents"],
                "event_date": r["event_date"],
                "publication_date": "",
                "esg_dimension": "G",
                "event_type": "反竞争行为（合谋/围标/价格操控）",
                "event_description": r["desc"],
                "penalty_amount": r["penalty_amount"],
                "case_status": r["case_status"],
                "source_url": URL,
                "collected_at": now,
                "case_no": r["case_no"],
                "respondents": r["respondents"],
                "companies": "; ".join(companies),
                "notice_url": r["notice_url"],
                "judgment_url": r["judgment_url"],
                "judgment_page_url": r["judgment_page_url"],
                "judgment_dates": r["judgment_dates"],
                "judgment_dis": r["judgment_dis"],
                "raw_text": r["desc"],
            })
    print(f"完成: {len(enriched)} 条 -> {OUT}")


if __name__ == "__main__":
    main()