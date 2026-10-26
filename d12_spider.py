import os, time, warnings
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

warnings.filterwarnings("ignore")
PAGE = "https://www.business-humanrights.org/en/from-us/company-response-mechanism/"
DL = ("https://www.business-humanrights.org/en/pages/download/"
      "?microsite_page_id=626319&content_types=companyresponse")
OUT = r"c:\Users\Administrator\Desktop\pdfs\output\d12_bhrrc.csv"

opts = Options()
opts.add_argument("--disable-blink-features=AutomationControlled")
opts.add_experimental_option("excludeSwitches", ["enable-automation"])
opts.add_experimental_option("useAutomationExtension", False)
drv = webdriver.Chrome(options=opts)
try:
    drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"})
    drv.get(PAGE)
    for _ in range(40):
        if drv.title and "Just a moment" not in drv.title:
            break
        time.sleep(3)
    print("title:", drv.title)
    cookies = {c["name"]: c["value"] for c in drv.get_cookies()}
    ua = drv.execute_script("return navigator.userAgent")
    print("cookies:", list(cookies), "\nUA:", ua[:60])
finally:
    drv.quit()

s = requests.Session()
s.headers.update({"User-Agent": ua, "Accept-Language": "en;q=0.9", "Referer": PAGE})
for k, v in cookies.items():
    s.cookies.set(k, v, domain=".business-humanrights.org")

r = s.get(DL, stream=True, timeout=90, verify=False)
print("download status:", r.status_code, r.headers.get("Content-Type"),
      r.headers.get("Content-Disposition"))
if r.status_code == 200:
    n = 0
    with open(OUT, "wb") as f:
        for chunk in r.iter_content(65536):
            if chunk:
                f.write(chunk)
                n += len(chunk)
    print(f"saved {n} bytes -> {OUT}")
else:
    print("head:", r.text[:200])