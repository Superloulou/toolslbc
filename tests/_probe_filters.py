import io
import json
import re
import sys

sys.path.insert(0, r"C:\Users\cneuv\projects\leboncoin-alertes\src")

from lbc_alertes.scraper.leboncoin import extract_next_data, parse_search_page  # noqa: E402

html = io.open(r"C:\Users\cneuv\projects\leboncoin-alertes\tests\fixtures\search_cat83.html", encoding="utf-8").read()
data = extract_next_data(html)
pp = data["props"]["pageProps"]
sd = pp["searchData"]
print("ads", len(sd["ads"]))
print("total", sd.get("total"), "max_pages", sd.get("max_pages"))
print("keys searchData", sorted(sd.keys()))
filters = pp.get("search", {}).get("filters")
print(json.dumps(filters, ensure_ascii=False, indent=2)[:3000])
