#!/usr/bin/env python3
"""
cf_persistent.py — Cloudflare Managed Challenge solver with session persistence.

Goal: solve CF challenges ONCE, then REUSE the clearance for the lifetime of the
session (typically 15-30 min for cf_clearance, up to 24h for high-trust).

Architecture:
  - Persistent Chromium profile (--user-data-dir) → preserves cookies/TLS session
  - SQLite cookie store (cf_session.db) with TTL tracking
  - Fingerprint consistency (canvas seed, WebGL spoof, navigator props) preserved
  - Challenge solving with HUMAN-LIKE behavior (Bezier mouse, type, scroll)
  - Smart cache: if cf_clearance is valid, skip browser entirely
  - Managed Challenge detection (3 types: JS, Turnstile, Managed)
  - Auto-reuse: HTTP requests via curl_cffi with stored UA/cookies/headers
  - Session health check: re-solve if CF blocks (403/503/CHALLENGE)

Usage:
  # 1. Solve challenge and save session
  python3 cf_persistent.py solve https://target.com
  
  # 2. Fetch protected page using saved session
  python3 cf_persistent.py fetch https://target.com/api/data
  
  # 3. Run continuous monitor (auto re-solve if expired)
  python3 cf_persistent.py monitor https://target.com --interval 60
  
  # 4. Show session info
  python3 cf_persistent.py status

Files:
  cf_session.db    - SQLite: cookies + fingerprint per host
  cf_profile/      - Chrome user data dir (cookies, cache, IndexedDB)
  cf_ua.txt        - stable User-Agent used for this session
"""

import asyncio
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error
import math
import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path

# ---------- deps ----------
try:
    import websockets
    from websockets.protocol import State
except ImportError:
    sys.exit("pip install websockets required")

try:
    from curl_cffi import requests as creq
    HAS_CURL = True
except ImportError:
    HAS_CURL = False
    print("[warn] curl_cffi not installed — fetch mode will use urllib only",
          file=sys.stderr)

# ---------- paths & state ----------
HOME = Path.home()
SESSION_DIR = HOME / ".cf_persistent"
SESSION_DIR.mkdir(exist_ok=True)
DB_PATH = SESSION_DIR / "cf_session.db"
PROFILE_DIR = SESSION_DIR / "cf_profile"
PROFILE_DIR.mkdir(exist_ok=True)
UA_FILE = SESSION_DIR / "cf_ua.txt"
LOG_FILE = SESSION_DIR / "cf_persistent.log"

PROOT = "proot-distro"
CHROME = "/root/chromium/chrome-linux/chrome"
CDP_PORT = 9222
DEBUG_URL = f"http://127.0.0.1:{CDP_PORT}"
CHROME_PID = "/root/chrome_persist.pid"
CHROME_LOG = "/root/chrome_persist.log"
PROFILE_REMOTE = "/root/cf_persistent_profile"

# ---------- logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("cf_persistent")


