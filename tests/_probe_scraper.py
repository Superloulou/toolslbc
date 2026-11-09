import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lbc_alertes.scraper import leboncoin as lb  # noqa: E402

html = (Path(__file__).resolve().parent / "fixtures" / "search_cat83.html").read_text(encoding="utf-8")
data = lb.extract_next_data(html)
p = data["props"]["pageProps"]
print("keys:", sorted(p.keys()))
print("filters:", json.dumps(p["search"].get("filters"), ensure_ascii=False)[:600])
print("total:", p["searchData"]["total"], "max_pages:", p["searchData"]["max_pages"])
sp = lb.parse_search_page(html)
print("ads:", len(sp.ads), "total:", sp.total, "max_pages:", sp.max_pages)
print("applied keys:", sorted(sp.applied_filters.keys()))

print("quote:", lb._quote("j'achète rtx/3090 x"))

# page sans searchData mais avec un titre
minimal = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(
    {"props": {"pageProps": {"title": "x", "search": {"filters": {"keywords": {"text": "rtx 3090"}}}}}}
) + "</script>"
sp2 = lb.parse_search_page(minimal)
print("empty page:", sp2.ads, sp2.total, sp2.max_pages, sp2.applied_filters)

# page sans searchData ni titre
bad = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {}}}) + "</script>"
try:
    lb.parse_search_page(bad)
except Exception as exc:  # noqa: BLE001
    print("bad page ->", type(exc).__name__, exc)

# page de challenge
try:
    lb.extract_next_data("<html><body>Captcha DataDome</body></html>")
except Exception as exc:  # noqa: BLE001
    print("challenge ->", type(exc).__name__, exc, isinstance(exc, lb.CaptchaError))

# absence totale de __NEXT_DATA__
try:
    lb.extract_next_data("<html><body>rien</body></html>")
except Exception as exc:  # noqa: BLE001
    print("missing ->", type(exc).__name__, exc)

# verify_applied
from lbc_alertes.config import SearchConfig  # noqa: E402

cfg = SearchConfig(name="rtx 3090", text="rtx 3090", category=83, price_min=200, price_max=800)
print("verify ok:", lb.verify_applied(cfg, sp.applied_filters))
print("verify bad:", lb.verify_applied(cfg, {"keywords": {"text": "autre"}, "category": {"id": 12}}))
