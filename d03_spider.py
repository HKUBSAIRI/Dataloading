#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D03 IPE 环境监管数据库(公开端) 采集器

站点: https://www.ipe.org.cn  (页面 /IndustryRecord/Regulatory.aspx)
接口: POST /data_ashx/GetAirData.ashx?xx=<cmd>   (企业级汇总, 非单条事件)
  需: 先 GET 页面建会话; 带 X-Requested-With / Referer;
      参数 cmd / keycode(页面隐藏域 id="keycode") / pageSize / pageIndex。
      可选过滤: countryId / provinceId / cityId / professionId (实测可缩小 recordCount)。
  返回: {isSuccess:'1', recordCount:'N', pager, content:'<URL编码<table行HTML>'}
  行单元格为 %uXXXX(UTF-16) 编码, 需自定义解码。

关键限制(实测):
  * 匿名分页 pageIndex 仅允许 1~2 (>=3 返回 isSuccess:'5' 安全考虑，请使用精准检索条件查询)。
  * 单次响应上限约 20 万行 (>=25 万 行触发 504), 故大表用 provinceId 分片。

采集栏目(公开可用, 单企业明细需登录):
  getxianqizhenggailist 限期整改   编号|企业名称|所在地|记录数|最近日期       (36703)
  getjiancha            检查       编号|企业名称|所在地|检查类型|年份         (37397)
  getshigu              事故       编号|企业名称|所在地|记录数|年份           (42901)
  getshenpixuke         审批许可   编号|企业名称|所在地|记录数|年份           (3706)
  getsafetylist         安全       编号|企业名称|所在地|年份                 (555772, 按省份分片)
(另有 getRecords 环境监管记录≈2769万 / getqitazhifa 其他执法≈570万, 千万级, 未纳入)

输出(统一事件表风格 + 扩展列):
  source_id,company_name,event_date,publication_date,esg_dimension,event_type,
  event_description,penalty_amount,case_status,source_url,collected_at,
  category,location,sub_type,record_count

用法:
  python -u d03_spider.py                       # 全量(单元级缓存续跑 d03_tmp/)
  python -u d03_spider.py --only safety
