import os
import time
import re
import urllib.request
import shutil
import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox
import random
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# =========================
# CONFIG
# =========================
OUTPUT_DIR = "adobe_stock_high_res_backups"
STATE_FILE = "stock_state.json"

# =========================
# HELPER: QUALITY PROBE (Improved Video)
# =========================
def get_quality_variants(reference_url, is_video=False):
    if not reference_url or "spacer.gif" in reference_url:
        return []
    
    match = re.search(r'_(\d+)_([a-zA-Z0-9]+)', str(reference_url))
    if not match:
        return [reference_url]

    asset_id = match.group(1)
    asset_hash = match.group(2)
    padded_id = str(asset_id).zfill(10)
    path = f"{padded_id[0:2]}/{padded_id[2:4]}/{padded_id[4:6]}/{padded_id[6:8]}"
    
    variants = []
    if is_video:
        # Enhanced video variants - best quality first
        variants.extend([
            f"https://v.ftcdn.net/{path}/4K_F_{asset_id}_{asset_hash}_ST.mp4",
            f"https://v.ftcdn.net/{path}/2160_F_{asset_id}_{asset_hash}_ST.mp4",
            f"https://v.ftcdn.net/{path}/1080_F_{asset_id}_{asset_hash}_ST.mp4",
            f"https://v.ftcdn.net/{path}/700_F_{asset_id}_{asset_hash}_ST.mp4",
        ])
    else:
        variants.extend([
            f"https://t4.ftcdn.net/jpg/{path}/2400_F_{asset_id}_{asset_hash}.jpg",
            f"https://t4.ftcdn.net/jpg/{path}/1600_F_{asset_id}_{asset_hash}.jpg",
            f"https://t4.ftcdn.net/jpg/{path}/1000_F_{asset_id}_{asset_hash}.jpg",
        ])
            
    return variants

# =========================
# DOWNLOADER
# =========================
def download_best_variant(variants, filename, app):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, filename)
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'}

    for url in variants:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status == 200:
                    with open(path, 'wb') as f:
                        shutil.copyfileobj(response, f)
                    label = "High-Res" if any(x in url for x in ["2400", "4K", "2160"]) else "Standard"
                    app.log(f"✅ Saved [{label}]: {filename}")
                    app.add_media_count(1)
                    return True
        except Exception:
            continue
    app.log(f"❌ Failed all variants for {filename}")
    return False

