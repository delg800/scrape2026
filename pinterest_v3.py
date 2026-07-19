#!/usr/bin/env python3
import asyncio
import hashlib
import re
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from urllib.parse import urlparse

import requests
from playwright.async_api import async_playwright

PAGE_TIMEOUT_MS = 30_000
DOWNLOAD_DELAY = 0.3
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Strong deduplication
seen_keys = set()

def get_unique_key(url: str) -> str:
    """Very aggressive unique key"""
    # Extract the long hex ID which is unique per pin media
    match = re.search(r'([a-f0-9]{15,})', url)
    if match:
        return match.group(1)
    # fallback
    return hashlib.md5(url.encode()).hexdigest()[:16]


def normalize_to_best_url(url: str) -> str:
    """Force highest resolution"""
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
            // Prefer srcset largest first
            document.querySelectorAll("img").forEach(img => {
                if (img.srcset) {
                    const candidates = img.srcset.split(",");
                    if (candidates.length > 0) {
                        urls.add(candidates[candidates.length-1].trim().split(" ")[0]);
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
    global seen_keys
    seen_keys.clear()
    collected = []
    stall = 0

    while len(collected) < max_items and stall < 25:
        media = await harvest_media(page)
        new_added = 0

        for url in media:
            if not url or "pinimg.com" not in url:
                continue
            key = get_unique_key(url)
            if key in seen_keys:
                continue

            seen_keys.add(key)
            best_url = normalize_to_best_url(url)
            collected.append(best_url)
            new_added += 1

            if len(collected) >= max_items:
                break

        if new_added == 0:
            stall += 1
        else:
            stall = max(0, stall - 1)

        await page.evaluate("window.scrollBy(0, window.innerHeight * 2.8)")
        await asyncio.sleep(1.8 if headless else 1.0)

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
            max_items = int(max_items_entry.get() or 50)
        except:
            max_items = 50

        if not url:
            messagebox.showerror("Error", "Enter a Pinterest URL")
            return

        start_btn.config(state="disabled")
        status_label.config(text="Starting...")

        async def scrape():
            try:
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=headless_var.get())
                    page = await browser.new_page(user_agent=BROWSER_UA)
                    await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

                    status_label.config(text="Scrolling for media...")
                    media_urls = await scroll_and_collect(page, max_items, headless_var.get())
                    await browser.close()

                output_dir = Path(export_entry.get() or "./downloads")
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

                messagebox.showinfo("Done", f"Downloaded {success} unique items to:\n{output_dir.resolve()}")
            except Exception as e:
                messagebox.showerror("Error", str(e))
            finally:
                start_btn.config(state="normal")
                status_label.config(text="Ready")

        asyncio.run(scrape())

    # GUI
    root = tk.Tk()
    root.title("Pinterest Downloader v3")
    root.geometry("680x400")

    tk.Label(root, text="Pinterest URL:", font=("", 10, "bold")).pack(pady=10, anchor="w", padx=20)
    url_entry = tk.Entry(root, width=70)
    url_entry.pack(padx=20, pady=5)
    url_entry.insert(0, "https://www.pinterest.com/pin/640918590714992679/")

    tk.Label(root, text="Max Items:", font=("", 10, "bold")).pack(pady=8, anchor="w", padx=20)
    max_items_entry = tk.Entry(root, width=10)
    max_items_entry.pack(padx=20, pady=5, anchor="w")
    max_items_entry.insert(0, "50")

    tk.Label(root, text="Save Folder (optional):", font=("", 10, "bold")).pack(pady=8, anchor="w", padx=20)
    export_entry = tk.Entry(root, width=70)
    export_entry.pack(padx=20, pady=5)

    headless_var = tk.BooleanVar(value=True)
    tk.Checkbutton(root, text="Headless (background)", variable=headless_var).pack(pady=10)

    start_btn = tk.Button(root, text="🚀 START DOWNLOAD", font=("", 12, "bold"), bg="#e60023", fg="white", command=start_download)
    start_btn.pack(pady=15)

    status_label = tk.Label(root, text="Ready", fg="green")
    status_label.pack()

    root.mainloop()


if __name__ == "__main__":
    run_gui()
EOF
