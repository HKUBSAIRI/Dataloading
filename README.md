# ESG 公开数据源采集器（D01–D16）

本目录下的 `dXX_spider.py` 是面向公开网页 / 官方接口的 ESG 替代数据采集脚本。
任务编号为 D01–D16，其中 **D05 / D08 / D09 不存在**（原任务编号跳号），实际为 **13 个数据源、13 个脚本**。

所有脚本默认把结果写入 `output/`，并遵循两类统一表结构：

| 表类型 | 用途 | 代表来源 |
|---|---|---|
| **事件表** | 企业层面的争议 / 监管事件（违规、召回、处罚、案件、公告），用于企业 ESG 风险画像 | D01、D02、D03、D07、D10、D11、D12、D13、D14 |
| **指标长表** | 地区 / 设施 / 车辆层面的环境与统计指标，用于背景量与空间、宏观匹配 | D04、D06、D15、D16 |

事件表与指标长表**不混表**。

---

## 一、环境依赖

- Python 3.9+
- 第三方库：

```bash
pip install requests beautifulsoup4 lxml pandas selenium openpyxl pymupdf
```

- **D11 / D12 / D14** 依赖本机安装的 **Google Chrome**（Selenium 驱动，脚本使用 `webdriver.Chrome`，无需另装 chromedriver 时请确保版本匹配）。
- D04 依赖 `pandas` + `openpyxl` 读取官方 xlsx；D11 依赖 `PyMuPDF(fitz)` 解析 PDF。

---

## 二、脚本清单

| 脚本 | 数据源 | 采集方式 | 主要参数 | 输出 |
|---|---|---|---|---|
| `d01_spider.py` | 港交所披露易 HKEXnews | 官方 JSON 检索接口，按月分窗 | `--only YYYYMM` `--from` `--to` `--out` | `output/d01_hkexnews.csv` |
| `d02_spider.py` | 全国排污许可证管理信息平台 | 服务端渲染 HTML 列表 + 隐藏域翻页 | `--only` `--max-pages` `--max-items` `--delay` `--out` | `output/d02_permits_events.csv` `output/d02_permit_details.csv` |
| `d03_spider.py` | IPE 环境监管数据库 | 站点 JSON/HTML 混合接口，按省份分片 | `--only` `--out` | `output/d03_ipe.csv` |
| `d04_spider.py` | WRI Aqueduct 4.0 | 官方 zip/xlsx 数据集下载 | 无 | `output/d04_aqueduct.csv` |
| `d06_spider.py` | Global Forest Watch | 官方匿名 `download/csv` API（无需 Key） | `--only` `--out` | `output/d06_gfw.csv` |
| `d07_spider.py` | 香港劳工处 | 新闻稿列表 + 详情 HTML 抽取 | `--years` `--max-pages` `--limit-details` `--delay` `--out` | `output/d07_labour.csv` |
| `d10_spider.py` | 缺陷产品召回技术中心 | 栏目列表 + 详情页 HTML | `--columns` `--max-pages` `--limit-details` `--delay` `--out` | `output/d10_recalls.csv` |
| `d11_spider.py` | 香港私隐专员公署 PCPD | Chrome 绕过 JS 挑战，下载并解析 PDF | `--limit` `--delay` `--out` | `output/d11_pcpd.csv` |
| `d12_spider.py` | 商业与人权资源中心 BHRRC | Chrome 取 Cookie → 官方导出端点下载完整 CSV | 无（路径写在脚本常量） | `output/d12_bhrrc.csv` |
| `d13_spider.py` | 香港廉政公署 ICAC | 新闻稿列表 + 详情 HTML | `--max-pages` `--limit-details` `--delay` `--out` | `output/d13_icac.csv` |
| `d14_spider.py` | 香港竞争事务委员会 | Chrome 取案件页 + legalref 判案书正文富化 | 无（路径写在脚本常量） | `output/d14_compcomm.csv` |
| `d15_spider.py` | Green NCAP | 两个公开 HTML 结果页 | `--out` | `output/d15_greenncap.csv` |
| `d16_spider.py` | 香港政府统计处 | data.gov.hk CKAN + 官方统计 API | `--limit` `--delay` `--out` | `output/d16_censtatd.csv` |

---

## 三、用法

### 全量采集

在项目根目录执行（建议用 `-u` 以便实时看日志）：

```bash
python -u d01_spider.py
python -u d02_spider.py --only events
python -u d03_spider.py
python -u d04_spider.py
python -u d06_spider.py
python -u d07_spider.py
python -u d10_spider.py
python -u d11_spider.py
python -u d12_spider.py
python -u d13_spider.py
python -u d14_spider.py
python -u d15_spider.py
python -u d16_spider.py
```

### 小样本验证

推荐先用小样本确认字段与解析正确，再跑全量：

