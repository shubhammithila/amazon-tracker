from playwright.sync_api import sync_playwright
PW = "admin123"
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_context(viewport={"width":1400,"height":900}).new_page()
    pg.goto("http://13.233.144.148/login", timeout=40000)
    pg.screenshot(path=".tmp-shots/live-login.png")
    pg.fill('input[type="password"]', PW); pg.click('button[type="submit"]')
    pg.wait_for_load_state("networkidle", timeout=40000)
    print("nav:", pg.eval_on_selector_all(".nav-links a","e=>e.map(x=>x.textContent.trim())"))
    pg.screenshot(path=".tmp-shots/live-dashboard.png")
    pg.goto("http://13.233.144.148/users-page", timeout=40000)
    pg.wait_for_selector("#createAreas .area", timeout=40000)
    pg.screenshot(path=".tmp-shots/live-users.png")
    print("users page loaded on production")
    b.close()
