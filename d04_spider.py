# -*- coding: utf-8 -*-
"""D04 WRI Aqueduct 4.0 采集器

官方数据源：https://www.wri.org/data/aqueduct-40-country-rankings
许可：Creative Commons (CC)
robots.txt：允许抓取数据页
产出：output/d04_aqueduct.csv —— 国家/省级水风险聚合指标表（长表）

统一指标表字段：区域编码、区域名称、年份、情景、指标、评分、类别、标签、
统计口径、数据版本、来源链接、采集时间。
"""
import io
import os
import zipfile
import datetime as dt

import pandas as pd
import requests

URL = "https://files.wri.org/aqueduct/aqueduct-4-0-country-rankings.zip"
SOURCE_URL = "https://www.wri.org/data/aqueduct-40-country-rankings"
DATA_VERSION = "Aqueduct40 (v4.0, release 2023-07-05)"
OUT = r"c:\Users\Administrator\Desktop\pdfs\output\d04_aqueduct.csv"
TMP = r"c:\Users\Administrator\Desktop\pdfs\d04_tmp"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

# 4 张工作表 -> (区域层级, 是否为未来情景)
SHEETS = {
    "country_baseline": ("country", False),
    "country_future":   ("country", True),
    "province_baseline": ("province", False),
    "province_future":  ("province", True),
}


def _find_cached_xlsx():
    for root, _, files in os.walk(TMP):
        for f in files:
            if f.lower().endswith(".xlsx"):
                return os.path.join(root, f)
    return None


def download_and_extract():
    os.makedirs(TMP, exist_ok=True)
    cached = _find_cached_xlsx()
    if cached:
        print("复用本地已解压文件:", cached)
        return cached

    last_err = None
    for attempt in range(1, 4):
        try:
            r = requests.get(URL, headers=HEADERS, timeout=180)
            r.raise_for_status()
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  下载失败(第{attempt}次): {e}")
    else:
        raise RuntimeError(f"下载失败: {last_err}")

    z = zipfile.ZipFile(io.BytesIO(r.content))
    z.extractall(TMP)
    for name in z.namelist():
        if name.lower().endswith(".xlsx"):
            return os.path.join(TMP, name)
    raise RuntimeError("zip 中未找到 xlsx")


def load_sheet(xl, sheet, level, is_future):
    df = xl.parse(sheet)
    df = df.copy()
    df["region_level"] = level
    # 统一区域编码/名称列
    if level == "country":
        df["region_code"] = df["gid_0"]
        df["region_name"] = df["name_0"]
        df["country_code"] = df["gid_0"]
        df["country_name"] = df["name_0"]
        df["province_code"] = ""
        df["province_name"] = ""
    else:
        df["region_code"] = df["gid_1"]
        df["region_name"] = df["name_1"]
        df["country_code"] = df["gid_0"]
        df["country_name"] = df["name_0"]
        df["province_code"] = df["gid_1"]
        df["province_name"] = df["name_1"]
    if not is_future:
        df["year"] = "baseline"
        df["scenario"] = "baseline"
    df["statistic_basis"] = ("aqueduct_baseline_annual_1979_2019"
                             if not is_future else "aqueduct_future_projection_cmip6")
    df["data_version"] = DATA_VERSION
    df["source_id"] = "D04"
    df["source_url"] = SOURCE_URL
    df["collected_at"] = dt.datetime.now().isoformat(timespec="seconds")
    return df


def main():
    xlsx = download_and_extract()
    xl = pd.ExcelFile(xlsx)
    frames = []
    for sheet, (level, is_future) in SHEETS.items():
        df = load_sheet(xl, sheet, level, is_future)
        frames.append(df)
        print(f"  {sheet}: {len(df)} 行")

    cols = ["source_id", "region_level", "region_code", "region_name",
            "country_code", "country_name", "province_code", "province_name",
            "indicator_name", "weight", "score", "score_ranked", "cat", "label",
            "year", "scenario", "statistic_basis", "data_version",
            "un_region", "wb_region", "source_url", "collected_at"]
    all_df = pd.concat(frames, ignore_index=True)
    all_df = all_df[cols]

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    all_df.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"完成: {len(all_df)} 行 -> {OUT}")


if __name__ == "__main__":
    main()