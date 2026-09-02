"""
hCaptcha fingerprinting inspection.
Check what hCaptcha sees about our browser — likely flagging us.
"""
import os, time, json
os.chdir(os.path.expanduser('~/specter'))
import sys; sys.path.insert(0, '.')
from cf_selenium import Browser

with Browser(headless=True) as b:
    b.get('https://xkiro.com/register')
    time.sleep(3)

    # Check hcaptcha config + fingerprinting signals
    info = b.execute_script(r"""(() => {
        const out = {};

        // hcaptcha global state
        out.hcaptcha = typeof window.hcaptcha;
        out.hcaptchaKeys = window.hcaptcha ? Object.keys(window.hcaptcha) : [];

        // hCaptcha internal config
        if (window.hcaptcha) {
            try {
                const w = window.hcaptcha;
                out.config = {
                    sitekey: w.sitekey,
                    endpoint: w.endpoint,
                    reportapi: w.reportapi,
                    assethost: w.assethost,
                    imghost: w.imghost,
                    host: w.host,
                    sentinel: w.sentinel,
                    regions: w.regions,
                    theme: w.theme,
                    hl: w.hl,
                    language: w.language,
                    recaptchaCompat: w.recaptchaCompat,
                    challengeUrl: w.challengeUrl,
                };
            } catch (e) { out.configErr = e.message; }
        }

        // Fingerprinting signals
        out.fingerprint = {
            ua: navigator.userAgent,
            platform: navigator.platform,
            vendor: navigator.vendor,
            lang: navigator.language,
            languages: navigator.languages,
            hardwareConcurrency: navigator.hardwareConcurrency,
            deviceMemory: navigator.deviceMemory,
            maxTouchPoints: navigator.maxTouchPoints,
            webdriver: navigator.webdriver,
            plugins: navigator.plugins.length,
            cookieEnabled: navigator.cookieEnabled,
            doNotTrack: navigator.doNotTrack,
            screen: {
                w: screen.width, h: screen.height,
                availW: screen.availWidth, availH: screen.availHeight,
                colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth,
            },
        };

        // WebGL
        try {
            const c = document.createElement('canvas');
            const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
            if (gl) {
                out.webgl = {
                    vendor: gl.getParameter(gl.VENDOR),
                    renderer: gl.getParameter(gl.RENDERER),
                    version: gl.getParameter(gl.VERSION),
                };
            }
        } catch (e) { out.webglErr = e.message; }

        // Canvas fingerprint
        try {
            const c = document.createElement('canvas');
            c.width = 200; c.height = 50;
            const ctx = c.getContext('2d');
            ctx.textBaseline = 'top';
            ctx.font = '14px Arial';
            ctx.fillText('fingerprint', 2, 2);
            out.canvasHash = c.toDataURL().slice(0, 100);
        } catch (e) { out.canvasErr = e.message; }

        // Chrome object
        out.chromeExists = typeof window.chrome;
        if (window.chrome) {
            out.chromeKeys = Object.keys(window.chrome);
        }

        return out;
    })()""")
    print(json.dumps(info, indent=2))