"""
import argparse
import csv
import json
import os
import re
import time
import urllib.parse
import warnings
from datetime import datetime

import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")
requests.packages.urllib3.disable_warnings()

B = "https://www.ipe.org.cn"
PAGE = "/IndustryRecord/Regulatory.aspx"
TMP = r"c:\Users\Administrator\Desktop\pdfs\d03_tmp"
MAXPAGE = 200000            # 单次响应安全上限
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

FIELDS = ["source_id", "company_name", "event_date", "publication_date", "esg_dimension",
          "event_type", "event_description", "penalty_amount", "case_status", "source_url",
          "collected_at", "category", "location", "sub_type", "record_count"]

LISTS = [
    {"key": "xianqizhenggai", "name": "限期整改", "cmd": "getxianqizhenggailist", "layout": "count_date"},
    {"key": "jiancha",        "name": "检查",     "cmd": "getjiancha",            "layout": "subtype_year"},
    {"key": "shigu",          "name": "事故",     "cmd": "getshigu",              "layout": "count_year"},
    {"key": "shenpixuke",     "name": "审批许可", "cmd": "getshenpixuke",         "layout": "count_year"},
    {"key": "safety",         "name": "安全",     "cmd": "getsafetylist",         "layout": "year", "slice": True},
]

# 实测有效 provinceId(1..100 枚举), 用于 safety 分片
PROVINCES = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23,
             24, 25, 27, 28, 29, 30, 31, 32, 33, 35, 37, 38, 39, 40, 41, 42, 43, 44, 45, 47,
             48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 98, 99, 100]

_U = re.compile(r"%u([0-9A-Fa-f]{4})")


def dec(s):
    return _U.sub(lambda m: chr(int(m.group(1), 16)), urllib.parse.unquote(s or ""))


def clean(s):
    return re.sub(r"\s+", " ", dec(s)).strip()


class Spider:
    def __init__(self, delay=0.4, retries=3, timeout=300):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.keycode = None
        self._last = 0.0
        self._warmed = False
        self.blocked = False

    def _sleep(self):
        w = self.delay - (time.time() - self._last)
        if w > 0:
            time.sleep(w)
        self._last = time.time()

    def warmup(self):
        self._sleep()
        try:
            r = self.s.get(B + PAGE, timeout=45, verify=False)
            m = re.search(r'id="keycode"[^>]*value="([^"]+)"', r.text, re.I)
            if m:
                self.keycode = m.group(1)
            self._warmed = True
            return True
        except Exception as e:
            print(f"    warmup 失败: {type(e).__name__}: {e}")
            return False

    def call(self, cmd, page, size, extra=None):
        """返回 (record_count:int, rows:list[list[str]]) 或 (None, []) 失败。"""
        if not self._warmed and not self.warmup():
            return None, []
        data = {"cmd": cmd, "keycode": self.keycode or "",
                "pageSize": str(size), "pageIndex": str(page)}
        if extra:
            data.update(extra)
        for i in range(self.retries):
            self._sleep()
            try:
                r = self.s.post(B + "/data_ashx/GetAirData.ashx?xx=" + cmd, data=data,
                                timeout=self.timeout, verify=False,
                                headers={"X-Requested-With": "XMLHttpRequest", "Referer": B + PAGE})
                t = r.text
                if "isSuccess:'1'" in t or 'isSuccess:"1"' in t:
                    rc = re.search(r"recordCount:'(\d+)'", t)
                    ct = re.search(r"content:'(.*?)'(?:,|})", t, re.S)
                    return int(rc.group(1)) if rc else 0, self._parse(ct.group(1) if ct else "")
                if "登录" in t:
                    print("    !! 提示需登录, 中止")
                    self.blocked = True
                    return None, []
                print(f"    p{page} 异常(第{i+1}次): {re.sub(chr(10),' ',t[:90])}")
            except Exception as e:
                print(f"    p{page} {type(e).__name__}: {e}")
            time.sleep(3 * (i + 1))
            self._warmed = False
        return None, []

    @staticmethod
    def _parse(encoded_html):
        soup = BeautifulSoup(dec(encoded_html), "lxml")
        out = []
        for tr in soup.find_all("tr"):
            tds = [clean(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
            if len(tds) >= 4:
                out.append(tds)
        return out


def to_row(cfg, cells, now):
    name, loc, rest = cells[1], cells[2], cells[3:]
    year = date = sub_type = rc = ""
    lay = cfg["layout"]
    if lay == "count_date" and len(rest) >= 2:
        rc, date = rest[0], rest[1]
    elif lay == "subtype_year" and len(rest) >= 2:
        sub_type, year = rest[0], rest[1]
    elif lay == "count_year" and len(rest) >= 2:
        rc, year = rest[0], rest[1]
    elif lay == "year" and rest:
        year = rest[0]
    event_date = date or year
    desc = f"IPE{cfg['name']}：{loc}"
    if sub_type:
        desc += f"；检查类型={sub_type}"
    if rc:
        desc += f"；记录数={rc}"
    if event_date:
        desc += f"；年份={event_date}"
    return {"source_id": "D03", "company_name": name, "event_date": event_date,
            "publication_date": "", "esg_dimension": "E", "event_type": cfg["name"],
            "event_description": desc, "penalty_amount": "", "case_status": "",
            "source_url": B + PAGE, "collected_at": now, "category": cfg["name"],
            "location": loc, "sub_type": sub_type, "record_count": rc}


def fetch_unit(sp, cfg, unit, extra=None):
    """取一个采集单元(整表或某省份切片), 单元级缓存续跑。返回 rows(list[list[str]])。"""
    cf = os.path.join(TMP, f"{unit}.json")
    if os.path.exists(cf):
        try:
            return json.load(open(cf, encoding="utf-8"))
        except Exception:
            pass
    rows = []
    rc, r = sp.call(cfg["cmd"], 1, MAXPAGE, extra)
    if rc is None:
        return None
    rows += r
    if rc > len(rows):                     # 需要第 2 页(受限于 pageIndex<=2)
        _, r2 = sp.call(cfg["cmd"], 2, MAXPAGE, extra)
        if r2 is None:
            return None
        rows += r2
    if rc > len(rows):
        print(f"    !! 单元 {unit} 仅取到 {len(rows)}/{rc} (分页受限)")
    os.makedirs(TMP, exist_ok=True)
    json.dump(rows, open(cf, "w", encoding="utf-8"))
    return rows


def crawl(sp, cfg):
    os.makedirs(TMP, exist_ok=True)
    all_rows = []
    if cfg.get("slice"):
        for pid in PROVINCES:
            if sp.blocked:
                break
            unit = f"{cfg['cmd']}__p{pid}"
            r = fetch_unit(sp, cfg, unit, {"provinceId": str(pid)})
            if r is None:
                print(f"  [{cfg['key']}] 省份 {pid} 中断, 留待续跑")
                break
            all_rows += r
            print(f"  [{cfg['key']}] 省份 {pid}: {len(r)} 条 (累计切片 {len(all_rows)})")
    else:
        r = fetch_unit(sp, cfg, cfg["cmd"])
        if r is None:
            print(f"  [{cfg['key']}] 中断(接口异常), 留待续跑")
        else:
            all_rows = r
            print(f"  [{cfg['key']}] {cfg['name']}: {len(r)} 条")
    return all_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    ap.add_argument("--out", default=r"c:\Users\Administrator\Desktop\pdfs\output\d03_ipe.csv")
    a = ap.parse_args()

    sp = Spider()
    if not sp.warmup() or not sp.keycode:
        print("无法建立会话, 退出")
        return

    now = datetime.now().isoformat(timespec="seconds")
    out_rows = []
    for cfg in LISTS:
        if a.only and cfg["key"] != a.only:
            continue
        cells = crawl(sp, cfg)
        seen = set()
        n_add = 0
        for c in cells:
            k = tuple(c)
            if k in seen:
                continue
            seen.add(k)
            try:
                out_rows.append(to_row(cfg, c, now))
                n_add += 1
            except Exception as e:
                print(f"    行解析失败: {e}")
        print(f"  [{cfg['key']}] 去重后新增 {n_add} 条 -> 累计 {len(out_rows)}")

    if not a.only:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with open(a.out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(out_rows)
        print(f"\n完成: 合计 {len(out_rows)} 行 -> {a.out}")


if __name__ == "__main__":
    main()