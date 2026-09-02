#!/usr/bin/env python3
"""
cf_selenium.py — Full-featured browser automation with built-in CF bypass.

Features:
  - Selenium-like API (Browser, WebElement)
  - Auto Cloudflare Managed Challenge solving (Turnstile, hCaptcha, JS, Managed)
  - Per-host stable fingerprint (canvas, WebGL, navigator, timezone, etc.)
  - Persistent profile (cookies survive across runs)
  - SQLite session cache (cf_clearance / cf_bm reuse)
  - Humanized actions (Bezier mouse, variable typing, smooth scroll)
  - **Multi-tab** (TabManager, switch, new tab, close, parallel)
  - **Recording** (record → save JSON → replay)
  - **AI inspection** (rich state, page snapshot, structured events,
    accessibility tree, screenshot, summary for LLM agents)
  - **Agent mode** (high-level actions: open, click_by_text, fill, submit, etc.)

Usage:
    from cf_selenium import Browser, Tab, Recorder

    # --- basic ---
    b = Browser(profile="default")
    b.get("https://nowsecure.nl")
    print(b.title, b.cookies)
    b.find_element("h1").click()
    b.quit()

    # --- context manager (auto-quit) ---
    with Browser() as b:
        b.get("https://example.com")
        el = b.find_element("#login")
        el.click()
        el.type("user@x.com")
        b.find_element("#pass").type("secret")
        b.find_element("button[type=submit]").click()

    # --- multi-tab ---
    with Browser() as b:
        b.get("https://a.com")
        b.new_tab("https://b.com")
        b.switch_tab(0)
        print("tab 0:", b.title)
        b.switch_tab(1)
        print("tab 1:", b.title)
        b.close_tab()  # close current

    # --- recording ---
    rec = Recorder()
    rec.start()
    b = Browser()
    b.get("https://x.com")
    b.find_element("a").click()        # recorded
    b.find_element("#q").type("hi")    # recorded
    b.find_element("#submit").click()  # recorded
    rec.save("session.json")
    b.quit()
    # later:
    Recorder.replay("session.json", headless=True)

    # --- AI inspection (LLM agent) ---
    b = Browser()
    b.get("https://example.com")
    snap = b.snapshot()             # full page state for LLM
    print(snap["summary"])          # one-line description
    print(snap["actions"])          # suggested actions
    b.screenshot("page.png")        # visual capture
    b.find_by_text("Login").click() # AI-friendly: find by visible text
    b.quit()
"""

import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import math
import hashlib
import sqlite3
import logging
import base64
from pathlib import Path
from typing import Any
from datetime import datetime

try:
    import websockets
except ImportError:
    sys.exit("pip install websockets required")

# State.OPEN lives in different modules across websockets versions.
# websockets<11 had `websockets.protocol.State`, websockets>=11 moved it to
# `websockets.connection.protocol.State` (lazy-imported). We resolve at runtime.
try:
    from websockets.protocol import State  # type: ignore
except Exception:
    try:
        from websockets.connection.protocol import State  # type: ignore
    except Exception:
        # Fallback: hardcode the magic number 1 = OPEN (stable across all versions).
        class _State:
            OPEN = 1
        State = _State()

try:
    from curl_cffi import requests as creq
    HAS_CURL = True
except ImportError:
    HAS_CURL = False

# ---------- paths ----------
HOME = Path.home()
PKG_DIR = HOME / ".cf_selenium"
PKG_DIR.mkdir(exist_ok=True)
PROFILE_DIR = PKG_DIR / "profiles"
PROFILE_DIR.mkdir(exist_ok=True)
RECORDINGS_DIR = PKG_DIR / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)
SCREENSHOTS_DIR = PKG_DIR / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)
SESSION_DIR = PKG_DIR / "sessions"
SESSION_DIR.mkdir(exist_ok=True)
DB_PATH = PKG_DIR / "cf_selenium.db"

PROOT = "proot-distro"
CHROME = "/root/chromium/chrome-linux/chrome"
CDP_PORT = 9222
DEBUG_URL = f"http://127.0.0.1:{CDP_PORT}"
CHROME_PID_FILE = "/root/chrome_cfsel.pid"
CHROME_LOG = "/root/chrome_cfsel.log"

# ---------- logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(PKG_DIR / "cf_selenium.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("cf_selenium")

CHALLENGE_MARKERS = [
    "just a moment", "checking your browser", "verifying you are human",
    "attention required", "cf-challenge", "challenge-form",
]
SOLVED_COOKIES = {"cf_clearance", "__cf_bm", "cf_bm"}


