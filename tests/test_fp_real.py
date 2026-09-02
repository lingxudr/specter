"""
Diagnostic: figure out what a REAL Android Chrome on this HP would report.
Then patch cf_selenium to match.
"""
import os, time, json
os.chdir(os.path.expanduser('~/specter'))
import sys; sys.path.insert(0, '.')
from cf_selenium import Browser

# First, let's see what current cf_selenium + proot Android Chrome reports
# This is the GIVEAWAY fingerprint
with Browser(headless=True) as b:
    b.get('about:version')
    time.sleep(1)
    info = b.execute_script(r"""(() => {
        return {
            ua: navigator.userAgent,
            platform: navigator.platform,
            hardwareConcurrency: navigator.hardwareConcurrency,
            deviceMemory: navigator.deviceMemory,
            maxTouchPoints: navigator.maxTouchPoints,
            screen: { w: screen.width, h: screen.height, aw: screen.availWidth, ah: screen.availHeight, cd: screen.colorDepth },
            window: { iw: window.innerWidth, ih: window.innerHeight, ow: window.outerWidth, oh: window.outerHeight },
            webgl: (() => { const c = document.createElement('canvas'); const g = c.getContext('webgl'); return g ? { vendor: g.getParameter(37445), renderer: g.getParameter(37446), version: g.getParameter(37444) } : null; })(),
            connection: navigator.connection ? { et: navigator.connection.effectiveType, rtt: navigator.connection.rtt, dl: navigator.connection.downlink } : null,
            touch: 'ontouchstart' in window,
            plugins: Array.from(navigator.plugins).map(p => p.name).slice(0, 5),
            lang: navigator.language,
            langs: navigator.languages,
            cookieEnabled: navigator.cookieEnabled,
            doNotTrack: navigator.doNotTrack,
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            timezoneOffset: new Date().getTimezoneOffset(),
        };
    })()""")
    print("=== Current cf_selenium fingerprint on proot Android Chrome ===")
    print(json.dumps(info, indent=2))
