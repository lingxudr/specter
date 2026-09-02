#!/usr/bin/env python3
"""Test routerku: check keys, provider status, and try a chat completion."""
import json, urllib.request, urllib.error, os, sys, re

BASE = "http://localhost:20130"

def get(url, key=None, timeout=10):
    req = urllib.request.Request(BASE + url)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)

def post(url, payload, key=None, timeout=30):
    req = urllib.request.Request(BASE + url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)

# 1. Find api keys from 9Router DB
key = None
try:
    import sqlite3
    db = sqlite3.connect(os.path.expanduser("~/.9router/db/data.sqlite"))
    rows = db.execute("SELECT key, name, isActive FROM apiKeys WHERE isActive=1").fetchall()
    print("=== API keys (9Router DB) ===")
    for k, name, act in rows:
        masked = k[:10] + "..." if len(k) > 14 else k
        print(f"  [{name}] {masked} active={act}")
        if not key:
            key = k
except Exception as e:
    print("DB error:", e)

if not key:
    # try config.yaml
    try:
        cfg = open(os.path.expanduser("~/.hermes/config.yaml")).read()
        m = re.search(r"api_key:\s*['\"]?(sk-[A-Za-z0-9_-]+)['\"]?", cfg)
        if m:
            key = m.group(1)
            print("key from config.yaml:", key[:12] + "...")
    except Exception as e:
        print("config err:", e)

print("\n=== /health ===")
print(get("/health")[1][:300])

print("\n=== /api/status (provider health) ===")
st, body = get("/api/status", key)
print("status:", st)
try:
    j = json.loads(body)
    provs = j.get("providers", j.get("providerStats", {}))
    if isinstance(provs, dict):
        for p, info in list(provs.items())[:20]:
            if isinstance(info, dict):
                state = info.get("circuit", info.get("state", "?"))
                print(f"  {p}: {state}")
    else:
        print(body[:800])
except Exception as e:
    print("parse err:", e, body[:400])

print("\n=== chat test model=Free-All ===")
st, body = post("/v1/chat/completions", {
    "model": "Free-All",
    "messages": [{"role": "user", "content": "balas: ok"}],
    "max_tokens": 30,
}, key)
print("status:", st)
try:
    j = json.loads(body)
    if "choices" in j:
        print("reply:", j["choices"][0]["message"]["content"][:200])
        print("model:", j.get("model"))
    else:
        print(body[:400])
except Exception:
    print(body[:400])