# ---------- persistent storage ----------
class SessionDB:
    """SQLite-backed cookie + fingerprint store with TTL."""

    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.path) as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    host TEXT PRIMARY KEY,
                    user_agent TEXT,
                    fingerprint_seed INTEGER,
                    fingerprint_json TEXT,
                    cf_clearance TEXT,
                    cf_clearance_expires INTEGER,
                    cf_bm TEXT,
                    session_created INTEGER,
                    last_used INTEGER,
                    last_challenge INTEGER,
                    challenge_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    notes TEXT
                );
                CREATE TABLE IF NOT EXISTS cookies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    host TEXT,
                    name TEXT,
                    value TEXT,
                    domain TEXT,
                    path TEXT,
                    expires INTEGER,
                    http_only INTEGER,
                    secure INTEGER,
                    same_site TEXT,
                    FOREIGN KEY (host) REFERENCES sessions(host) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_cookies_host ON cookies(host);
            """)

    def get_session(self, host: str) -> dict | None:
        with sqlite3.connect(self.path) as c:
            cur = c.execute(
                "SELECT * FROM sessions WHERE host=?",
                (host,),
            )
            cols = [d[0] for d in cur.description]
            r = cur.fetchone()
            if r:
                return dict(zip(cols, r))
        return None

    def save_session(self, host: str, ua: str, fp: dict,
                     cf_clearance: str | None = None,
                     cf_bm: str | None = None,
                     cf_clearance_ttl: int = 1800):
        now = int(time.time())
        expires = now + cf_clearance_ttl if cf_clearance else None
        fp_json = json.dumps(fp, sort_keys=True)
        with sqlite3.connect(self.path) as c:
            existing = c.execute(
                "SELECT challenge_count, success_count FROM sessions WHERE host=?",
                (host,),
            ).fetchone()
            if existing:
                cc, sc = existing
                c.execute("""
                    UPDATE sessions SET
                      user_agent=?, fingerprint_seed=?, fingerprint_json=?,
                      cf_clearance=COALESCE(?, cf_clearance),
                      cf_clearance_expires=COALESCE(?, cf_clearance_expires),
                      cf_bm=COALESCE(?, cf_bm),
                      last_used=?, last_challenge=?
                    WHERE host=?
                """, (ua, fp.get("_seed", 0), fp_json,
                      cf_clearance, expires, cf_bm,
                      now, now, host))
            else:
                c.execute("""
                    INSERT INTO sessions
                      (host, user_agent, fingerprint_seed, fingerprint_json,
                       cf_clearance, cf_clearance_expires, cf_bm,
                       session_created, last_used, last_challenge,
                       challenge_count, success_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
                """, (host, ua, fp.get("_seed", 0), fp_json,
                      cf_clearance, expires, cf_bm,
                      now, now, now))

    def save_cookies(self, host: str, cookies: list):
        with sqlite3.connect(self.path) as c:
            c.execute("DELETE FROM cookies WHERE host=?", (host,))
            for ck in cookies:
                c.execute("""
                    INSERT INTO cookies
                      (host, name, value, domain, path, expires,
                       http_only, secure, same_site)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    host, ck.get("name"), ck.get("value"),
                    ck.get("domain"), ck.get("path", "/"),
                    ck.get("expires", -1),
                    int(ck.get("httpOnly", False)),
                    int(ck.get("secure", False)),
                    ck.get("sameSite", "Lax"),
                ))

    def get_cookies(self, host: str) -> list:
        with sqlite3.connect(self.path) as c:
            rows = c.execute(
                "SELECT name, value, domain, path, expires, "
                "http_only, secure, same_site FROM cookies WHERE host=?",
                (host,),
            ).fetchall()
        out = []
        for r in rows:
            out.append({
                "name": r[0], "value": r[1], "domain": r[2], "path": r[3],
                "expires": r[4], "httpOnly": bool(r[5]),
                "secure": bool(r[6]), "sameSite": r[7],
            })
        return out

    def is_cf_clearance_valid(self, host: str) -> tuple[bool, int]:
        """Return (is_valid, seconds_until_expiry).
        Valid if EITHER cf_clearance OR cf_bm cookie is set and not expired."""
        s = self.get_session(host)
        if not s:
            return False, 0
        now = int(time.time())
        # check cf_clearance
        if s.get("cf_clearance") and s.get("cf_clearance_expires"):
            remaining = s["cf_clearance_expires"] - now
            if remaining > 300:
                return True, remaining
        # check cf_bm (Bot Management) — typically 30 min, no separate TTL in our DB
        if s.get("cf_bm"):
            # cf_bm has no explicit expiry stored, assume 30 min from last challenge
            last_chal = s.get("last_challenge", 0) or s.get("last_used", 0)
            if now - last_chal < 1800:  # within 30 min
                return True, 1800 - (now - last_chal)
        return False, 0

    def record_success(self, host: str):
        with sqlite3.connect(self.path) as c:
            c.execute(
                "UPDATE sessions SET success_count=success_count+1, "
                "last_used=? WHERE host=?",
                (int(time.time()), host),
            )

    def record_challenge(self, host: str):
        with sqlite3.connect(self.path) as c:
            c.execute(
                "UPDATE sessions SET challenge_count=challenge_count+1, "
                "last_challenge=? WHERE host=?",
                (int(time.time()), host),
            )

    def all_sessions(self) -> list[dict]:
        with sqlite3.connect(self.path) as c:
            rows = c.execute(
                "SELECT host, cf_clearance, cf_clearance_expires, "
                "last_used, challenge_count, success_count FROM sessions"
            ).fetchall()
        out = []
        for r in rows:
            exp = r[2] or 0
            now = int(time.time())
            out.append({
                "host": r[0],
                "has_clearance": bool(r[1]),
                "expires_in": exp - now if exp else -1,
                "last_used": r[3],
                "challenges": r[4],
                "successes": r[5],
            })
        return out


