# 只读排查：调开放平台 webadmin-archive-articlelist / admin-archive-detail，打印后台栏目对照表和指定文章的 tabs / archivetopic / channels。
# 用法（服务器 ~/automatic-post 下）：.venv/bin/python scripts/probe_dqd_tabs.py <archive_id> ...（每次调用间隔 1.2s，避开 QPS=2 限流）
import json, secrets, sys, time
from urllib.parse import urlencode
import requests
sys.path.insert(0, ".")
from app.config import AppConfig
from app.services.open_platform import sign_query_params

cfg = AppConfig()
GW = "https://platform.dongqiudi.com/open/v1/do"

def call(api, **params):
    q = [("api_name", api), ("appid", cfg.dqd_open_appid), ("timestamp", str(int(time.time()))), ("nonce", secrets.token_hex(16))]
    q += [(k, str(v)) for k, v in params.items()]
    q.append(("sign", sign_query_params(q, cfg.dqd_open_appsecret)))
    r = requests.get(GW + "?" + urlencode(q), timeout=20)
    time.sleep(1.2)
    return r.json()

lst = call("webadmin-archive-articlelist", tab=0, page=1, size=1)
d = lst.get("data") or {}
if isinstance(d, dict) and "data" in d and "tablistnew" not in d:
    print("outer code", lst.get("code"), "inner code", d.get("code"), d.get("message"))
    d = d.get("data") or {}
print("tablistnew:", json.dumps(d.get("tablistnew"), ensure_ascii=False))
print("tablist:", json.dumps(d.get("tablist"), ensure_ascii=False))
for aid in sys.argv[1:]:
    det = call("admin-archive-detail", archive_id=aid)
    x = det.get("data") or {}
    if isinstance(x, dict) and "data" in x: x = x.get("data") or {}
    print(aid, {k: x.get(k) for k in ("title", "tabs", "archivetopic", "channels", "channelsnew", "classifications")})