# =========================
# SCRAPER LOGIC (Rest unchanged)
# =========================
def scrape_adobe_stock(search_url, max_results, app, headless):
    with Stealth().use_sync(sync_playwright()) as p:
        launch_args = [
            '--disable-blink-features=AutomationControlled',
            '--no-sandbox',
            '--disable-infobars',
            '--disable-dev-shm-usage',
            '--ignore-certificate-errors',
            '--window-position=0,0',
        ]
        
        browser = p.chromium.launch(
            headless=headless,
            args=launch_args
        )
        
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        
        context_args = {
            "user_agent": user_agent,
            "viewport": {'width': 1920, 'height': 1080},
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        
        if os.path.exists(STATE_FILE):
            context = browser.new_context(storage_state=STATE_FILE, **context_args)
            app.log("Loaded session state.")
        else:
            context = browser.new_context(**context_args)

        page = context.new_page()
        
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
        """)
        
        page.set_default_timeout(60000)

        try:
            app.log(f"Navigating to: {search_url}")
            page.goto(search_url, wait_until="domcontentloaded")
            
            app.log("Waiting for page to stabilize...")
            time.sleep(random.uniform(4, 7))
            
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except:
                app.log("Network idle timeout - continuing anyway...")
                time.sleep(5)
            
            try:
                page.wait_for_selector("div.search-result-cell, [data-t*='search-result']", timeout=20000)
                app.log("Search results container found.")
            except:
                app.log("Warning: Could not find result container quickly.")

            collected_assets = []
            seen_ids = set()

            while len(collected_assets) < max_results:
                selector = "div.search-result-cell, [data-t*='search-result']"
                
                try:
                    page.wait_for_selector(selector, timeout=15000)
                except:
                    app.log("❌ Timeout waiting for results.")
                    try:
                        page.screenshot(path="headless_debug.png")
                    except:
                        pass
                    break

                app.log("Scrolling to load more assets...")
                for _ in range(7):
                    page.evaluate("window.scrollBy(0, window.innerHeight * (0.6 + Math.random() * 0.7))")
                    time.sleep(random.uniform(0.8, 1.8))

                cells = page.query_selector_all(selector)
                app.log(f"Scanning {len(cells)} cells...")

                for cell in cells:
                    if len(collected_assets) >= max_results:
                        break
                    
                    html = cell.inner_html().lower()
                    # Improved video detection
                    is_video = any(keyword in html for keyword in ["video", "type-video", "play-icon", "video-icon"])
                    
                    img = cell.query_selector("img")
                    if img:
                        src = img.get_attribute("src") or img.get_attribute("data-lazy") or img.get_attribute("data-src")
                        if src and "spacer.gif" not in src:
                            id_match = re.search(r'_(\d+)_', src)
                            asset_id = id_match.group(1) if id_match else None
                            
                            if asset_id and asset_id not in seen_ids:
                                variants = get_quality_variants(src, is_video=is_video)
                                collected_assets.append((variants, is_video, asset_id))
                                seen_ids.add(asset_id)

                app.log(f"Collected so far: {len(collected_assets)} / {max_results}")

                if len(collected_assets) < max_results:
                    next_btn = page.query_selector("a[aria-label*='Next'], button[aria-label*='Next'], .js-search-next")
                    if next_btn and next_btn.is_visible():
                        app.log("Going to next page...")
                        next_btn.click()
                        time.sleep(random.uniform(4, 6))
                    else:
                        app.log("No more pages.")
                        break

            try:
                context.storage_state(path=STATE_FILE)
                app.log("💾 Session state saved.")
            except Exception as e:
                app.log(f"State save note: {e}")

            app.log(f"Starting downloads for {len(collected_assets)} assets...")
            for variants, is_vid, aid in collected_assets:
                ext = ".mp4" if is_vid else ".jpg"
                download_best_variant(variants, f"backup_{aid}{ext}", app)

        except Exception as e:
            app.log(f"Critical Error: {str(e)}")
            try:
                page.screenshot(path=f"error_{int(time.time())}.png")
            except:
                pass
        finally:
            browser.close()

# =========================
# UI (unchanged)
# =========================
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Adobe Stock High-Res Stealth Backup")
        self.root.geometry("920x760")

        header_frame = tk.Frame(root)
        header_frame.pack(pady=10)

        tk.Label(header_frame, text="Max Results:").pack(side="left")
        self.max_entry = tk.Entry(header_frame, width=8)
        self.max_entry.insert(0, "25")
        self.max_entry.pack(side="left", padx=5)

        self.headless_var = tk.BooleanVar(value=True)
        self.headless_chk = tk.Checkbutton(header_frame, text="Run Headless", variable=self.headless_var)
        self.headless_chk.pack(side="left", padx=15)

        tk.Label(root, text="Search URL(s) (one per line):").pack(anchor="w", padx=15)
        self.input_box = scrolledtext.ScrolledText(root, height=7)
        self.input_box.pack(fill="x", padx=15, pady=5)

        self.run_btn = tk.Button(root, text="START BACKUP", command=self.start, 
                               bg="#2c3e50", fg="white", font=("Arial", 11, "bold"), height=2)
        self.run_btn.pack(pady=10, fill="x", padx=15)

        self.media_count_label = tk.Label(root, text="Files Downloaded: 0", font=("Arial", 10, "bold"))
        self.media_count_label.pack()

        self.log_box = scrolledtext.ScrolledText(root, state="disabled", bg="#0c0c0c", fg="#00ff00", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=15, pady=10)

    def log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state="disabled")

    def add_media_count(self, count):
        current = int(self.media_count_label.cget("text").split(": ")[1])
        self.media_count_label.config(text=f"Files Downloaded: {current + count}")

    def start(self):
        urls = self.input_box.get("1.0", tk.END).strip()
        if not urls:
            messagebox.showwarning("Input Required", "Please paste Adobe Stock search URL(s).")
            return
        
        try:
            max_res = int(self.max_entry.get())
        except:
            max_res = 20
            
        self.run_btn.config(state="disabled")
        headless = self.headless_var.get()
        threading.Thread(target=self.worker, args=(urls, max_res, headless), daemon=True).start()

    def worker(self, urls_text, max_res, headless):
        for url in urls_text.split("\n"):
            clean_url = url.strip()
            if clean_url:
                self.log(f"--- Processing: {clean_url} ---")
                scrape_adobe_stock(clean_url, max_res, self, headless)
        
        self.root.after(0, lambda: self.run_btn.config(state="normal"))
        self.log("--- ALL TASKS FINISHED ---")

if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