# ---------- stable fingerprint generator ----------
class Fingerprint:
    """Generate a STABLE per-host fingerprint that won't change across reloads."""

    DEFAULT = {
        "navigator_platform": "Linux x86_64",
        "navigator_languages": "en-US,en",
        "navigator_hardware_concurrency": 8,
        "navigator_device_memory": 8,
        "navigator_max_touch_points": 0,
        "screen_width": 1366,
        "screen_height": 768,
        "color_depth": 24,
        "timezone": "America/Los_Angeles",
        "webgl_vendor": "Intel Inc.",
        "webgl_renderer": "Intel Iris OpenGL Engine",
        "ua_chrome_version": "130.0.0.0",
    }

    def __init__(self, host: str):
        # deterministic seed from hostname
        h = hashlib.sha256(host.encode()).digest()
        self._seed = int.from_bytes(h[:4], "big")
        self._host = host
        self._cache_file = SESSION_DIR / f"fp_{host.replace('/', '_').replace(':', '_')}.json"
        self._load_or_generate()

    def _load_or_generate(self):
        if self._cache_file.exists():
            try:
                self.data = json.loads(self._cache_file.read_text())
                # ensure seed consistency
                if self.data.get("_seed") != self._seed:
                    self.data["_seed"] = self._seed
                return
            except Exception:
                pass
        # generate stable values from seed
        rng = random.Random(self._seed)
        self.data = {
            "_seed": self._seed,
            "_host": self._host,
            "navigator_platform": self.DEFAULT["navigator_platform"],
            "navigator_languages": ",".join(
                ["en-US", "en"] + (
                    ["id-ID", "id"] if rng.random() < 0.3 else []
                )
            ),
            "navigator_hardware_concurrency": rng.choice([4, 8, 8, 12, 16]),
            "navigator_device_memory": rng.choice([4, 8, 8, 16]),
            "navigator_max_touch_points": 0,
            "screen_width": rng.choice([1366, 1440, 1536, 1920]),
            "screen_height": rng.choice([768, 900, 864, 1080]),
            "color_depth": 24,
            "timezone": rng.choice([
                "America/Los_Angeles", "America/New_York",
                "Europe/London", "Europe/Berlin", "Asia/Jakarta",
            ]),
            "webgl_vendor": "Intel Inc.",
            "webgl_renderer": "Intel Iris OpenGL Engine",
            "ua_chrome_version": "130.0.0.0",
            "canvas_noise_seed": self._seed,
        }
        self._cache_file.write_text(json.dumps(self.data, indent=2))

    def get(self) -> dict:
        return self.data

    def user_agent(self) -> str:
        return (
            f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{self.data['ua_chrome_version']} "
            f"Safari/537.36"
        )

    def to_stealth_js(self) -> str:
        """Return JS code to inject — fingerprint values baked in."""
        d = self.data
        return r"""
(() => {
  // ---- Stable per-host fingerprint ----
  const FP = """ + json.dumps(d) + r""";
  // ---- Canvas noise (deterministic from FP.canvas_noise_seed) ----
  const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
  const _getImageData = CanvasRenderingContext2D.prototype.getImageData;
  function lcg(s){return (s*1664525+1013904223)&0xffffffff}
  let _state = FP.canvas_noise_seed;
  HTMLCanvasElement.prototype.toDataURL = function(...a){
    const ctx=this.getContext('2d'); if(ctx){
      try{const img=ctx.getImageData(0,0,this.width,this.height);
        for(let i=0;i<img.data.length;i+=4){
          if((_state=lcg(_state))%200===0){
            img.data[i]=(img.data[i]+1)&0xff;
            img.data[i+1]=(img.data[i+1]+1)&0xff;
            img.data[i+2]=(img.data[i+2]+1)&0xff;}}
        ctx.putImageData(img,0,0);}catch(e){}}
    return _toDataURL.apply(this,a);};
  const _proto=CanvasRenderingContext2D.prototype;
  const _getData=_proto.getImageData;
  _proto.getImageData=function(...a){
    const r=_getData.apply(this,a);
    for(let i=0;i<r.data.length;i+=4){
      if((_state=lcg(_state))%150===0){
        r.data[i]=(r.data[i]+1)&0xff;
        r.data[i+1]=(r.data[i+1]+1)&0xff;
        r.data[i+2]=(r.data[i+2]+1)&0xff;}}
    return r;};

  // ---- WebGL vendor spoof ----
  const _spoof={37445:FP.webgl_vendor, 37446:FP.webgl_renderer};
  const _gp=WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter=function(p){
    if(_spoof[p])return _spoof[p];return _gp.call(this,p);};
  if(typeof WebGL2RenderingContext!=='undefined'){
    const _gp2=WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter=function(p){
      if(_spoof[p])return _spoof[p];return _gp2.call(this,p);}}

  // ---- Navigator ----
  Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>FP.navigator_hardware_concurrency});
  Object.defineProperty(navigator,'deviceMemory',{get:()=>FP.navigator_device_memory});
  Object.defineProperty(navigator,'maxTouchPoints',{get:()=>FP.navigator_max_touch_points});
  Object.defineProperty(navigator,'platform',{get:()=>FP.navigator_platform});
  Object.defineProperty(navigator,'languages',{get:()=>FP.navigator_languages.split(',')});
  Object.defineProperty(navigator,'language',{get:()=>FP.navigator_languages.split(',')[0]});
  Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
  Object.defineProperty(navigator,'doNotTrack',{get:()=>null});

  // ---- Screen (limited — some props immutable in headless) ----
  try{Object.defineProperty(screen,'width',{get:()=>FP.screen_width,configurable:true});}catch(e){}
  try{Object.defineProperty(screen,'height',{get:()=>FP.screen_height,configurable:true});}catch(e){}
  try{Object.defineProperty(screen,'colorDepth',{get:()=>FP.color_depth,configurable:true});}catch(e){}

  // ---- Timezone ----
  try{
    const _orig=Date.prototype.getTimezoneOffset;
    const offsetMap={'America/Los_Angeles':480,'America/New_York':300,
      'Europe/London':0,'Europe/Berlin':-120,'Asia/Jakarta':-420};
    Date.prototype.getTimezoneOffset=function(){
      return offsetMap[FP.timezone]!==undefined?offsetMap[FP.timezone]:_orig.call(this);};
  }catch(e){}

  // ---- Plugins (realistic) ----
  Object.defineProperty(navigator,'plugins',{get:()=>{
    const arr=[
      {name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'Portable Document Format'},
      {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:''},
      {name:'Native Client',filename:'internal-nacl-plugin',description:''},
    ];
    arr.item=i=>arr[i]||null;
    arr.namedItem=n=>arr.find(p=>p.name===n)||null;
    arr.refresh=()=>{};return arr;}});

  // ---- WebGL extensions (strip debug) ----
  const _gse=WebGLRenderingContext.prototype.getSupportedExtensions;
  WebGLRenderingContext.prototype.getSupportedExtensions=function(){
    return _gse.call(this).filter(e=>!e.includes('debug'));};

  // ---- chrome.runtime (anti-detect) ----
  window.chrome=window.chrome||{};chrome.runtime=chrome.runtime||{};
  Object.defineProperty(chrome.runtime,'connect',{value:undefined,writable:false});
  Object.defineProperty(chrome.runtime,'sendMessage',{value:undefined,writable:false});

  // ---- Permissions API ----
  if(navigator.permissions){
    const _q=navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query=(p)=>{
      if(p.name==='notifications')return Promise.resolve({state:Notification.permission,name:'notifications'});
      return _q(p);};}

  // ---- Connection (network info) ----
  Object.defineProperty(navigator,'connection',{get:()=>({
    effectiveType:'4g',rtt:50,downlink:10,saveData:false
  })});

  console.log('[cf_persistent] fingerprint loaded, seed='+FP.canvas_noise_seed);
})();
"""