```bash
python -u d07_spider.py --years 2026 --max-pages 1 --limit-details 8 --out output/_sample.csv
python -u d10_spider.py --columns qczh_ann --max-pages 1 --limit-details 5 --out output/_sample.csv
python -u d11_spider.py --limit 3 --out output/_sample.csv
python -u d13_spider.py --max-pages 2 --limit-details 6 --out output/_sample.csv
python -u d16_spider.py --limit 5 --out output/_sample.csv
```

### D02 的分步采集

```bash
python -u d02_spider.py --only revoke     # 许可撤销公告
python -u d02_spider.py --only cancel     # 许可注销公告（大表）
python -u d02_spider.py --only rectify    # 限期整改
python -u d02_spider.py --only preapply   # 申请前公示
python -u d02_spider.py --only details    # 详情增强（撤销 + 申请前公示）
python -u d02_spider.py --only registry   # 许可登记簿（可选，约 38 万条）
```

---

## 四、统一字段

### 1. 事件表（11 个核心字段 + 各源扩展列）

| 字段 | 含义 |
|---|---|
| `source_id` | 来源编号，如 `D03` |
| `company_name` | 企业名称 |
| `event_date` | 事件 / 文档日期 |
| `publication_date` | 信息发布日期（无则留空） |
| `esg_dimension` | E / S / G 分类 |
| `event_type` | 事件类型，如「限期整改」「产品召回」 |
| `event_description` | 事件概述 |
| `penalty_amount` | 罚款 / 处罚金额（无则留空） |
| `case_status` | 涉嫌 / 调查 / 起诉 / 定罪 / 结案等 |
| `source_url` | 原始公告或报告链接 |
| `collected_at` | 采集时间（ISO 8601） |

各源扩展列见文末「附录：字段速查」。

### 2. 指标长表

通用字段：

```
source_id, dataset, dataset_version, region_level, region_code, region_name,
year, period, indicator, value, unit, statistical_calibre, source_url, collected_at
```

- `dataset` / `dataset_version`：数据集名与版本，跨版本不宜直接比较。
- `region_level` / `region_code` / `region_name`：区域层级与编码（国家 / 省 / 车辆 / 统计表）。
- `year` / `period`：年份与周期（周、季度等）。
- `indicator` / `value` / `unit`：指标名、数值、单位。
- `statistical_calibre`：统计口径说明。

D15 另有车辆字段（`vehicle_id`、`make`、`model`、`vehicle_class`、`powertrain` 等），D16 另统计表字段（`table_id`、`table_title`、`freq`、`sv_code`、`sv_desc` 等），D04 另水风险字段（`indicator_name`、`score`、`cat`、`label`、`scenario` 等）。

---

## 五、断点续跑与缓存

脚本普遍采用**本地缓存 + 单元级完成标记**，中断后重跑不会重复请求：

| 脚本 | 缓存目录 | 粒度 |
|---|---|---|
| D01 | `d01_tmp/` | 按月（`YYYYMM.csv`） |
| D02 | `d02_tmp/` | 按列表（`<key>.csv` + `<key>.done`），列表内逐页追加 |
| D03 | `d03_tmp/` | 按采集单元（整表或某省 `<unit>.json`） |
| D04 | `d04_tmp/` | 官方 xlsx 原文件 |
| D11 | `d11_tmp/` | PDF 缓存，避免重复下载 |
| D16 | `d16_tmp/` | 按统计表缓存 |

删除对应 `*_tmp/` 目录即强制重新采集；采集完成后可删除以释放空间（体积较大）。

---

## 六、反爬与合规注意事项

1. **限速**：各脚本内置 `delay`（默认 0.6–1.8 秒）与重试；请勿把间隔改得过小。
2. **D01**：只采集**公告索引元数据**，**不下载 PDF**——港交所对文件转载 / 再发布有使用限制。
3. **D02**：应用层 **IP 级封禁**（返回 458B「错误页」并 302 到 `errorinfo.jsp`），真实浏览器同样被拦，Selenium 无法绕过。应对：长冷却后重试；脚本识别错误页后退避 90 秒，多次失败安全中断并保留已采数据。
4. **D03**：匿名分页 `pageIndex` 仅允许 1–2 页（≥3 返回「安全考虑，请使用精准检索条件」）；单响应上限约 20 万行。大表用 `provinceId` 分片规避。
5. **D11**：站点有 JS 指纹验证（EO_Bot），`requests` 无法直取，统一用本机 Chrome；PDF 单独缓存。
6. **D12**：不爬网页，走官方导出端点；需先用 Chrome 取得 Cloudflare Cookie。
7. **D14**：`compcomm.hk` 与 `legalref.judiciary.hk` 会重置 Python 的 TLS 指纹，统一用 Chrome 取页。
8. 正式批量使用前请逐站核对 robots.txt、使用条款与访问频率；不以绕过验证码 / 登录限制的方式取数。

---
