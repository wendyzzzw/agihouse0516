"""Playwright DOM-liveness test for the live-viz frontend.

Loads demo.html in headless Chromium, clicks "Live (Backend)", and verifies the
page updates genuinely live: the activity log grows, the HUD event counter
climbs, node lifecycle classes change, and agents reach the 'done' state.

A MutationObserver records every node-class change, so transient states
('active') are caught even though a round's events arrive in one burst.

Usage:  python3 check_ui.py <base_url>      e.g. http://localhost:8081
Writes screenshots to runs/ui_t0.png .. ui_final.png.
"""
import sys
import time
import os

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8081"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(REPO, "runs")

_failures = []


def check(cond, label):
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        _failures.append(label)


# A MutationObserver on the graph subtree records which node-state classes ever
# appear — robust against transient states a periodic snapshot would miss.
_OBSERVER = """() => {
  window.__seen = {active: false, done: false, idle: false};
  const obs = new MutationObserver(muts => {
    for (const m of muts) {
      const c = (m.target.getAttribute && m.target.getAttribute('class')) || '';
      if (c.includes('node-state-active')) window.__seen.active = true;
      if (c.includes('node-state-done'))   window.__seen.done   = true;
      if (c.includes('node-state-idle'))   window.__seen.idle   = true;
    }
  });
  obs.observe(document.getElementById('graph'),
              {subtree: true, attributes: true, attributeFilter: ['class']});
}"""


def snapshot(page):
    return page.evaluate("""() => {
        const nodes = [...document.querySelectorAll('#graph g')]
            .map(g => g.getAttribute('class') || '');
        return {
            logRows:   document.querySelectorAll('#activity-log .log-entry').length,
            hudEvents: parseInt(document.getElementById('hud-events').textContent || '0', 10),
            hudGoals:  parseInt(document.getElementById('hud-goals').textContent || '0', 10),
            active:    nodes.filter(c => c.includes('node-state-active')).length,
            done:      nodes.filter(c => c.includes('node-state-done')).length,
            priceTxt:  document.getElementById('stat-price').textContent,
        };
    }""")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(f"{BASE}/demo.html?debug=1")
        page.wait_for_selector("#graph g circle", timeout=10000)

        page.evaluate(_OBSERVER)
        page.click("#btn-live")

        time.sleep(1.5)
        s0 = snapshot(page); page.screenshot(path=os.path.join(SHOTS, "ui_t0.png"))
        time.sleep(4.5)
        s1 = snapshot(page); page.screenshot(path=os.path.join(SHOTS, "ui_t1.png"))
        time.sleep(6.0)
        s2 = snapshot(page); page.screenshot(path=os.path.join(SHOTS, "ui_t2.png"))
        time.sleep(8.0)                                   # let the run finish
        s3 = snapshot(page); page.screenshot(path=os.path.join(SHOTS, "ui_final.png"))
        seen = page.evaluate("window.__seen")
        browser.close()

    print(f"  t0={s0}\n  t1={s1}\n  t2={s2}\n  final={s3}\n  observed states={seen}")
    print()

    # 1. SSE counter climbs across time — events keep arriving
    check(s1["hudEvents"] > s0["hudEvents"], "HUD event counter climbed t0->t1 (stream live)")
    check(s2["hudEvents"] > s1["hudEvents"], "HUD event counter climbed t1->t2 (still live)")

    # 2. activity log GROWS — the DOM re-renders, not just a counter
    check(s2["logRows"] > s0["logRows"], "activity log grew (DOM is updating)")

    # 3. node lifecycle states actually applied (MutationObserver — catches transients)
    check(seen["active"], "nodes entered the 'active' state (agent activated)")
    check(seen["done"],   "nodes entered the 'done' state (goal reached)")

    # 4. by the end, agents reached their goal
    check(s3["done"] > 0, f"green 'done' nodes present at end (done={s3['done']})")
    check(s3["hudGoals"] > 0, f"HUD goals-reached counter > 0 (goals={s3['hudGoals']})")

    # 5. not frozen — final state differs from the first
    check(s3["logRows"] != s0["logRows"] or s3["done"] != s0["done"],
          "final DOM differs from initial (page did not freeze)")

    print()
    if _failures:
        print(f"ui liveness: {len(_failures)} FAILURE(S)")
        return 1
    print("ui liveness: ALL OK")
    print(f"screenshots: {SHOTS}/ui_t0.png .. ui_final.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