# ---------- CDP client (same minimal pattern as cf_stealth) ----------
class CDPClient:
    def __init__(self):
        self.ws = None
        self.id = 0
        self._fut = {}
        self.events: asyncio.Queue = asyncio.Queue()
        self._reader: asyncio.Task | None = None
        self.closed = False

    async def connect(self, retries=15):
        for i in range(retries):
            try:
                with urllib.request.urlopen(f"{DEBUG_URL}/json/list", timeout=2) as r:
                    tgts = json.loads(r.read())
                    page = next((t for t in tgts if t.get("type") == "page"), None)
                    if not page:
                        # create about:blank
                        with urllib.request.urlopen(
                            f"{DEBUG_URL}/json/new?about:blank", timeout=3
                        ) as rr:
                            page = json.loads(rr.read())
                    target = page["webSocketDebuggerUrl"]
                    log.debug("connected to %s", page.get("url", "?")[:50])
                    break
            except Exception as e:
                if i == retries - 1:
                    raise RuntimeError(f"CDP unreachable: {e}")
                await asyncio.sleep(1)
        self.ws = await websockets.connect(
            target, max_size=2**28, ping_interval=None, ping_timeout=None,
        )
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    fut = self._fut.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                else:
                    await self.events.put(msg)
        except Exception as e:
            if not self.closed:
                log.debug("reader exit: %s", e)

    async def cmd(self, method, params=None, timeout=30):
        self.id += 1
        req_id = self.id
        fut = asyncio.get_event_loop().create_future()
        self._fut[req_id] = fut
        msg = {"id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        await self.ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._fut.pop(req_id, None)
            raise

    async def js(self, expr, timeout=15):
        try:
            r = await self.cmd("Runtime.evaluate", {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
            }, timeout=timeout)
            if not r or "result" not in r:
                return ""
            res = r.get("result", {}).get("result", {})
            if res.get("type") == "string":
                return res.get("value", "")
            if "value" in res:
                return json.dumps(res["value"])
            return ""
        except Exception as e:
            return f"__ERR__:{e}"

    async def close(self):
        self.closed = True
        if self._reader:
            self._reader.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass


# ---------- Chrome lifecycle ----------
def cleanup_chrome():
    """Kill leftover Chrome/proot for this profile."""
    for proc in ["chrome_persist", "chrome-linux/chrome", "proot-distro.*ubuntu"]:
        subprocess.run(["pkill", "-9", "-f", proc],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)


def launch_chrome(profile_path: Path, user_agent: str):
    cleanup_chrome()
    flags = (
        "--headless=new --no-sandbox --disable-gpu "
        "--disable-dev-shm-usage "
        "--disable-extensions --no-first-run "
        "--no-default-browser-check "
        "--disable-background-networking "
        "--disable-component-update "
        "--disable-features=VizDisplayCompositor,Vulkan,UseSkiaRenderer,AudioServiceOutOfProcess "
        "--disable-accelerated-2d-canvas "
        "--disable-background-media-suspend "
        "--disable-renderer-backgrounding "
        "--disable-field-trial-config "
        "--enable-low-end-device-mode "
        "--use-gl=angle --use-angle=swiftshader "
        "--enable-unsafe-swiftshader "
        "--js-flags=--max-old-space-size=384 --jitless "
        f"--window-size=1366,768 "
        f"--user-data-dir={PROFILE_REMOTE} "
        f"--user-agent=\"{user_agent}\" "
        f"--remote-debugging-port={CDP_PORT} "
        "--remote-allow-origins=*"
    )
    launcher = (
        "#!/bin/bash\n"
        f"{CHROME} {flags} >{CHROME_LOG} 2>&1 &\n"
        "CHROME_PID=$!\n"
        f"echo $CHROME_PID > {CHROME_PID}\n"
        f"wait $CHROME_PID\n"
    )
    # write launcher inside proot
    write = subprocess.run(
        [PROOT, "login", "ubuntu", "--", "bash", "-c",
         f"cat > /root/launch_persist.sh <<'__HERMES_EOF__'\n{launcher}\n__HERMES_EOF__\n"
         f"chmod +x /root/launch_persist.sh"],
        capture_output=True, text=True, timeout=10,
    )
    # start
    subprocess.Popen(
        [PROOT, "login", "ubuntu", "--", "bash", "/root/launch_persist.sh"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, stdin=subprocess.DEVNULL,
    )
    # wait cdp
    for i in range(30):
        try:
            with urllib.request.urlopen(f"{DEBUG_URL}/json/version", timeout=1) as r:
                v = json.loads(r.read())
                log.info("chrome up: %s", v.get("Browser", "?"))
                return v
        except Exception:
            time.sleep(1)
    raise RuntimeError("Chrome failed to start in 30s")


# ---------- humanization (reused from cf_stealth) ----------
def bezier_points(p0, p1, p2, p3, n=20):
    pts = []
    for i in range(n + 1):
        t = i / n
        x = ((1-t)**3)*p0[0] + 3*((1-t)**2)*t*p1[0] + 3*(1-t)*(t**2)*p2[0] + (t**3)*p3[0]
        y = ((1-t)**3)*p0[1] + 3*((1-t)**2)*t*p1[1] + 3*(1-t)*(t**2)*p2[1] + (t**3)*p3[1]
        pts.append((x, y))
    return pts


async def human_mouse_move(c, start, end):
    dist = math.hypot(end[0] - start[0], end[1] - start[1])
    steps = max(8, min(40, int(dist / 12)))
    cp1 = (start[0] + (end[0]-start[0])*0.3 + random.uniform(-40, 40),
           start[1] + (end[1]-start[1])*0.3 + random.uniform(-40, 40))
    cp2 = (start[0] + (end[0]-start[0])*0.7 + random.uniform(-30, 30),
           start[1] + (end[1]-start[1])*0.7 + random.uniform(-30, 30))
    pts = bezier_points(start, cp1, cp2, end, n=steps)
    for i, (x, y) in enumerate(pts):
        jx, jy = x + random.uniform(-1.2, 1.2), y + random.uniform(-1.2, 1.2)
        try:
            await c.cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": jx, "y": jy,
                "button": "none", "buttons": 0,
            }, timeout=3)
        except Exception:
            return
        if i == 0:
            await asyncio.sleep(random.uniform(0.04, 0.12))
        elif i >= steps - 2:
            await asyncio.sleep(random.uniform(0.015, 0.04))
        else:
            await asyncio.sleep(random.uniform(0.008, 0.025))


