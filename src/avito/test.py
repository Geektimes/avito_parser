from fake_useragent import UserAgent
from playwright.sync_api import sync_playwright

ua = UserAgent()
print("Случайный User-Agent:", ua.random)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("http://httpbin.org/headers")
    print(page.title())
    browser.close()

print("Всё работает отлично! 🎉")