
import json
import os
import asyncio
import threading
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk
from urllib.parse import urlparse
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

MAX_CONCURRENT_DETAIL_PAGES = 8  # tune this based on your CPU/network — 5-10 is a safe range

# =========================
# STEP 1: GRID SCRAPER (post URLs + fallback thumb) - runs once, sync-style inside async
# =========================
async def collect_post_urls(page, log_callback, max_scrolls: int = 15):
    results_dict = {}
    prev_total = 0
    no_new_items_count = 0
    scroll_count = 0

    while scroll_count < max_scrolls:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        for _ in range(3):
            await page.keyboard.press("End")
            await page.wait_for_timeout(400)

        try:
            await page.wait_for_function(
                """() => {
                    const imgs = Array.from(document.querySelectorAll('.sku-card-box img'));
                    const visible = imgs.slice(-30);
                    return visible.every(i => i.complete);
                }""",
                timeout=4000,
            )
        except PWTimeout:
            pass
        await page.wait_for_timeout(1000)

        artwork_nodes = page.locator("a.sku-card-box")
        count = await artwork_nodes.count()
        for i in range(count):
            node = artwork_nodes.nth(i)
            post_url = await node.get_attribute("href")
            if not post_url:
                continue
            if post_url.startswith("/"):
                post_url = f"https://www.seaart.ai{post_url}"

            if post_url not in results_dict:
                img_loc = node.locator("img.bg-image, img.cover-image, img.cover-img").first
                if await img_loc.count() == 0:
                    img_loc = node.locator("img:not(.base-image)").first
                raw_src = await img_loc.get_attribute("src") if await img_loc.count() > 0 else None
                title_text = (await node.inner_text()).strip().replace("\n", " ")
                results_dict[post_url] = {
                    "title_or_author": title_text,
                    "post_url": post_url,
                    "thumb_url": raw_src,
                }

        current_total = len(results_dict)
        if current_total == prev_total:
            no_new_items_count += 1
            if no_new_items_count >= 2:
                log_callback("No new grid items found after multiple scrolls. Reached the end.")
                break
        else:
            no_new_items_count = 0

        prev_total = current_total
        scroll_count += 1
        log_callback(f"Grid scroll {scroll_count}/{max_scrolls} | Unique posts collected: {current_total}")

    return list(results_dict.values())


# =========================
# STEP 2: CONCURRENT DETAIL PAGE VISITS
# =========================
async def get_high_res_url(context, item, semaphore, log_callback, index, total):
    async with semaphore:
        post_url = item["post_url"]
        page = await context.new_page()
        try:
            await page.goto(post_url, wait_until="domcontentloaded", timeout=20000)
            try:
                await page.wait_for_selector("img.main-work-image", timeout=10000)
            except PWTimeout:
                log_callback(f"[{index}/{total}] No main-work-image found: {post_url}")
                item["high_res_url"] = None
                return item

            try:
                await page.wait_for_function(
                    """() => {
                        const img = document.querySelector('img.main-work-image');
                        return img && img.complete && img.src && img.src.includes('_high');
                    }""",
                    timeout=8000,
                )
            except PWTimeout:
                pass

            img_loc = page.locator("img.main-work-image").first
            high_res_url = await img_loc.get_attribute("src")
            item["high_res_url"] = high_res_url
            status = "OK" if high_res_url else "missing"
            log_callback(f"[{index}/{total}] {status}: {post_url}")
        except Exception as e:
            log_callback(f"[{index}/{total}] Error on {post_url}: {e}")
            item["high_res_url"] = None
        finally:
            await page.close()
        return item


async def extract_seaart_data_async(url: str, log_callback, max_scrolls: int = 15):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        log_callback(f"Navigating to grid page: {url}...")
        await page.goto(url, wait_until="domcontentloaded")

        log_callback("Waiting for initial grid content to render...")
        try:
            await page.wait_for_selector(".sku-card-box", timeout=15000)
        except PWTimeout:
            log_callback("Timed out waiting for cards. Page may require login or layout changed.")
            await browser.close()
            return []

        log_callback("Scrolling grid to collect all post URLs...")
        grid_items = await collect_post_urls(page, log_callback, max_scrolls=max_scrolls)
        await page.close()

        total = len(grid_items)
        log_callback(f"Collected {total} post URLs. Visiting detail pages concurrently (max {MAX_CONCURRENT_DETAIL_PAGES} at a time)...")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_DETAIL_PAGES)
        tasks = [
            get_high_res_url(context, item, semaphore, log_callback, idx + 1, total)
            for idx, item in enumerate(grid_items)
        ]
        grid_items = await asyncio.gather(*tasks)

        await browser.close()

    return grid_items


def extract_seaart_data(url: str, log_callback, max_scrolls: int = 15):
    """Sync wrapper so the rest of the app (threading model) stays unchanged."""
    return asyncio.run(extract_seaart_data_async(url, log_callback, max_scrolls=max_scrolls))