# ===================================================================
# Stable fingerprint (per-host, deterministic)
# ===================================================================
class Fingerprint:
    # Realistic Android Chrome on a mid-range phone (Redmi Note 11 / MT6781 / 6GB)
    # All values must be INTERNALLY CONSISTENT with the user agent string
    DEFAULT = {
        # Android Chrome UA
        "navigator_platform": "Linux armv81",
        "navigator_languages": "en-US,en",
        "ua_chrome_version": "130.0.0.0",
        # WebGL: Mali-G57 (real GPU on MT6781 Helio G96)
        # Use a generic mobile GPU string
        "webgl_vendor": "ARM",
        "webgl_renderer": "Mali-G57 MC2",
        # Redmi Note 11 screen: 1080x2400, ~6.5"
        "screen_width": 1080,
        "screen_height": 2400,
        "color_depth": 24,
        # device
        "navigator_hardware_concurrency": 8,   # MT6781: 2xA76 + 6xA55 = 8
        "navigator_device_memory": 4,           # 6GB physical, but JS reports 4
        "navigator_max_touch_points": 5,        # touchscreen
    }

    def __init__(self, host: str):
        h = hashlib.sha256(host.encode()).digest()
        self._seed = int.from_bytes(h[:4], "big")
        self._host = host
        self._cache = PKG_DIR / f"fp_{host.replace('/', '_').replace(':', '_')}.json"
        self._load_or_generate()

    def _load_or_generate(self):
        if self._cache.exists():
            try:
                self.data = json.loads(self._cache.read_text())
                if self.data.get("_seed") != self._seed:
                    self.data["_seed"] = self._seed
                # Reset to defaults to ensure consistency on every run
                # (don't keep stale values from old code)
                self.data["navigator_platform"] = self.DEFAULT["navigator_platform"]
                self.data["webgl_vendor"] = self.DEFAULT["webgl_vendor"]
                self.data["webgl_renderer"] = self.DEFAULT["webgl_renderer"]
                self.data["screen_width"] = self.DEFAULT["screen_width"]
                self.data["screen_height"] = self.DEFAULT["screen_height"]
                self.data["navigator_hardware_concurrency"] = self.DEFAULT["navigator_hardware_concurrency"]
                self.data["navigator_device_memory"] = self.DEFAULT["navigator_device_memory"]
                self.data["navigator_max_touch_points"] = self.DEFAULT["navigator_max_touch_points"]
                return
            except Exception:
                pass
        rng = random.Random(self._seed)
        self.data = {
            "_seed": self._seed,
            "_host": self._host,
            "navigator_platform": self.DEFAULT["navigator_platform"],
            "navigator_languages": ",".join(
                ["en-US", "en"] + (["id-ID", "id"] if rng.random() < 0.3 else [])
            ),
            "navigator_hardware_concurrency": self.DEFAULT["navigator_hardware_concurrency"],
            "navigator_device_memory": self.DEFAULT["navigator_device_memory"],
            "navigator_max_touch_points": self.DEFAULT["navigator_max_touch_points"],
            "screen_width": self.DEFAULT["screen_width"],
            "screen_height": self.DEFAULT["screen_height"],
            "color_depth": 24,
            "timezone": rng.choice([
                "America/Los_Angeles", "America/New_York",
                "Europe/London", "Europe/Berlin", "Asia/Jakarta",
            ]),
            "webgl_vendor": self.DEFAULT["webgl_vendor"],
            "webgl_renderer": self.DEFAULT["webgl_renderer"],
            "ua_chrome_version": self.DEFAULT["ua_chrome_version"],
            "canvas_noise_seed": self._seed,
        }
        self._cache.write_text(json.dumps(self.data, indent=2))

    def get(self) -> dict:
        return self.data

    def user_agent(self) -> str:
        # Real Android Chrome UA on Redmi Note 11 with Chrome 130
        # Matches: Linux armv81, Mali-G57, 1080x2400
        return (
            f"Mozilla/5.0 (Linux; Android 13; 2201117PG) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{self.data['ua_chrome_version']} "
            f"Mobile Safari/537.36"
        )

    def to_stealth_js(self) -> str:
        d = self.data
        return r"""
(() => {
  const FP = """ + json.dumps(d) + r""";
  function lcg(s){return (s*1664525+1013904223)&0xffffffff}
  let _state = FP.canvas_noise_seed;
  // canvas
  const _td=HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL=function(...a){
    const ctx=this.getContext('2d');if(ctx){
      try{const img=ctx.getImageData(0,0,this.width,this.height);
        for(let i=0;i<img.data.length;i+=4){
          if((_state=lcg(_state))%200===0){
            img.data[i]=(img.data[i]+1)&0xff;
            img.data[i+1]=(img.data[i+1]+1)&0xff;
            img.data[i+2]=(img.data[i+2]+1)&0xff;}}
        ctx.putImageData(img,0,0);}catch(e){}}
    return _td.apply(this,a);};
  const _proto=CanvasRenderingContext2D.prototype;
  const _gd=_proto.getImageData;
  _proto.getImageData=function(...a){
    const r=_gd.apply(this,a);
    for(let i=0;i<r.data.length;i+=4){
      if((_state=lcg(_state))%150===0){
        r.data[i]=(r.data[i]+1)&0xff;
        r.data[i+1]=(r.data[i+1]+1)&0xff;
        r.data[i+2]=(r.data[i+2]+1)&0xff;}}
    return r;};
  // webgl
  const _spoof={37445:FP.webgl_vendor,37446:FP.webgl_renderer};
  const _gp=WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter=function(p){
    if(_spoof[p])return _spoof[p];return _gp.call(this,p);};
  if(typeof WebGL2RenderingContext!=='undefined'){
    const _gp2=WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter=function(p){
      if(_spoof[p])return _spoof[p];return _gp2.call(this,p);}}
  // navigator
  Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>FP.navigator_hardware_concurrency});
  Object.defineProperty(navigator,'deviceMemory',{get:()=>FP.navigator_device_memory});
  Object.defineProperty(navigator,'maxTouchPoints',{get:()=>FP.navigator_max_touch_points});
  Object.defineProperty(navigator,'platform',{get:()=>FP.navigator_platform});
  Object.defineProperty(navigator,'languages',{get:()=>FP.navigator_languages.split(',')});
  Object.defineProperty(navigator,'language',{get:()=>FP.navigator_languages.split(',')[0]});
  Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
  Object.defineProperty(navigator,'doNotTrack',{get:()=>null});
  // screen (mobile: availWidth=screenWidth, no desktop chrome)
  try{Object.defineProperty(screen,'width',{get:()=>FP.screen_width,configurable:true});}catch(e){}
  try{Object.defineProperty(screen,'height',{get:()=>FP.screen_height,configurable:true});}catch(e){}
  try{Object.defineProperty(screen,'availWidth',{get:()=>FP.screen_width,configurable:true});}catch(e){}
  try{Object.defineProperty(screen,'availHeight',{get:()=>FP.screen_height,configurable:true});}catch(e){}
  try{Object.defineProperty(screen,'colorDepth',{get:()=>FP.color_depth,configurable:true});}catch(e){}
  try{Object.defineProperty(screen,'pixelDepth',{get:()=>FP.color_depth,configurable:true});}catch(e){}
  // touch: mobile has touch event support
  try{Object.defineProperty(window,'ontouchstart',{get:()=>null,configurable:true});}catch(e){}
  Object.defineProperty(navigator,'maxTouchPoints',{get:()=>FP.navigator_max_touch_points,configurable:true});
  // iframe contentWindow fingerprint inherits
  try{Object.defineProperty(Element.prototype,'onpointerrawupdate','x',{get:()=>undefined});}catch(e){}
  // timezone
  try{const _orig=Date.prototype.getTimezoneOffset;
    const om={'America/Los_Angeles':480,'America/New_York':300,
      'Europe/London':0,'Europe/Berlin':-120,'Asia/Jakarta':-420};
    Date.prototype.getTimezoneOffset=function(){
      return om[FP.timezone]!==undefined?om[FP.timezone]:_orig.call(this);};}catch(e){}
  // plugins (mobile Chrome has NO plugins)
  Object.defineProperty(navigator,'plugins',{get:()=>{
    const arr=[];arr.item=()=>null;arr.namedItem=()=>null;arr.refresh=()=>{};return arr;}});
  Object.defineProperty(navigator,'mimeTypes',{get:()=>{
    const arr=[];arr.item=()=>null;arr.namedItem=()=>null;return arr;}});
  // ext
  const _gse=WebGLRenderingContext.prototype.getSupportedExtensions;
  WebGLRenderingContext.prototype.getSupportedExtensions=function(){
    return _gse.call(this).filter(e=>!e.includes('debug'));};
  // chrome.runtime
  window.chrome=window.chrome||{};chrome.runtime=chrome.runtime||{};
  Object.defineProperty(chrome.runtime,'connect',{value:undefined,writable:false});
  Object.defineProperty(chrome.runtime,'sendMessage',{value:undefined,writable:false});
  // permissions
  if(navigator.permissions){
    const _q=navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query=(p)=>{
      if(p.name==='notifications')return Promise.resolve({state:Notification.permission,name:'notifications'});
      return _q(p);};}
  Object.defineProperty(navigator,'connection',{get:()=>({
    effectiveType:'4g',rtt:50,downlink:10,saveData:false})});
  // expose for AI inspection
  window.__cf_selenium_fp = FP;
  console.log('[cf_selenium] fp loaded, seed='+FP.canvas_noise_seed);
})();
"""