async def human_click(c, x, y):
    await human_mouse_move(c, (random.uniform(100, 1200), random.uniform(100, 600)),
                            (x - random.uniform(-3, 3), y - random.uniform(-3, 3)))
    await asyncio.sleep(random.uniform(0.08, 0.18))
    await c.cmd("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x, "y": y,
        "button": "left", "buttons": 1, "clickCount": 1,
    }, timeout=3)
    await asyncio.sleep(random.uniform(0.08, 0.18))
    await c.cmd("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y,
        "button": "left", "buttons": 0, "clickCount": 1,
    }, timeout=3)


async def human_scroll(c, dy):
    n = max(1, abs(dy) // 100)
    step = dy // n
    for _ in range(n):
        try:
            await c.cmd("Input.dispatchMouseEvent", {
                "type": "mouseWheel",
                "x": random.uniform(400, 900),
                "y": random.uniform(200, 500),
                "deltaX": 0, "deltaY": step,
            }, timeout=3)
        except Exception:
            return
        await asyncio.sleep(random.uniform(0.05, 0.15))


# ---------- challenge detection & solve ----------
CHALLENGE_MARKERS = [
    "just a moment", "checking your browser", "verifying you are human",
    "attention required", "cf-challenge", "challenge-form",
    "turnstile", "hcaptcha",
]
SOLVED_COOKIES = {"cf_clearance", "__cf_bm", "cf_bm"}


async def detect_challenge(c) -> dict:
    """Detect challenge type. Returns {type, title, hasTurnstile, ...}."""
    script = """
    JSON.stringify({
      title: document.title,
      url: location.href,
      hasTurnstile: !!document.querySelector('[class*="turnstile"], iframe[src*="turnstile"]'),
      hasHcaptcha: !!document.querySelector('[class*="hcap"], iframe[src*="hcaptcha"]'),
      hasRecaptcha: !!document.querySelector('[class*="recaptcha"], iframe[src*="google.com/recaptcha"]'),
      hasCfChallenge: !!document.querySelector('#challenge-form, .cf-challenge, [class*="challenge"]'),
      hasManagedChallenge: !!document.querySelector('#cf-challenge-running, [id*="cf-"]'),
      bodyLen: (document.body ? document.body.innerText : '').length,
      bodyHead: (document.body ? document.body.innerText : '').slice(0, 400),
      turnstileBox: (() => {
        const e = document.querySelector('[class*="cf-turnstile"], .cf-turnstile, [data-sitekey]');
        if (!e) return null;
        const r = e.getBoundingClientRect();
        if (r.width < 5 || r.height < 5) return null;
        return {x: r.x, y: r.y, w: r.width, h: r.height};
      })(),
      chlFrame: (() => {
        const f = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
        if (!f) return null;
        const r = f.getBoundingClientRect();
        return {x: r.x, y: r.y, w: r.width, h: r.height, src: f.src};
      })(),
    })
    """
    raw = await c.js(script, timeout=8)
    try:
        info = json.loads(raw)
    except Exception:
        info = {"title": "", "url": "", "bodyLen": 0}
    # classify challenge type
    title = (info.get("title") or "").lower()
    body = (info.get("bodyHead") or "").lower()
    haystack = title + " " + body
    # element-based detection is primary; text markers are fallback
    info["isChallenge"] = (
        info.get("hasTurnstile") or info.get("hasCfChallenge")
        or info.get("hasHcaptcha") or info.get("hasRecaptcha")
        or info.get("chlFrame") is not None
        or any(mk in haystack for mk in ["just a moment", "checking your browser",
                                          "verifying you are human",
                                          "attention required"])
    )
    # challengeType: prioritize element-based (real widget present)
    info["challengeType"] = (
        "turnstile" if info.get("chlFrame") is not None
        else "turnstile" if info.get("hasTurnstile")
        else "hcaptcha" if info.get("hasHcaptcha")
        else "recaptcha" if info.get("hasRecaptcha")
        else "managed" if info.get("hasManagedChallenge")
        else "js" if info.get("isChallenge")
        else "none"
    )
    return info


async def solve_turnstile(c, info) -> bool:
    """Click Turnstile widget with human motion. Returns True if clicked."""
    box = info.get("turnstileBox") or info.get("chlFrame")
    log.debug("solve_turnstile box=%s", box)
    if not box or box.get("w", 0) < 10:
        log.info("no turnstileBox/chlFrame, searching iframes...")
        # last resort: search any visible iframe
        ifr = await c.js("""
            (() => {
              const ifs = document.querySelectorAll('iframe');
              for (const f of ifs) {
                const r = f.getBoundingClientRect();
                if (r.width > 50 && r.width < 800 && r.height > 30 && r.height < 300
                    && r.x > 50 && r.y > 50 && r.x < 1300 && r.y < 700) {
                  return JSON.stringify({x: r.x, y: r.y, w: r.width, h: r.height, src: f.src});
                }
              }
              return null;
            })()
        """)
        if not ifr or ifr == "null":
            log.info("no iframe found either")
            return False
        try:
            box = json.loads(ifr)
            log.info("found iframe fallback: %s", box)
        except Exception as e:
            log.warning("iframe parse: %s", e)
            return False
    cx = box["x"] + box["w"] / 2
    cy = box["y"] + box["h"] / 2
    log.info("clicking turnstile at (%.0f, %.0f)", cx, cy)
    await human_click(c, cx, cy)
    log.info("click dispatched")
    return True


# ---------- solver core ----------
async def solve_managed_challenge(url: str, db: SessionDB, timeout=120):
    """
    Solve CF Managed Challenge for `url`, persist session to DB.
    Returns (success, cookies_dict, html).
    """
    host = urllib.parse.urlparse(url).netloc
    log.info("=== solving challenge for %s ===", host)
    fp = Fingerprint(host)
    ua = fp.user_agent()
    log.info("user-agent: %s", ua)
    log.info("fingerprint seed: %d", fp.data["_seed"])

    launch_chrome(PROFILE_DIR, ua)
    c = CDPClient()
    try:
        await c.connect()

        # enable domains
        for dom in ["Page.enable", "Runtime.enable", "Network.enable", "DOM.enable"]:
            for r in range(4):
                try:
                    to = 30 if r == 0 else 12
                    await c.cmd(dom, timeout=to)
                    break
                except Exception as e:
                    if r < 3:
                        await asyncio.sleep(2)
                        if not c.ws or c.ws.state is not State.OPEN:
                            try:
                                with urllib.request.urlopen(
                                    f"{DEBUG_URL}/json/list", timeout=2
                                ) as rr:
                                    t = json.loads(rr.read())
                                    page = next(
                                        (x for x in t if x.get("type") == "page"),
                                        None,
                                    )
                                    if page:
                                        c.ws = await websockets.connect(
                                            page["webSocketDebuggerUrl"],
                                            max_size=2**28, ping_interval=None,
                                            ping_timeout=None,
                                        )
                                        c._reader = asyncio.create_task(c._read_loop())
                            except Exception:
                                pass
                    else:
                        log.warning("%s failed: %s", dom, e)

        # viewport
        try:
            await c.cmd("Emulation.setDeviceMetricsOverride", {
                "width": fp.data["screen_width"],
                "height": fp.data["screen_height"],
                "deviceScaleFactor": 1, "mobile": False,
            })
        except Exception:
            pass

        # inject STABLE fingerprint (per-host)
        try:
            await c.cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": fp.to_stealth_js(),
            })
            log.info("stable fingerprint injected")
        except Exception as e:
            log.warning("stealth inject: %s", e)

        # save UA to file
        UA_FILE.write_text(ua)

        # warm up: human-like mouse wander BEFORE navigating
        for _ in range(random.randint(3, 5)):
            await human_mouse_move(
                c, (random.uniform(100, 1200), random.uniform(100, 600)),
                (random.uniform(100, 1200), random.uniform(100, 600)),
            )
            await asyncio.sleep(random.uniform(0.2, 0.5))

        # navigate
        log.info("navigate → %s", url)
        await c.cmd("Page.navigate", {"url": url})
        # brief drain
        for _ in range(5):
            try:
                await asyncio.wait_for(c.events.get(), timeout=0.3)
            except asyncio.TimeoutError:
                pass
        await asyncio.sleep(random.uniform(1.0, 2.0))

        t0 = time.time()
        last_check = 0
        last_widget_click = 0
        last_state_print = 0
        challenge_solved = False
        consecutive_no_challenge = 0

        while time.time() - t0 < timeout:
            now = time.time()
            # drain events
            try:
                while True:
                    ev = c.events.get_nowait()
            except Exception:
                pass

            # check state every 3s
            if now - last_check >= 3:
                last_check = now
                info = await detect_challenge(c)
                is_challenge = info.get("isChallenge", False)
                ctype = info.get("challengeType", "none")

                if now - last_state_print >= 6:
                    last_state_print = now
                    log.info("t=%ds type=%s title=%r bodyLen=%d",
                             int(now - t0), ctype,
                             info.get("title", "")[:40], info.get("bodyLen", 0))

                if is_challenge:
                    db.record_challenge(host)
                    consecutive_no_challenge = 0
                    # try solve
                    if ctype in ("turnstile", "managed", "js"):
                        if now - last_widget_click >= 8:
                            last_widget_click = now
                            clicked = await solve_turnstile(c, info)
                            if clicked:
                                # human-like behavior after click
                                await human_scroll(c, random.randint(80, 200))
                                await asyncio.sleep(random.uniform(0.5, 1.2))
                else:
                    consecutive_no_challenge += 1
                    if consecutive_no_challenge >= 2:
                        # stable for 6s — assume success
                        log.info("page stable for 6s, assuming success")
                        break

                # check cookies
                try:
                    cr = await c.cmd("Network.getAllCookies", {}, timeout=8)
                    cookies = cr.get("result", {}).get("cookies", [])
                    jar = {ck["name"]: ck["value"] for ck in cookies}
                    if any(k in jar for k in SOLVED_COOKIES):
                        # CF clearance found
                        cf_clear = jar.get("cf_clearance") or jar.get("cf_bm")
                        cf_bm = jar.get("__cf_bm") or jar.get("cf_bm")
                        cf_ttl = 1800  # default 30 min
                        # parse TTL from cookie if available
                        for ck in cookies:
                            if ck.get("name") in ("cf_clearance", "cf_bm", "__cf_bm"):
                                exp = ck.get("expires", -1)
                                if exp > 0:
                                    cf_ttl = max(60, int(exp - time.time()))
                        db.save_session(
                            host, ua, fp.data,
                            cf_clearance=cf_clear, cf_bm=cf_bm,
                            cf_clearance_ttl=cf_ttl,
                        )
                        db.save_cookies(host, cookies)
                        db.record_success(host)
                        log.info("✅ cf_clearance saved (TTL=%ds, expires in %s)",
                                 cf_ttl,
                                 datetime.fromtimestamp(time.time() + cf_ttl).isoformat())
                        challenge_solved = True
                        # get final html
                        html = await c.js(
                            "document.documentElement.outerHTML", timeout=10
                        )
                        if html and not html.startswith("__ERR__"):
                            (SESSION_DIR / f"page_{host.replace(':', '_')}.html").write_text(html)
                        return True, jar, html
                except Exception as e:
                    log.debug("cookie check: %s", e)

            await asyncio.sleep(random.uniform(0.3, 0.7))

        # timeout
        log.warning("timeout %ds — challenge may not be fully solved", timeout)
        return False, {}, ""

    finally:
        await c.close()
        cleanup_chrome()


