import os, time, json
os.chdir(os.path.expanduser('~/specter'))
import sys; sys.path.insert(0, '.')
from cf_selenium import Browser

with Browser(headless=True) as b:
    b.get('https://xkiro.com/register')
    time.sleep(3)

    info = b.execute_script(r"""(() => {
        const out = {};
        try {
            out.hcaptcha_keys = Object.keys(window.hcaptcha || {});
        } catch(e) { out.err1 = e.message; }
        const inline = Array.from(document.scripts).map(s => s.textContent || '').join('\n');
        const re = /HCAPTCHA_SITEKEY["'\s:=]+([a-f0-9-]{20,})|sitekey["'\s:=]+([a-f0-9-]{20,})/gi;
        const m = [];
        let r;
        while ((r = re.exec(inline)) !== null) m.push(r[0].slice(0, 100));
        out.script_matches = m.slice(0, 10);
        return JSON.stringify(out);
    })()""")
    print(info)