# ===================================================================
# Session store (SQLite)
# ===================================================================
class SessionDB:
    def __init__(self):
        self.path = DB_PATH
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.path) as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    host TEXT PRIMARY KEY,
                    user_agent TEXT,
                    fingerprint_json TEXT,
                    cf_clearance TEXT,
                    cf_clearance_expires INTEGER,
                    cf_bm TEXT,
                    session_created INTEGER,
                    last_used INTEGER,
                    last_challenge INTEGER,
                    challenge_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0
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
            """)

    def get_session(self, host):
        with sqlite3.connect(self.path) as c:
            cur = c.execute("SELECT * FROM sessions WHERE host=?", (host,))
            cols = [d[0] for d in cur.description]
            r = cur.fetchone()
            if r:
                return dict(zip(cols, r))
        return None

    def save_session(self, host, ua, fp, cf_clearance=None, cf_bm=None, cf_ttl=1800):
        now = int(time.time())
        expires = now + cf_ttl if cf_clearance else None
        fp_json = json.dumps(fp, sort_keys=True)
        with sqlite3.connect(self.path) as c:
            c.execute("""
                INSERT INTO sessions
                  (host, user_agent, fingerprint_json, cf_clearance,
                   cf_clearance_expires, cf_bm, session_created,
                   last_used, last_challenge, challenge_count, success_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
                ON CONFLICT(host) DO UPDATE SET
                  user_agent=excluded.user_agent,
                  fingerprint_json=excluded.fingerprint_json,
                  cf_clearance=COALESCE(excluded.cf_clearance, cf_clearance),
                  cf_clearance_expires=COALESCE(excluded.cf_clearance_expires, cf_clearance_expires),
                  cf_bm=COALESCE(excluded.cf_bm, cf_bm),
                  last_used=excluded.last_used,
                  last_challenge=excluded.last_challenge,
                  challenge_count=challenge_count+1
            """, (host, ua, fp_json, cf_clearance, expires, cf_bm, now, now, now))

    def save_cookies(self, host, cookies):
        with sqlite3.connect(self.path) as c:
            c.execute("DELETE FROM cookies WHERE host=?", (host,))
            for ck in cookies:
                c.execute("""
                    INSERT INTO cookies (host, name, value, domain, path,
                      expires, http_only, secure, same_site)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (host, ck.get("name"), ck.get("value"),
                      ck.get("domain"), ck.get("path", "/"),
                      ck.get("expires", -1),
                      int(ck.get("httpOnly", False)),
                      int(ck.get("secure", False)),
                      ck.get("sameSite", "Lax")))

    def get_cookies(self, host):
        with sqlite3.connect(self.path) as c:
            rows = c.execute(
                "SELECT name, value, domain, path, expires, "
                "http_only, secure, same_site FROM cookies WHERE host=?",
                (host,),
            ).fetchall()
        return [{
            "name": r[0], "value": r[1], "domain": r[2], "path": r[3],
            "expires": r[4], "httpOnly": bool(r[5]),
            "secure": bool(r[6]), "sameSite": r[7],
        } for r in rows]

    def is_valid(self, host):
        s = self.get_session(host)
        if not s:
            return False, 0
        now = int(time.time())
        if s.get("cf_clearance") and s.get("cf_clearance_expires"):
            rem = s["cf_clearance_expires"] - now
            if rem > 300:
                return True, rem
        if s.get("cf_bm"):
            last = s.get("last_challenge", 0) or s.get("last_used", 0)
            if now - last < 1800:
                return True, 1800 - (now - last)
        return False, 0

    def record_success(self, host):
        with sqlite3.connect(self.path) as c:
            c.execute(
                "UPDATE sessions SET success_count=success_count+1, "
                "last_used=? WHERE host=?",
                (int(time.time()), host),
            )


