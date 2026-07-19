#!/usr/bin/env python3
import asyncio
import hashlib
import json
import random
import re
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from urllib.parse import urlparse

import requests
from playwright.async_api import async_playwright

PAGE_TIMEOUT_MS = 60_000
DOWNLOAD_DELAY = 0.5

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def load_and_fix_cookies(cookie_file: Path):
    if not cookie_file.exists():
        return None
    try:
        cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
        fixed = []
        for cookie in cookies:
            c = dict(cookie)
            # Fix sameSite
            same_site = c.get("sameSite", "Lax")
            if same_site not in ["Strict", "Lax", "None"]:
                c["sameSite"] = "Lax"
            # Ensure required fields
            c.setdefault("secure", True)
            c.setdefault("httpOnly", False)
            fixed.append(c)
        return fixed
    except Exception as e:
        print(f"Error loading cookies: {e}")
        return None


def get_unique_key(url: str) -> str:
    match = re.search(r'([a-f0-9]{15,})', url)
    return match.group(1) if match else hashlib.md5(url.encode()).hexdigest()[:16]


def normalize_to_best_url(url: str) -> str:
    if "pinimg.com" not in url:
        return url
    return re.sub(r"/(?:originals|[0-9]+x[0-9]*)/", "/originals/", url)


def url_to_filename(url: str) -> str:
    path = urlparse(url).path
    stem = Path(path).stem or "media"
    suffix = Path(path).suffix.lower() or ".jpg"
    uid = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    return f"{stem}_{uid}{suffix}"


async def harvest_media(page):
    return await page.evaluate("""
        () => {
            const urls = new Set();
            document.querySelectorAll("img").forEach(img => {
                if (img.srcset) {
                    const candidates = img.srcset.split(",");
                    if (candidates.length > 0) {
                        urls.add(candidates[candidates.length-1].trim().split(/\s+/)[0]);
                    }
                } else if (img.src && img.src.includes("pinimg.com")) {
                    urls.add(img.src);
                }
            });
            document.querySelectorAll("video").forEach(v => {
                if (v.src) urls.add(v.src);
            });
            return Array.from(urls);
        }
    """)


async def scroll_and_collect(page, max_items: int, headless: bool):
    seen_keys = set()
    collected = []
    stall = 0
    last_count = 0

    while len(collected) < max_items and stall < 25:
        media = await harvest_media(page)

        for url in media:
            if not url or "pinimg.com" not in url:
                continue
            key = get_unique_key(url)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            collected.append(normalize_to_best_url(url))
            if len(collected) >= max_items:
                break

        if len(collected) == last_count:
            stall += 1
        else:
            stall = 0
            last_count = len(collected)

        await page.evaluate(f"window.scrollBy(0, {random.randint(700, 1400)})")
        await asyncio.sleep(random.uniform(1.3, 2.4))

        if random.random() < 0.35:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.6)

    return collected[:max_items]


def download_file(session, url, dest):
    try:
        resp = session.get(url, timeout=30, stream=True)
        if resp.status_code == 200 and len(resp.content) > 5000:
            dest.write_bytes(resp.content)
            return True
    except:
        pass
    return False


def run_gui():
    def start_download():
        url = url_entry.get().strip()
        try:
            max_items = int(max_items_entry.get() or 100)
        except:
            max_items = 100

        if not url:
            messagebox.showerror("Error", "Please enter a Pinterest URL")
            return

        start_btn.config(state="disabled")
        status_label.config(text="Starting...")

        async def scrape():
            try:
                cookie_file = Path("pinterest_cookies.json")
                cookies = load_and_fix_cookies(cookie_file)

                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=headless_var.get())
                    context = await browser.new_context(user_agent=BROWSER_UA)
                    
                    if cookies:
                        await context.add_cookies(cookies)
                        status_label.config(text="✅ Cookies loaded (signed in)")
                    else:
                        status_label.config(text="⚠ No cookies found - running as guest")

                    page = await context.new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

                    status_label.config(text=f"Scrolling for up to {max_items} items...")
                    media_urls = await scroll_and_collect(page, max_items, headless_var.get())
                    await browser.close()

                output_dir = Path(export_entry.get() or "pinterest_downloads")
                output_dir.mkdir(parents=True, exist_ok=True)

                session = requests.Session()
                session.headers.update({"User-Agent": BROWSER_UA})

                success = 0
                for i, murl in enumerate(media_urls, 1):
                    filename = url_to_filename(murl)
                    dest = output_dir / filename
                    status_label.config(text=f"Downloading {i}/{len(media_urls)}")
                    if download_file(session, murl, dest):
                        success += 1
                    await asyncio.sleep(DOWNLOAD_DELAY)

                messagebox.showinfo("Success", f"Downloaded {success} items!\nSaved to: {output_dir.resolve()}")
            except Exception as e:
                messagebox.showerror("Error", str(e))
            finally:
                start_btn.config(state="normal")
                status_label.config(text="Ready")

        asyncio.run(scrape())

    # GUI
    root = tk.Tk()
    root.title("Pinterest Downloader (Signed-in)")
    root.geometry("730x470")

    tk.Label(root, text="Pinterest URL:", font=("", 10, "bold")).pack(pady=10, anchor="w", padx=20)
    url_entry = tk.Entry(root, width=80)
    url_entry.pack(padx=20, pady=5)
    url_entry.insert(0, "https://www.pinterest.com/pin/640918590714992679/")

    tk.Label(root, text="Max Items:", font=("", 10, "bold")).pack(pady=(10,5), anchor="w", padx=20)
    max_items_entry = tk.Entry(root, width=12)
    max_items_entry.pack(padx=20, pady=5, anchor="w")
    max_items_entry.insert(0, "200")

    tk.Label(root, text="Save Folder:", font=("", 10, "bold")).pack(pady=(10,5), anchor="w", padx=20)
    export_entry = tk.Entry(root, width=80)
    export_entry.pack(padx=20, pady=5)

    headless_var = tk.BooleanVar(value=False)
    tk.Checkbutton(root, text="Headless mode (uncheck recommended)", variable=headless_var).pack(pady=8)

    tk.Label(root, text="Make sure 'pinterest_cookies.json' is in the same folder", fg="blue").pack()

    start_btn = tk.Button(root, text="🚀 START DOWNLOAD", font=("", 12, "bold"), bg="#e60023", fg="white", height=2, command=start_download)
    start_btn.pack(pady=15)

    status_label = tk.Label(root, text="Ready", fg="green")
    status_label.pack()

    root.mainloop()


if __name__ == "__main__":
    run_gui()
EOF