# =========================
# DOWNLOADER (unchanged logic, high_res_url first, thumb_url fallback)
# =========================
def download_images(data_list: list, download_dir: str, log_callback):
    if not os.path.exists(download_dir):
        os.makedirs(download_dir)
        log_callback(f"Created directory: {download_dir}/")

    log_callback(f"Starting image downloads to '{download_dir}/'...")
    success_count = 0
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    for index, item in enumerate(data_list):
        candidates = []
        if item.get("high_res_url"):
            candidates.append(item["high_res_url"])
        if item.get("thumb_url"):
            candidates.append(item["thumb_url"])

        downloaded = False
        for cand_url in candidates:
            ext = cand_url.rsplit(".", 1)[-1].split("?")[0][:4]
            file_hash = cand_url.split("/")[-1].split(".")[0][:8]
            tag = "HighRes" if cand_url == item.get("high_res_url") else "ThumbFallback"
            filepath = os.path.join(download_dir, f"image_{index}_{tag}_{file_hash}.{ext}")
            try:
                req = urllib.request.Request(cand_url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as response:
                    if response.status == 200:
                        data = response.read()
                        with open(filepath, "wb") as f:
                            f.write(data)
                        log_callback(f"[{index+1}/{len(data_list)}] Saved ({tag}, {len(data)//1024}KB): {os.path.basename(filepath)}")
                        success_count += 1
                        downloaded = True
                        break
            except (urllib.error.HTTPError, urllib.error.URLError):
                continue
            except Exception:
                continue

        if not downloaded:
            log_callback(f"[{index+1}/{len(data_list)}] Failed all download attempts for {item.get('post_url')}")

    log_callback(f"Process complete. Downloaded {success_count} images.")
    return success_count


# =========================
# PROCESS LOGIC (UI Threading) - unchanged
# =========================
def process_urls(input_text, app):
    raw_urls = [u.strip() for u in input_text.split("\n") if u.strip()]
    if not raw_urls:
        app.root.after(0, lambda: messagebox.showerror("Error", "No URLs provided"))
        app.done()
        return

    all_urls = list(dict.fromkeys(raw_urls))
    total = len(all_urls)
    app.set_total_count(total)
    app.log(f"Total URLs to process: {total}")

    total_images = 0
    success_urls = 0

    for idx, url in enumerate(all_urls, start=1):
        app.log(f"=== [{idx}/{total}] Processing URL ===")
        try:
            parsed_url = urlparse(url)
            url_path_id = parsed_url.path.strip("/").split("/")[-1]
            if not url_path_id or len(url_path_id) < 5:
                url_path_id = f"post_{idx}"
            target_dir = os.path.join("seaart_downloads", url_path_id)

            scraped_data = extract_seaart_data(url, app.log, max_scrolls=15)

            if scraped_data:
                os.makedirs(target_dir, exist_ok=True)
                json_path = os.path.join(target_dir, "metadata.json")
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(scraped_data, f, indent=2, ensure_ascii=False)

                downloaded_count = download_images(scraped_data, target_dir, app.log)
                if downloaded_count > 0:
                    success_urls += 1
                    total_images += downloaded_count
                    app.set_download_count(success_urls)
                    app.add_image_count(downloaded_count)
            else:
                app.log(f"No data found for {url}")
        except Exception as e:
            app.log(f"Unhandled exception on {url}: {e}")

    app.log(f"=== DONE: {success_urls}/{total} URLs successful ===")
    app.log(f"Total images downloaded: {total_images}")
    app.done()


# =========================
# UI CLASS - unchanged
# =========================
class SeaArtScraperApp:
    def __init__(self, root):
        self.root = root
        self.root.title("SeaArt High-Res Scraper (Concurrent Detail Pages)")
        self.root.geometry("780x620")

        frame = ttk.Frame(root, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Enter one or more SeaArt grid/profile URLs (one per line):").pack(anchor="w")
        self.url_input = scrolledtext.ScrolledText(frame, height=6)
        self.url_input.pack(fill=tk.X, pady=(0, 10))

        stats_frame = ttk.Frame(frame)
        stats_frame.pack(fill=tk.X, pady=(0, 10))

        self.total_var = tk.StringVar(value="Total URLs: 0")
        self.success_var = tk.StringVar(value="Successful URLs: 0")
        self.images_var = tk.StringVar(value="Images downloaded: 0")

        ttk.Label(stats_frame, textvariable=self.total_var).pack(side=tk.LEFT, padx=10)
        ttk.Label(stats_frame, textvariable=self.success_var).pack(side=tk.LEFT, padx=10)
        ttk.Label(stats_frame, textvariable=self.images_var).pack(side=tk.LEFT, padx=10)

        self.start_btn = ttk.Button(frame, text="Start Scraping", command=self.start)
        self.start_btn.pack(pady=(0, 10))

        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(frame, text="Log:").pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(frame, state="disabled")
        self.log_box.pack(fill=tk.BOTH, expand=True)

        self._image_count = 0
        self._download_count = 0

    def log(self, message):
        def _append():
            self.log_box.config(state="normal")
            self.log_box.insert(tk.END, message + "\n")
            self.log_box.see(tk.END)
            self.log_box.config(state="disabled")
        self.root.after(0, _append)

    def set_total_count(self, count):
        self.root.after(0, lambda: self.total_var.set(f"Total URLs: {count}"))

    def set_download_count(self, count):
        self._download_count = count
        self.root.after(0, lambda: self.success_var.set(f"Successful URLs: {count}"))

    def add_image_count(self, count):
        self._image_count += count
        self.root.after(0, lambda: self.images_var.set(f"Images downloaded: {self._image_count}"))

    def start(self):
        text = self.url_input.get("1.0", tk.END).strip()
        if not text:
            messagebox.showerror("Error", "Please enter at least one URL.")
            return
        self.start_btn.config(state="disabled")
        self.progress.start(10)
        threading.Thread(target=process_urls, args=(text, self), daemon=True).start()

    def done(self):
        def _finish():
            self.progress.stop()
            self.start_btn.config(state="normal")
            messagebox.showinfo("Done", "Scraping process complete. Check the log for details.")
        self.root.after(0, _finish)


if __name__ == "__main__":
    root = tk.Tk()
    app = SeaArtScraperApp(root)
    root.mainloop()
