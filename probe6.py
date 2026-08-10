import os, json, time, urllib.request
BASE = os.environ.get("FRONT_API_URL", "https://api2.frontapp.com")
TOK  = os.environ["FRONT_API_KEY"]
TYPES = ["inbound", "outbound", "out_reply", "comment"]

def measure(hours, cap=600):
    after = int(time.time() - hours * 3600)
    qs = "&".join(f"q[types][]={t}" for t in TYPES)
    url = f"{BASE}/events?q[after]={after}&limit=100&{qs}"
    t0 = time.time(); pages = 0; n = 0; convos = set()
    while url and pages < cap:
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + TOK, "Accept": "application/json"})
        b = json.loads(urllib.request.urlopen(req, timeout=30).read())
        pages += 1
        for e in b.get("_results", []):
            n += 1
            c = (e.get("conversation") or {}).get("id")
            if c: convos.add(c)
        url = (b.get("_pagination") or {}).get("next")
    print(f"{hours:>4}h window: {n:>5} activity events | {pages:>4} pages | "
          f"{len(convos):>4} distinct conversations | {time.time()-t0:>5.1f}s | "
          f"exhausted={not url}")

measure(1)
measure(6, cap=200)

