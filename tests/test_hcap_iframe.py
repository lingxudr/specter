import os, time, json
os.chdir(os.path.expanduser('~/specter'))
import sys; sys.path.insert(0, '.')
from cf_selenium import Browser

email = f"test{int(time.time())}@gmail.com"
pw = "TestPass123"

with Browser(headless=True) as b:
    b.get('https://xkiro.com/register')
    time.sleep(3)

    # Fill
    b.execute_script(f"""(() => {{
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        const e = document.querySelector('input[type=email]');
        setter.call(e, '{email}');
        e.dispatchEvent(new Event('input', {{bubbles: true}}));
        e.dispatchEvent(new Event('blur', {{bubbles: true}}));
        const p = document.querySelector('input[type=password]');
        setter.call(p, '{pw}');
        p.dispatchEvent(new Event('input', {{bubbles: true}}));
        p.dispatchEvent(new Event('blur', {{bubbles: true}}));
    }})()""")
    time.sleep(0.5)

    # Use CDP to inject script INTO the challenge iframe (cross-origin)
    # Target: click the checkbox in the hCaptcha challenge iframe
    # First, get the frame tree
    frames = b._run(b._cdp.cmd('Page.getFrameTree'))
    frames = frames.get('frameTree', frames)
    hcap_frames = []
    def walk(node, path=[]):
        f = node['frame']
        if 'hcaptcha' in f.get('url', ''):
            hcap_frames.append((f['url'], f.get('name'), f.get('id')))
        for child in node.get('childFrames', []):
            walk(child, path + [f.get('name')])
    walk(frames)
    print("hCaptcha frames:")
    for url, name, fid in hcap_frames:
        print(f"  url={url[:80]}... name={name} id={fid}")

    # Find challenge frames and try to inspect their context
    for url, name, fid in hcap_frames:
        if 'frame=challenge' in url:
            print(f"\n=== Inspecting challenge frame: {fid} ===")
            # Try to attach to the frame's execution context
            # First, list execution contexts
            b._run(b._cdp.cmd('Page.enable'))
            b._run(b._cdp.cmd('Runtime.enable'))

            # In modern CDP, use Target.getTargets to find iframe
            # OR use Page.createIsolatedWorld to inject script
            # OR use DOM.resolveNode with the frame element

            # Method: navigate the frame to a blank page we control, then we can inject
            # But that breaks the challenge flow.

            # Better method: just make the frame visible via CSS in the parent + click on its checkbox
            # The frame is in same-origin:parent (hCaptcha.com vs xkiro.com = cross-origin)
            # Cross-origin requires CDP frameTree access

            # Use Page.frameNavigated with a JavaScript URL? No.
            # Use DOM.resolveNode on the iframe element to get backendNodeId
            pass

    # Instead, let's use a CDP hack: the challenge iframe's checkbox has a specific selector
    # We can use Page.captureScreenshot with clip to see it, then dispatchMouseEvent

    # Make challenge iframe visible first
    b.execute_script(r"""(() => {
        document.querySelectorAll('iframe[src*="hcaptcha.com"]').forEach(f => {
            if (f.src.includes('challenge')) {
                f.style.cssText = 'position: fixed !important; top: 100px !important; left: 200px !important; z-index: 999999 !important; width: 600px !important; height: 600px !important; display: block !important;';
            }
        });
    })()""")
    time.sleep(2)

    # Find ALL iframes (now visible) and their positions
    all_iframes = b.execute_script(r"""(() => {
        return Array.from(document.querySelectorAll('iframe')).map(f => {
            const r = f.getBoundingClientRect();
            return {
                src: f.src.slice(0, 80),
                id: f.getAttribute('data-hcaptcha-widget-id'),
                x: r.x, y: r.y, w: r.width, h: r.height,
                visible: r.width > 0 && r.height > 0,
            };
        });
    })()""")
    print("\nAll iframes (after style fix):")
    for i in all_iframes:
        print(f"  {i}")

    # The hCaptcha challenge iframe contains a checkbox at a specific position
    # Take screenshot to see what's there
    p = b.screenshot(full_page=True)
    print(f"\nFull-page shot: {p}")