# ---------- fetch mode (reuse session) ----------
def fetch_with_session(url: str, db: SessionDB, force_solve=False) -> dict:
    """
    Fetch URL using saved session. If cf_clearance valid, use curl_cffi
    with same fingerprint. If expired or 403, re-solve.

    Returns: {ok, status, html, cookies, used_browser, message}
    """
    import urllib.parse
    host = urllib.parse.urlparse(url).netloc
    log.info("=== fetch %s ===", url)

    if force_solve:
        log.info("force_solve=True — re-solving")
        valid, _ = db.is_cf_clearance_valid(host)
        if valid:
            db.get_session(host)  # touch
        ok, jar, html = asyncio.run(solve_managed_challenge(url, db, timeout=90))
        return {
            "ok": ok, "status": 200 if ok else 403,
            "html": html, "cookies": jar,
            "used_browser": True,
            "message": "force-solved" if ok else "solve failed",
        }

    valid, remaining = db.is_cf_clearance_valid(host)
    if not valid:
        log.info("no valid session — solving first")
        ok, jar, html = asyncio.run(solve_managed_challenge(url, db, timeout=90))
        if not ok:
            return {
                "ok": False, "status": 403, "html": "",
                "cookies": {}, "used_browser": True,
                "message": "no session and solve failed",
            }
        return {
            "ok": True, "status": 200, "html": html, "cookies": jar,
            "used_browser": True, "message": "solved + fetched",
        }

    log.info("reusing session, %.0fs remaining", remaining)
    sess = db.get_session(host)
    cookies_list = db.get_cookies(host)
    cookies_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies_list)
    ua = sess.get("user_agent") or "Mozilla/5.0 ..."

    if not HAS_CURL:
        # urllib fallback
        req = urllib.request.Request(url, headers={
            "User-Agent": ua,
            "Cookie": cookies_str,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", errors="replace")
                status = r.status
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            status = e.code
    else:
        # build cookie jar
        jar = {}
        for c in cookies_list:
            jar[c["name"]] = c["value"]
        try:
            r = creq.get(
                url,
                headers={
                    "User-Agent": ua,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                cookies=jar,
                impersonate="chrome124",
                timeout=30,
                allow_redirects=True,
            )
            body = r.text
            status = r.status_code
        except Exception as e:
            log.error("curl_cffi fetch: %s", e)
            return {
                "ok": False, "status": 0, "html": "",
                "cookies": {}, "used_browser": False,
                "message": f"fetch error: {e}",
            }

    # detect challenge in response
    is_challenge = (
        status in (403, 503, 429)
        or "cf-chl" in body.lower()
        or "just a moment" in body.lower()
        or "checking your browser" in body.lower()
        or 'id="challenge-form"' in body
    )

    if is_challenge:
        log.warning("server returned challenge (status=%d) — re-solving", status)
        ok, jar, html = asyncio.run(solve_managed_challenge(url, db, timeout=90))
        return {
            "ok": ok, "status": 200 if ok else 403,
            "html": html, "cookies": jar,
            "used_browser": True,
            "message": "re-solved (was challenged)",
        }

    db.record_success(host)
    log.info("✅ fetched %d bytes, status=%d", len(body), status)
    return {
        "ok": True, "status": status, "html": body, "cookies": {},
        "used_browser": False, "message": "used cached session",
    }


# ---------- monitor mode ----------
async def monitor(url: str, db: SessionDB, interval: int = 60, max_iter: int = 0):
    """
    Continuously fetch URL, auto re-solve if challenge appears.
    max_iter=0 means forever.
    """
    import urllib.parse
    host = urllib.parse.urlparse(url).netloc
    log.info("=== monitor mode: %s, interval=%ds ===", url, interval)
    iteration = 0
    while max_iter == 0 or iteration < max_iter:
        iteration += 1
        log.info("--- iteration %d ---", iteration)
        result = fetch_with_session(url, db)
        log.info("result: ok=%s status=%d msg=%s browser=%s",
                 result["ok"], result["status"], result["message"],
                 result["used_browser"])
        if result["ok"]:
            valid, remaining = db.is_cf_clearance_valid(host)
            log.info("session valid: %s, %.0fs remaining", valid, remaining)
        await asyncio.sleep(interval)


# ---------- CLI ----------
def cmd_solve(args):
    db = SessionDB()
    ok, jar, html = asyncio.run(solve_managed_challenge(args.url, db, timeout=args.timeout))
    print(f"{'✅' if ok else '❌'} {args.url}")
    print(f"  cookies: {len(jar)}")
    for k, v in list(jar.items())[:8]:
        if k in SOLVED_COOKIES:
            print(f"  · {k}={v[:50]}...")
    sys.exit(0 if ok else 1)


def cmd_fetch(args):
    db = SessionDB()
    r = fetch_with_session(args.url, db, force_solve=args.force)
    print(f"{'✅' if r['ok'] else '❌'} {args.url} (status={r['status']})")
    print(f"  mode: {r['message']}")
    if args.save_html and r["html"]:
        out = Path(args.save_html)
        out.write_text(r["html"])
        print(f"  saved: {out} ({len(r['html'])} bytes)")
    sys.exit(0 if r["ok"] else 1)


def cmd_status(args):
    db = SessionDB()
    sessions = db.all_sessions()
    if not sessions:
        print("no sessions saved")
        return
    print(f"{'host':30s} {'clearance':10s} {'expires_in':12s} "
          f"{'last_used':12s} {'#chall':7s} {'#succ':7s}")
    print("-" * 90)
    now = int(time.time())
    for s in sessions:
        exp = s["expires_in"]
        exp_str = (
            f"{exp//60}m{(exp%60):02d}s" if exp > 0
            else ("expired" if exp < 0 else "—")
        )
        last = s["last_used"]
        last_str = (
            datetime.fromtimestamp(last).strftime("%H:%M:%S")
            if last else "—"
        )
        print(f"{s['host']:30s} "
              f"{('yes' if s['has_clearance'] else 'no'):10s} "
              f"{exp_str:12s} {last_str:12s} "
              f"{s['challenges']:7d} {s['successes']:7d}")


def cmd_monitor(args):
    db = SessionDB()
    asyncio.run(monitor(args.url, db, interval=args.interval, max_iter=args.max_iter))


def main():
    import argparse
    p = argparse.ArgumentParser(
        description="Cloudflare Managed Challenge solver with session persistence",
    )
    sub = p.add_subparsers(dest="cmd")

    # solve
    sp = sub.add_parser("solve", help="solve challenge and save session")
    sp.add_argument("url")
    sp.add_argument("--timeout", type=int, default=120)
    sp.set_defaults(func=cmd_solve)

    # fetch
    fp = sub.add_parser("fetch", help="fetch URL using saved session")
    fp.add_argument("url")
    fp.add_argument("--force", action="store_true",
                    help="force re-solve even if session valid")
    fp.add_argument("--save-html", help="path to save HTML response")
    fp.set_defaults(func=cmd_fetch)

    # status
    sp = sub.add_parser("status", help="show all saved sessions")
    sp.set_defaults(func=cmd_status)

    # monitor
    mp = sub.add_parser("monitor", help="continuous fetch + auto re-solve")
    mp.add_argument("url")
    mp.add_argument("--interval", type=int, default=60)
    mp.add_argument("--max-iter", type=int, default=0, help="0=forever")
    mp.set_defaults(func=cmd_monitor)

    if len(sys.argv) < 2:
        p.print_help()
        sys.exit(1)
    args = p.parse_args()
    if not hasattr(args, "func"):
        p.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