# ===================================================================
# CDP client + tab manager
# ===================================================================
class CDPClient:
    """Single-target CDP connection."""

    def __init__(self, target_url: str | None = None):
        self.ws = None
        self.id = 0
        self._fut = {}
        self.events: asyncio.Queue = asyncio.Queue()
        self._reader = None
        self.closed = False
        self.target_id: str | None = None
        self.target_url = target_url
        self.target_type: str | None = None

    async def connect_to(self, ws_url: str):
        self.ws = await websockets.connect(
            ws_url, max_size=2**28, ping_interval=None, ping_timeout=None,
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


# ===================================================================
# Chrome lifecycle
# ===================================================================
def cleanup_chrome():
    for proc in ["chrome_cfsel", "chrome-linux/chrome", "proot-distro.*ubuntu"]:
        subprocess.run(["pkill", "-9", "-f", proc],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)


def launch_chrome(user_agent: str, profile_name: str = "default"):
    cleanup_chrome()
    profile_remote = f"/root/cf_selenium_profile_{profile_name}"
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
        "--window-size=1080,2400 "
        f"--user-data-dir={profile_remote} "
        f"--user-agent=\"{user_agent}\" "
        f"--remote-debugging-port={CDP_PORT} "
        "--remote-allow-origins=*"
    )
    launcher = (
        "#!/bin/bash\n"
        f"{CHROME} {flags} >{CHROME_LOG} 2>&1 &\n"
        "CHROME_PID=$!\n"
        f"echo $CHROME_PID > {CHROME_PID_FILE}\n"
        "wait $CHROME_PID\n"
    )
    subprocess.run(
        [PROOT, "login", "ubuntu", "--", "bash", "-c",
         f"cat > /root/launch_cfsel.sh <<'__HERMES_EOF__'\n{launcher}\n__HERMES_EOF__\n"
         f"chmod +x /root/launch_cfsel.sh"],
        capture_output=True, timeout=10,
    )
    subprocess.Popen(
        [PROOT, "login", "ubuntu", "--", "bash", "/root/launch_cfsel.sh"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, stdin=subprocess.DEVNULL,
    )
    for i in range(30):
        try:
            with urllib.request.urlopen(f"{DEBUG_URL}/json/version", timeout=1) as r:
                v = json.loads(r.read())
                log.info("chrome up: %s", v.get("Browser", "?"))
                return v
        except Exception:
            time.sleep(1)
    raise RuntimeError("Chrome failed to start in 30s")


def list_targets(retries=10):
    """Return list of CDP targets (pages, iframes, etc)."""
    for i in range(retries):
        try:
            with urllib.request.urlopen(f"{DEBUG_URL}/json/list", timeout=2) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(1)
    return []


def create_target(url: str = "about:blank"):
    """Create a new target via HTTP endpoint (POST required)."""
    # /json/new is the official Chrome DevTools endpoint
    # Accepts both GET (some versions) and POST. Try POST first.
    try:
        req = urllib.request.Request(
            f"{DEBUG_URL}/json/new?{urllib.parse.quote(url, safe='')}",
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 405:
            # try GET
            with urllib.request.urlopen(
                f"{DEBUG_URL}/json/new?{urllib.parse.quote(url, safe='')}", timeout=5
            ) as r:
                return json.loads(r.read())
        raise


# ===================================================================
# Humanized actions
# ===================================================================
def bezier_points(p0, p1, p2, p3, n=20):
    pts = []
    for i in range(n + 1):
        t = i / n
        x = ((1-t)**3)*p0[0] + 3*((1-t)**2)*t*p1[0] + 3*(1-t)*(t**2)*p2[0] + (t**3)*p3[0]
        y = ((1-t)**3)*p0[1] + 3*((1-t)**2)*t*p1[1] + 3*(1-t)*(t**2)*p2[1] + (t**3)*p3[1]
        pts.append((x, y))
    return pts


async def human_mouse_move(cdp, start, end):
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
            await cdp.cmd("Input.dispatchMouseEvent", {
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


async def human_click(cdp, x, y):
    await human_mouse_move(cdp, (random.uniform(100, 1200), random.uniform(100, 600)),
                            (x - random.uniform(-3, 3), y - random.uniform(-3, 3)))
    await asyncio.sleep(random.uniform(0.08, 0.18))
    await cdp.cmd("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x, "y": y,
        "button": "left", "buttons": 1, "clickCount": 1,
    }, timeout=3)
    await asyncio.sleep(random.uniform(0.08, 0.18))
    await cdp.cmd("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y,
        "button": "left", "buttons": 0, "clickCount": 1,
    }, timeout=3)


async def human_type(cdp, text):
    for ch in text:
        delay = random.gauss(0.12, 0.04)
        if ch == " ":
            delay *= 0.5
        elif ch in ".,!?":
            delay *= 1.6
        if random.random() < 0.02:
            delay += random.uniform(0.3, 0.7)
        try:
            await cdp.cmd("Input.insertText", {"text": ch}, timeout=3)
        except Exception:
            await cdp.cmd("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch}, timeout=3)
            await cdp.cmd("Input.dispatchKeyEvent", {"type": "keyUp", "text": ch}, timeout=3)
        await asyncio.sleep(max(0.04, delay))


# ===================================================================
# Recorder — records actions to JSON for later replay
# ===================================================================
class Recorder:
    """Records browser actions to a JSON file. Replay later.

    Captures:
      - navigation (URL, final title, page state)
      - clicks (selector, position, target info)
      - type inputs (selector, text)
      - hovers, scrolls
      - challenges detected + how they were solved
      - timing info
    """

    def __init__(self):
        self.actions: list[dict] = []
        self.active = False
        self.start_time = None
        self._browser_ref = None

    def start(self):
        self.actions = []
        self.active = True
        self.start_time = time.time()
        log.info("recorder: started")

    def stop(self):
        self.active = False
        log.info("recorder: stopped, %d actions", len(self.actions))

    def attach(self, browser: "Browser"):
        """Attach to a browser instance to auto-record its actions."""
        self._browser_ref = browser
        # Monkey-patch key methods
        orig_get = browser.get
        orig_find = browser.find_element
        orig_find_all = browser.find_elements
        self_active = self.active

        def wrapped_get(url, wait_for=None):
            if self.active:
                self._record({"type": "navigate", "url": url,
                              "wait_for": wait_for, "t": time.time() - self.start_time})
            return orig_get(url, wait_for)
        browser.get = wrapped_get

        def wrapped_find(selector):
            el = orig_find(selector)
            return _RecordedElement(el, self)
        browser.find_element = wrapped_find

    def _record(self, action: dict):
        if self.active:
            self.actions.append(action)
            log.debug("recorded: %s", action.get("type"))

    def save(self, path: str | Path | None = None) -> Path:
        if path is None:
            path = RECORDINGS_DIR / f"rec_{int(time.time())}.json"
        path = Path(os.path.expanduser(str(path)))
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": "1.0",
            "created": datetime.now().isoformat(),
            "duration": time.time() - (self.start_time or time.time()),
            "actions": self.actions,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        log.info("recording saved: %s (%d actions)", path, len(self.actions))
        return path

    @staticmethod
    def replay(path: str | Path, headless: bool = True, profile: str = "replay",
               browser_class: type | None = None) -> "Browser":
        """Replay a recording into a fresh browser session."""
        path = Path(os.path.expanduser(str(path)))
        data = json.loads(path.read_text())
        actions = data.get("actions", [])
        log.info("replaying %d actions from %s", len(actions), path)
        browser_class = browser_class or Browser
        b = browser_class(profile=profile)
        b.recorder = None  # don't double-record
        for a in actions:
            t = a.get("type")
            if t == "navigate":
                log.info("replay: navigate → %s", a["url"])
                b.get(a["url"])
            elif t == "click":
                log.info("replay: click %s", a.get("selector"))
                el = b.find_element(a["selector"])
                el.click()
            elif t == "type":
                log.info("replay: type %s into %s",
                         a.get("text", "")[:20], a.get("selector"))
                el = b.find_element(a["selector"])
                el.type(a.get("text", ""))
            elif t == "wait":
                log.info("replay: wait %.1fs", a.get("seconds", 1))
                time.sleep(a.get("seconds", 1))
            else:
                log.warning("replay: unknown action type %s", t)
        return b


class _RecordedElement:
    """Wrapper that records actions before delegating to real WebElement.

    For sync usage (selenium-like), awaits async ops via browser's loop.
    """

    def __init__(self, real: "WebElement", recorder: Recorder):
        self._real = real
        self._rec = recorder

    def _record(self, action: dict):
        if self._rec.active:
            self._rec._record(action)

    def _sync(self, coro):
        """Run async coro synchronously via browser's loop."""
        return self._real._b._run(coro)

    def click(self, human=True):
        self._record({
            "type": "click",
            "selector": self._real._selector,
            "t": time.time() - self._rec.start_time,
        })
        return self._sync(self._real.click(human=human))

    def type(self, text, human=True):
        self._record({
            "type": "type",
            "selector": self._real._selector,
            "text": text,
            "t": time.time() - self._rec.start_time,
        })
        return self._sync(self._real.type(text, human=human))

    def hover(self):
        self._record({
            "type": "hover",
            "selector": self._real._selector,
            "t": time.time() - self._rec.start_time,
        })
        return self._sync(self._real.hover())

    def __getattr__(self, name):
        return getattr(self._real, name)


# ===================================================================
# WebElement (Selenium-like)
# ===================================================================
class WebElement:
    def __init__(self, browser: "Browser", selector: str, index: int = 0):
        self._b = browser
        self._selector = selector
        self._index = index

    async def _resolve(self) -> dict | None:
        js = f"""
        (() => {{
          const els = document.querySelectorAll({json.dumps(self._selector)});
          if ({self._index} >= els.length) return null;
          const e = els[{self._index}];
          const r = e.getBoundingClientRect();
          if (r.width < 0 || r.height < 0) return null;
          return JSON.stringify({{
            x: r.x, y: r.y, w: r.width, h: r.height,
            text: (e.innerText || e.value || '').slice(0, 1000),
            value: e.value || '',
            html: e.outerHTML.slice(0, 5000),
            tag: e.tagName.toLowerCase(),
            type: e.type || '',
            name: e.name || '',
            id: e.id || '',
            placeholder: e.placeholder || '',
            visible: r.width > 0 && r.height > 0 && r.x > -1000 && r.y > -1000,
            cx: r.x + r.width/2,
            cy: r.y + r.height/2,
          }});
        }})()
        """
        raw = await self._b._cdp.js(js, timeout=8)
        if not raw or raw == "null":
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def _wait_present(self, timeout=10):
        t0 = time.time()
        while time.time() - t0 < timeout:
            info = await self._resolve()
            if info and info.get("visible"):
                return info
            await asyncio.sleep(0.3)
        return None

    @property
    async def text(self) -> str:
        info = await self._resolve()
        return info["text"] if info else ""

    @property
    async def value(self) -> str:
        info = await self._resolve()
        return info["value"] if info else ""

    @property
    async def html(self) -> str:
        info = await self._resolve()
        return info["html"] if info else ""

    @property
    async def is_displayed(self) -> bool:
        info = await self._resolve()
        return bool(info and info.get("visible"))

    async def get_attribute(self, name: str):
        js = f"""
        (() => {{
          const e = document.querySelectorAll({json.dumps(self._selector)})[{self._index}];
          return e ? e.getAttribute({json.dumps(name)}) : null;
        }})()
        """
        return await self._b._cdp.js(js, timeout=5)

    async def click(self, human=True):
        info = await self._wait_present()
        if not info:
            raise RuntimeError(f"element not found: {self._selector}[{self._index}]")
        cx, cy = info["cx"], info["cy"]
        log.info("click %s at (%.0f, %.0f)", self._selector, cx, cy)
        if human:
            await human_click(self._b._cdp, cx, cy)
        else:
            await self._b._cdp.cmd("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": cx, "y": cy,
                "button": "left", "buttons": 1, "clickCount": 1,
            }, timeout=3)
            await self._b._cdp.cmd("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": cx, "y": cy,
                "button": "left", "buttons": 0, "clickCount": 1,
            }, timeout=3)

    async def type(self, text: str, human=True):
        await self._b._cdp.js(f"""
            (() => {{
              const e = document.querySelectorAll({json.dumps(self._selector)})[{self._index}];
              if (e) {{ e.focus(); e.click(); }}
            }})()
        """, timeout=5)
        await asyncio.sleep(random.uniform(0.1, 0.25))
        if human:
            # human_type uses Input.insertText which is invisible to React onChange
            # for controlled inputs. Use char-by-char with React-compatible setter.
            for ch in text:
                await self._b._cdp.js(f"""
                    (() => {{
                      const e = document.querySelectorAll({json.dumps(self._selector)})[{self._index}];
                      if (!e) return false;
                      const desc = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                      ) || Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                      );
                      if (desc && desc.set) {{
                        desc.set.call(e, e.value + {json.dumps(ch)});
                        e.dispatchEvent(new Event('input', {{bubbles: true}}));
                        e.dispatchEvent(new Event('change', {{bubbles: true}}));
                      }}
                      return true;
                    }})()
                """, timeout=5)
                await asyncio.sleep(random.uniform(0.04, 0.12))
        else:
            # Non-human: use native setter + Input.insertText for max compat
            await self._b._cdp.js(f"""
                (() => {{
                  const e = document.querySelectorAll({json.dumps(self._selector)})[{self._index}];
                  if (!e) return false;
                  const proto = e.tagName === 'TEXTAREA'
                    ? window.HTMLTextAreaElement.prototype
                    : window.HTMLInputElement.prototype;
                  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                  if (desc && desc.set) {{
                    desc.set.call(e, {json.dumps(text)});
                    e.dispatchEvent(new Event('input', {{bubbles: true}}));
                    e.dispatchEvent(new Event('change', {{bubbles: true}}));
                  }}
                  return true;
                }})()
            """, timeout=5)

    async def hover(self):
        info = await self._wait_present()
        if not info:
            return
        await human_mouse_move(
            self._b._cdp,
            (random.uniform(100, 1200), random.uniform(100, 600)),
            (info["cx"], info["cy"]),
        )

    async def scroll_into_view(self):
        await self._b._cdp.js(f"""
            (() => {{
              const e = document.querySelectorAll({json.dumps(self._selector)})[{self._index}];
              if (e) e.scrollIntoView({{behavior: 'smooth', block: 'center'}});
            }})()
        """, timeout=5)
        await asyncio.sleep(random.uniform(0.3, 0.7))

    async def clear(self):
        await self._b._cdp.js(f"""
            (() => {{
              const e = document.querySelectorAll({json.dumps(self._selector)})[{self._index}];
              if (!e) return false;
              const proto = e.tagName === 'TEXTAREA'
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
              const desc = Object.getOwnPropertyDescriptor(proto, 'value');
              if (desc && desc.set) {{
                desc.set.call(e, '');
                e.dispatchEvent(new Event('input', {{bubbles: true}}));
                e.dispatchEvent(new Event('change', {{bubbles: true}}));
              }}
              return true;
            }})()
        """, timeout=5)


# ===================================================================
# Tab — represents one browser tab
# ===================================================================
class Tab:
    """One tab in the browser. Has its own CDPClient, target info."""

    def __init__(self, browser: "Browser", target_id: str, ws_url: str,
                 target_url: str = "about:blank"):
        self._browser = browser
        self.target_id = target_id
        self.ws_url = ws_url
        self.url = target_url
        self.title = ""
        self._cdp: CDPClient | None = None

    @property
    def cdp(self) -> CDPClient:
        return self._cdp

    async def _attach(self):
        if self._cdp is None or not self._cdp.ws or self._cdp.ws.state is not State.OPEN:
            self._cdp = CDPClient(self.ws_url)
            await self._cdp.connect_to(self.ws_url)
            for d in ["Page.enable", "Runtime.enable", "Network.enable", "DOM.enable"]:
                try:
                    await self._cdp.cmd(d, timeout=15)
                except Exception:
                    pass
            try:
                await self._cdp.cmd("Page.addScriptToEvaluateOnNewDocument", {
                    "source": self._browser._current_fp().to_stealth_js(),
                })
            except Exception:
                pass

    async def close(self):
        if self._cdp:
            await self._cdp.close()
        # close target via browser
        try:
            with urllib.request.urlopen(
                f"{DEBUG_URL}/json/close/{self.target_id}", timeout=3
            ) as r:
                pass
        except Exception:
            pass

    async def navigate(self, url: str):
        await self._attach()
        await self._cdp.cmd("Page.navigate", {"url": url})
        # drain events
        for _ in range(5):
            try:
                await asyncio.wait_for(self._cdp.events.get(), timeout=0.3)
            except asyncio.TimeoutError:
                pass
        await asyncio.sleep(random.uniform(0.5, 1.0))
        self.url = url
        # refresh title
        try:
            self.title = await self._cdp.js("document.title", timeout=5)
        except Exception:
            pass

    async def get_html(self) -> str:
        await self._attach()
        return await self._cdp.js("document.documentElement.outerHTML", timeout=10)


# ===================================================================
# Browser (Selenium-like + multi-tab + AI)
# ===================================================================
class Browser:
    def __init__(self, headless=True, profile="default", host=None,
                 auto_solve=True, timeout=90):
        self.headless = headless
        self.profile_name = profile
        self.profile_dir = PROFILE_DIR / profile
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.auto_solve = auto_solve
        self.timeout = timeout
        self._db = SessionDB()
        self._cdp: CDPClient | None = None
        self._fp: Fingerprint | None = None
        self._ua: str | None = None
        self._fp_host: str | None = host
        self._tabs: dict[str, Tab] = {}
        self._active_tab: str | None = None
        self._recorder: Recorder | None = None
        self._event_log: list[dict] = []
        try:
            self._loop = asyncio.get_event_loop()
            if self._loop.is_closed():
                raise RuntimeError
        except Exception:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

    def _current_fp(self) -> Fingerprint:
        if self._fp is None:
            self._fp = Fingerprint(self._fp_host or "default.local")
        return self._fp

    @property
    def recorder(self) -> Recorder | None:
        return self._recorder

    @recorder.setter
    def recorder(self, value):
        self._recorder = value

    @property
    def tabs(self) -> list[Tab]:
        return [self._tabs[tid] for tid in self._tabs]

    @property
    def active_tab(self) -> Tab | None:
        return self._tabs.get(self._active_tab) if self._active_tab else None

    # ---------- public API ----------
    @property
    def title(self) -> str:
        if not self._cdp:
            return ""
        return self._run(self._cdp.js("document.title", timeout=5))

    @property
    def url(self) -> str:
        if not self._cdp:
            return ""
        return self._run(self._cdp.js("location.href", timeout=5))

    @property
    def html(self) -> str:
        if not self._cdp:
            return ""
        return self._run(self._cdp.js("document.documentElement.outerHTML", timeout=10))

    @property
    def cookies(self) -> dict:
        if not self._cdp:
            return {}
        try:
            r = self._run(self._cdp.cmd("Network.getAllCookies", {}, timeout=8))
            return {ck["name"]: ck["value"] for ck in r.get("result", {}).get("cookies", [])}
        except Exception:
            return {}

    def get(self, url: str, wait_for: str | None = None) -> "Browser":
        if self._recorder and self._recorder.active:
            self._recorder._record({
                "type": "navigate", "url": url, "wait_for": wait_for,
                "t": time.time() - (self._recorder.start_time or time.time()),
            })
        return self._run(self._get(url, wait_for))

    def find_element(self, selector: str) -> WebElement:
        return WebElement(self, selector, 0)

    def find_elements(self, selector: str) -> list[WebElement]:
        return _ElementList(self, selector)

    def find_by_text(self, text: str, tag: str = "*") -> WebElement:
        """AI-friendly: find first element containing text."""
        js = f"""
        (() => {{
          const els = document.querySelectorAll({json.dumps(tag)});
          for (let i = 0; i < els.length; i++) {{
            const t = (els[i].innerText || els[i].textContent || '').trim();
            if (t.includes({json.dumps(text)})) {{
              const r = els[i].getBoundingClientRect();
              return JSON.stringify({{i, tag: els[i].tagName.toLowerCase(),
                text: t.slice(0, 200),
                cx: r.x + r.width/2, cy: r.y + r.height/2,
                w: r.width, h: r.height,
                id: els[i].id, cls: els[i].className,
                selector: els[i].id ? '#'+els[i].id :
                         (els[i].className ? '.'+els[i].className.split(' ').join('.') : els[i].tagName.toLowerCase())
              }});
            }}
          }}
          return null;
        }})()
        """
        raw = self._run(self._cdp.js(js, timeout=5))
        if not raw or raw == "null":
            raise RuntimeError(f"text not found: {text!r}")
        return _TextElement(self, json.loads(raw), text)

    def find_by_role(self, role: str, name: str | None = None) -> WebElement:
        """AI-friendly: find by ARIA role."""
        sel = f"[role='{role}']"
        if name:
            sel += f"[aria-label*='{name}']"
        return WebElement(self, sel, 0)

    def execute_script(self, js: str):
        if not self._cdp:
            return ""
        return self._run(self._cdp.js(js, timeout=15))

    def execute(self, js: str):
        """Alias for execute_script."""
        return self.execute_script(js)

    def screenshot(self, path: str | Path | None = None, full_page=False) -> Path:
        """Take screenshot. Default: screenshots/<host>_<timestamp>.png"""
        if not self._cdp:
            raise RuntimeError("no active page")
        if path is None:
            path = SCREENSHOTS_DIR / f"shot_{int(time.time()*1000)}.png"
        path = Path(os.path.expanduser(str(path)))
        path.parent.mkdir(parents=True, exist_ok=True)
        params = {"format": "png"}
        if full_page:
            params["captureBeyondViewport"] = True
        r = self._run(self._cdp.cmd("Page.captureScreenshot", params, timeout=15))
        data = r.get("result", {}).get("data", "")
        if not data:
            raise RuntimeError("screenshot failed: no data")
        path.write_bytes(base64.b64decode(data))
        log.info("screenshot: %s (%d bytes)", path, path.stat().st_size)
        return path

    def snapshot(self, include_html=False) -> dict:
        """Rich page snapshot for AI/LLM agent.

        Returns dict with:
          - url, title
          - summary (one-line description)
          - actions (suggested actions based on page state)
          - forms (visible forms with fields)
          - buttons (visible buttons with text)
          - links (top 20 visible links)
          - inputs (top 30 visible inputs)
          - headings (h1-h3)
          - text_preview (first 500 chars of visible text)
          - challenge (if any CF challenge present)
          - cookies_count
        """
        js = """
        (() => {
          const visible = (e) => {
            const r = e.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && r.x > -100 && r.y > -100
              && r.x < window.innerWidth + 100 && r.y < window.innerHeight + 100;
          };
          const txt = (e) => (e.innerText || e.textContent || '').trim().slice(0, 200);

          // forms
          const forms = [...document.querySelectorAll('form')].filter(visible).map(f => {
            const fields = [...f.querySelectorAll('input, textarea, select')].map(el => ({
              tag: el.tagName.toLowerCase(),
              type: el.type || '',
              name: el.name || '',
              id: el.id || '',
              placeholder: el.placeholder || '',
              required: el.required || false,
            }));
            return {
              action: f.action || '',
              method: f.method || 'get',
              id: f.id || '',
              fields,
            };
          });

          // buttons
          const buttons = [...document.querySelectorAll('button, input[type=submit], input[type=button]')]
            .filter(visible).map(b => ({
              tag: b.tagName.toLowerCase(),
              type: b.type || '',
              text: txt(b),
              id: b.id || '',
              disabled: b.disabled || false,
            })).slice(0, 20);

          // links
          const links = [...document.querySelectorAll('a[href]')]
            .filter(visible).map(a => ({
              text: txt(a).slice(0, 80),
              href: a.href,
            })).slice(0, 20);

          // inputs
          const inputs = [...document.querySelectorAll('input, textarea')]
            .filter(visible).map(i => ({
              tag: i.tagName.toLowerCase(),
              type: i.type || 'text',
              name: i.name || '',
              id: i.id || '',
              placeholder: i.placeholder || '',
              value: (i.value || '').slice(0, 100),
              required: i.required || false,
            })).slice(0, 30);

          // headings
          const headings = [...document.querySelectorAll('h1, h2, h3')]
            .filter(visible).map(h => ({
              level: parseInt(h.tagName[1]),
              text: txt(h),
            })).slice(0, 20);

          // body preview
          const bodyPreview = (document.body?.innerText || '').slice(0, 500);

          return JSON.stringify({
            url: location.href,
            title: document.title,
            forms, buttons, links, inputs, headings,
            bodyPreview,
            hasTurnstile: !!document.querySelector('[class*="turnstile"], iframe[src*="turnstile"]'),
            hasChallenge: !!document.querySelector('#challenge-form, .cf-challenge, [class*="challenge"]'),
            challengeTitle: (document.title || '').toLowerCase().includes('just a moment') ? 'managed' : 'none',
            viewport: {w: window.innerWidth, h: window.innerHeight},
          });
        })()
        """
        raw = self._run(self._cdp.js(js, timeout=10))
        try:
            d = json.loads(raw)
        except Exception:
            d = {}

        # build summary
        title = d.get("title", "")
        url = d.get("url", "")
        body = d.get("bodyPreview", "").replace("\n", " ").strip()
        body_short = (body[:200] + "...") if len(body) > 200 else body
        challenge = d.get("challengeTitle", "none")
        if challenge != "none":
            summary = f"[CHALLENGE: {challenge}] {title} — {body_short}"
        else:
            summary = f"{title} ({url}) — {body_short}"

        # build suggested actions
        actions = []
        for f in d.get("forms", []):
            for field in f.get("fields", []):
                if field.get("type") not in ("hidden", "submit"):
                    sel = f"#{field['id']}" if field.get("id") else \
                          f"[name='{field['name']}']" if field.get("name") else \
                          f"input[type='{field.get('type', 'text')}']"
                    actions.append({
                        "kind": "fill",
                        "selector": sel,
                        "name": field.get("name") or field.get("id") or "",
                        "type": field.get("type", "text"),
                        "placeholder": field.get("placeholder", ""),
                    })
        for b in d.get("buttons", []):
            if not b.get("disabled") and b.get("text"):
                sel = f"#{b['id']}" if b.get("id") else f"button:contains('{b['text'][:20]}')"
                actions.append({"kind": "click_button", "text": b["text"], "selector": sel})
        for l in d.get("links", []):
            if l.get("text"):
                actions.append({"kind": "click_link", "text": l["text"][:50], "href": l["href"]})

        result = {
            "url": url,
            "title": title,
            "summary": summary,
            "actions": actions[:50],
            "forms": d.get("forms", []),
            "buttons": d.get("buttons", []),
            "links": d.get("links", []),
            "inputs": d.get("inputs", []),
            "headings": d.get("headings", []),
            "text_preview": body,
            "challenge": challenge,
            "cookies_count": len(self.cookies),
            "viewport": d.get("viewport", {}),
        }
        if include_html:
            result["html"] = self.html
        return result

    # ---------- multi-tab API ----------
    def new_tab(self, url: str = "about:blank") -> Tab:
        target = create_target(url)
        tid = target["id"]
        tab = Tab(self, tid, target["webSocketDebuggerUrl"], url)
        self._tabs[tid] = tab
        self._active_tab = tid
        log.info("new tab: %s (%s)", tid[:8], url[:50])
        return tab

    def switch_tab(self, index_or_id) -> Tab:
        if isinstance(index_or_id, int):
            tabs = self.tabs
            if 0 <= index_or_id < len(tabs):
                tid = tabs[index_or_id].target_id
            else:
                raise IndexError(f"tab index {index_or_id} out of range")
        else:
            tid = index_or_id
        self._active_tab = tid
        tab = self._tabs[tid]
        # rebind _cdp to this tab
        self._cdp = tab._cdp
        log.info("switched to tab: %s", tid[:8])
        return tab

    def close_tab(self, index_or_id=None):
        if index_or_id is None:
            tid = self._active_tab
        elif isinstance(index_or_id, int):
            tabs = self.tabs
            tid = tabs[index_or_id].target_id
        else:
            tid = index_or_id
        if tid and tid in self._tabs:
            tab = self._tabs.pop(tid)
            self._run(tab.close())
            log.info("closed tab: %s", tid[:8])
        # pick another active tab
        if self._tabs:
            self._active_tab = list(self._tabs.keys())[0]
            self._cdp = self._tabs[self._active_tab]._cdp
        else:
            self._active_tab = None
            self._cdp = None

    def save_session(self, path: str | Path | None = None):
        path = Path(path) if path else SESSION_DIR / f"{self._fp_host or 'default'}.json"
        cookies = self.cookies
        path.write_text(json.dumps(cookies, indent=2))
        log.info("saved %d cookies to %s", len(cookies), path)

    def load_session(self, path: str | Path):
        cookies = json.loads(Path(path).read_text())
        self._run(self._load_cookies(cookies))

    def start_recording(self) -> Recorder:
        if not self._recorder:
            self._recorder = Recorder()
        self._recorder.start()
        return self._recorder

    def stop_recording(self):
        if self._recorder:
            self._recorder.stop()

    def save_recording(self, path: str | Path | None = None) -> Path:
        if not self._recorder:
            raise RuntimeError("no recorder")
        return self._recorder.save(path)

    def quit(self):
        # close all tabs
        for tid in list(self._tabs.keys()):
            try:
                self._run(self._tabs[tid].close())
            except Exception:
                pass
        self._tabs.clear()
        if self._cdp:
            try:
                self._run(self._cdp.close())
            except Exception:
                pass
        cleanup_chrome()
        log.info("browser quit")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.quit()

    # ---------- async runtime ----------
    def _run(self, coro):
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        return self._loop.run_until_complete(coro)

    async def _ensure_chrome(self, host: str):
        if self._cdp is None or not self._cdp.ws or self._cdp.ws.state is not State.OPEN:
            self._fp = Fingerprint(host)
            self._ua = self._fp.user_agent()
            launch_chrome(self._ua, profile_name=self.profile_name)
            # find or create first page
            targets = list_targets()
            page = next((t for t in targets if t.get("type") == "page"), None)
            if not page:
                page = create_target("about:blank")
            self._cdp = CDPClient(page.get("url", "about:blank"))
            await self._cdp.connect_to(page["webSocketDebuggerUrl"])
            self._cdp.target_id = page["id"]
            self._cdp.target_type = page.get("type")
            # register as first tab
            tab = Tab(self, page["id"], page["webSocketDebuggerUrl"], page.get("url", ""))
            tab._cdp = self._cdp
            self._tabs[page["id"]] = tab
            self._active_tab = page["id"]
            # enable domains
            for d in ["Page.enable", "Runtime.enable", "Network.enable", "DOM.enable"]:
                for r in range(3):
                    try:
                        await self._cdp.cmd(d, timeout=20)
                        break
                    except Exception:
                        if r == 2:
                            log.warning("%s enable failed", d)
                        await asyncio.sleep(1)
            # viewport
            try:
                await self._cdp.cmd("Emulation.setDeviceMetricsOverride", {
                    "width": self._fp.data["screen_width"],
                    "height": self._fp.data["screen_height"],
                    "deviceScaleFactor": 1, "mobile": False,
                })
            except Exception:
                pass
            # stealth
            try:
                await self._cdp.cmd("Page.addScriptToEvaluateOnNewDocument", {
                    "source": self._fp.to_stealth_js(),
                })
            except Exception:
                pass

    async def _get(self, url: str, wait_for: str | None):
        host = urllib.parse.urlparse(url).netloc
        self._fp_host = host
        await self._ensure_chrome(host)

        for _ in range(random.randint(2, 4)):
            await human_mouse_move(
                self._cdp,
                (random.uniform(100, 1200), random.uniform(100, 600)),
                (random.uniform(100, 1200), random.uniform(100, 600)),
            )
            await asyncio.sleep(random.uniform(0.2, 0.5))

        await self._cdp.cmd("Page.navigate", {"url": url})
        for _ in range(5):
            try:
                await asyncio.wait_for(self._cdp.events.get(), timeout=0.3)
            except asyncio.TimeoutError:
                pass
        await asyncio.sleep(random.uniform(0.8, 1.5))

        if self.auto_solve:
            await self._solve_if_needed(host)
        else:
            await asyncio.sleep(2)

        if wait_for:
            js = f"""
            (() => new Promise(r => {{
              const s = Date.now();
              const c = () => {{
                if (document.querySelector({json.dumps(wait_for)})) return r('found');
                if (Date.now() - s > 30000) return r('timeout');
                setTimeout(c, 200);
              }}; c();
            }}))()
            """
            await self._cdp.js(js, timeout=35)

    async def _solve_if_needed(self, host: str):
        t0 = time.time()
        last_check = 0
        last_widget_click = 0
        consecutive_no_challenge = 0
        while time.time() - t0 < self.timeout:
            now = time.time()
            try:
                while True:
                    self._cdp.events.get_nowait()
            except Exception:
                pass
            if now - last_check >= 3:
                last_check = now
                info = await self._detect_challenge()
                ctype = info.get("challengeType", "none")
                is_ch = info.get("isChallenge", False)
                log.info("t=%ds %s type=%s title=%r",
                         int(now - t0), "🛡" if is_ch else "✓", ctype,
                         info.get("title", "")[:30])
                if is_ch and ctype in ("turnstile", "managed", "js"):
                    if now - last_widget_click >= 8:
                        last_widget_click = now
                        await self._click_turnstile(info)
                else:
                    consecutive_no_challenge += 1
                    if consecutive_no_challenge >= 2:
                        log.info("page stable, challenge phase done")
                        break
                try:
                    cr = await self._cdp.cmd("Network.getAllCookies", {}, timeout=8)
                    cookies = cr.get("result", {}).get("cookies", [])
                    jar = {ck["name"]: ck["value"] for ck in cookies}
                    if any(k in jar for k in SOLVED_COOKIES):
                        cf_clear = jar.get("cf_clearance")
                        cf_bm = jar.get("__cf_bm") or jar.get("cf_bm")
                        cf_ttl = 1800
                        for ck in cookies:
                            if ck.get("name") in ("cf_clearance",):
                                exp = ck.get("expires", -1)
                                if exp > 0:
                                    cf_ttl = max(60, int(exp - now))
                        self._db.save_session(host, self._ua, self._fp.get(),
                                              cf_clearance=cf_clear, cf_bm=cf_bm,
                                              cf_ttl=cf_ttl)
                        self._db.save_cookies(host, cookies)
                        self._db.record_success(host)
                        log.info("✅ session saved (TTL=%ds)", cf_ttl)
                        return
                except Exception as e:
                    log.debug("cookie check: %s", e)
            await asyncio.sleep(random.uniform(0.3, 0.7))

    async def _detect_challenge(self) -> dict:
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
            if (r.width < 5) return null;
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
        raw = await self._cdp.js(script, timeout=8)
        try:
            info = json.loads(raw)
        except Exception:
            info = {"title": "", "bodyLen": 0}
        title = (info.get("title") or "").lower()
        body = (info.get("bodyHead") or "").lower()
        info["isChallenge"] = (
            info.get("hasTurnstile") or info.get("hasCfChallenge")
            or info.get("hasHcaptcha") or info.get("hasRecaptcha")
            or info.get("chlFrame") is not None
            or any(mk in title + " " + body for mk in [
                "just a moment", "checking your browser",
                "verifying you are human", "attention required",
            ])
        )
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

    async def _click_turnstile(self, info):
        box = info.get("turnstileBox") or info.get("chlFrame")
        if not box or box.get("w", 0) < 10:
            ifr = await self._cdp.js("""
                (() => {
                  const ifs = document.querySelectorAll('iframe');
                  for (const f of ifs) {
                    const r = f.getBoundingClientRect();
                    if (r.width > 50 && r.width < 800 && r.height > 30 && r.height < 300
                        && r.x > 50 && r.y > 50 && r.x < 1300 && r.y < 700) {
                      return JSON.stringify({x: r.x, y: r.y, w: r.width, h: r.height});
                    }
                  }
                  return null;
                })()
            """)
            if not ifr or ifr == "null":
                return
            try:
                box = json.loads(ifr)
            except Exception:
                return
        cx = box["x"] + box["w"] / 2
        cy = box["y"] + box["h"] / 2
        log.info("clicking turnstile at (%.0f, %.0f)", cx, cy)
        await human_click(self._cdp, cx, cy)

    async def _load_cookies(self, cookies: dict):
        for name, value in cookies.items():
            try:
                await self._cdp.cmd("Network.setCookie", {
                    "name": name, "value": value,
                    "domain": ".%s" % self._fp_host,
                    "path": "/",
                }, timeout=3)
            except Exception:
                pass


# ===================================================================
# _ElementList + _TextElement helpers
# ===================================================================
class _ElementList:
    def __init__(self, browser: "Browser", selector: str):
        self._b = browser
        self._selector = selector

    def __len__(self) -> int:
        if not self._b._cdp:
            return 0
        js = f"document.querySelectorAll({json.dumps(self._selector)}).length"
        n = self._b.execute_script(js)
        try:
            return int(n)
        except Exception:
            return 0

    def __getitem__(self, i: int) -> WebElement:
        return WebElement(self._b, self._selector, i)

    def __iter__(self):
        n = len(self)
        for i in range(n):
            yield WebElement(self._b, self._selector, i)


class _TextElement:
    """Result of find_by_text — wraps found element with selector."""

    def __init__(self, browser: "Browser", info: dict, query_text: str):
        self._b = browser
        self._info = info
        self._query = query_text
        self._selector = info.get("selector", "")
        self._index = info.get("i", 0)

    @property
    def text(self) -> str:
        return self._info.get("text", "")

    @property
    def tag(self) -> str:
        return self._info.get("tag", "")

    @property
    def selector(self) -> str:
        return self._selector

    async def click(self, human=True):
        cx, cy = self._info["cx"], self._info["cy"]
        log.info("click '%s' at (%.0f, %.0f)", self._query[:30], cx, cy)
        if human:
            await human_click(self._b._cdp, cx, cy)
        else:
            await self._b._cdp.cmd("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": cx, "y": cy,
                "button": "left", "buttons": 1, "clickCount": 1,
            }, timeout=3)
            await self._b._cdp.cmd("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": cx, "y": cy,
                "button": "left", "buttons": 0, "clickCount": 1,
            }, timeout=3)

    async def hover(self):
        await human_mouse_move(
            self._b._cdp,
            (random.uniform(100, 1200), random.uniform(100, 600)),
            (self._info["cx"], self._info["cy"]),
        )

    async def type(self, text: str, human=True):
        await self.click(human=human)
        # Use React-compatible native value setter (Input.insertText invisible to React onChange)
        if human:
            for ch in text:
                await self._b._cdp.js(f"""
                    (() => {{
                      const e = document.querySelectorAll({json.dumps(self._selector)})[{self._index}];
                      if (!e) return false;
                      const proto = e.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                      if (desc && desc.set) {{
                        desc.set.call(e, e.value + {json.dumps(ch)});
                        e.dispatchEvent(new Event('input', {{bubbles: true}}));
                        e.dispatchEvent(new Event('change', {{bubbles: true}}));
                      }}
                      return true;
                    }})()
                """, timeout=5)
                await asyncio.sleep(random.uniform(0.04, 0.12))
        else:
            await self._b._cdp.js(f"""
                (() => {{
                  const e = document.querySelectorAll({json.dumps(self._selector)})[{self._index}];
                  if (!e) return false;
                  const proto = e.tagName === 'TEXTAREA'
                    ? window.HTMLTextAreaElement.prototype
                    : window.HTMLInputElement.prototype;
                  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                  if (desc && desc.set) {{
                    desc.set.call(e, {json.dumps(text)});
                    e.dispatchEvent(new Event('input', {{bubbles: true}}));
                    e.dispatchEvent(new Event('change', {{bubbles: true}}));
                  }}
                  return true;
                }})()
            """, timeout=5)


# ===================================================================
# Demo
# ===================================================================
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    url = sys.argv[1]
    with Browser() as b:
        b.get(url)
        print(f"title:   {b.title}")
        print(f"url:     {b.url}")
        print(f"cookies: {list(b.cookies.keys())}")
        snap = b.snapshot()
        print(f"summary: {snap['summary'][:100]}")
        print(f"actions: {len(snap['actions'])} suggested")
        for a in snap["actions"][:5]:
            print(f"  - {a}")
