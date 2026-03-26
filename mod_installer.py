import json
import os
import re
import sys
import shutil
import tempfile
import zipfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from http.cookiejar import CookieJar
from urllib.request import build_opener, HTTPCookieProcessor

DEFAULT_GDRIVE_URL = "https://drive.google.com/file/d/1ECqt_pRijY4JMyL7gNtxc0pvPGRdaW80/view"
DEFAULT_TARGET_DIR = r"C:\Games\World_of_Tanks_EU"
DEFAULT_YOUTUBE_URL = "https://www.youtube.com/watch?v=kW02I9xf5aU"
CHUNK_SIZE = 32768



def fetch_gdrive_url_from_youtube(yt_url: str):
    """Fetch YouTube video page, parse description, find Google Drive URL.

    Returns (version_line, gdrive_url) or raises on failure.
    version_line is the text line immediately above the Google Drive link.
    """
    request = Request(yt_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    response = urlopen(request, timeout=15)
    html = response.read().decode("utf-8", errors="replace")

    # Extract ytInitialData JSON blob from page source
    description_text = None

    # Try shortDescription from ytInitialPlayerResponse (contains full URLs)
    match = re.search(r'var ytInitialPlayerResponse\s*=\s*({.*?});\s*</script>', html, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            description_text = data.get("videoDetails", {}).get("shortDescription", "")
        except (json.JSONDecodeError, KeyError):
            pass

    # Fallback: extract shortDescription directly from raw HTML
    if not description_text:
        match = re.search(r'"shortDescription"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
        if match:
            description_text = match.group(1).encode().decode('unicode_escape', errors='replace')

    if not description_text:
        raise RuntimeError("Could not extract video description from YouTube page.")

    # Split into lines and find a Google Drive URL
    lines = description_text.replace('\\n', '\n').split('\n')
    gdrive_url = None
    version_line = None
    for i, line in enumerate(lines):
        if re.search(r'drive\.google\.com/file/d/|drive\.google\.com/open\?id=', line):
            # Extract just the URL from the line
            url_match = re.search(r'https?://drive\.google\.com/[A-Za-z0-9/_?=&.%+-]+', line)
            if url_match:
                gdrive_url = url_match.group(0)
            else:
                gdrive_url = line.strip()
            # Version is the non-empty line above
            for j in range(i - 1, -1, -1):
                candidate = lines[j].strip()
                if candidate:
                    version_line = candidate
                    break
            break

    if not gdrive_url:
        raise RuntimeError("No Google Drive link found in video description.")

    return version_line or "Unknown version", gdrive_url


def extract_file_id(url: str) -> str:
    """Extract Google Drive file ID from various URL formats."""
    patterns = [
        r'/file/d/([a-zA-Z0-9_-]+)',
        r'id=([a-zA-Z0-9_-]+)',
        r'^([a-zA-Z0-9_-]+)$',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract file ID from URL: {url}")


def download_from_gdrive(file_id: str, dest_path: str, progress_callback=None):
    """Download a file from Google Drive, handling large file confirmation."""
    base_url = "https://drive.google.com/uc?export=download"
    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))

    # First request
    url = f"{base_url}&id={file_id}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    response = opener.open(request)

    # Check if we got a confirmation page (virus scan warning for large files)
    confirm_token = None
    for cookie in cookie_jar:
        if cookie.name.startswith("download_warning"):
            confirm_token = cookie.value
            break

    if not confirm_token:
        # Check response body for confirmation token
        content = response.read()
        match = re.search(rb'confirm=([0-9A-Za-z_-]+)', content)
        if match:
            confirm_token = match.group(1).decode()
        else:
            # Check for a different confirmation pattern (uuid)
            match = re.search(rb'name="uuid" value="([^"]+)"', content)
            if match:
                uuid_val = match.group(1).decode()
                url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t&uuid={uuid_val}"
                request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
                response = opener.open(request)
                confirm_token = None  # Already handled
            elif len(content) < 100000 and b'<!DOCTYPE' in content:
                # Probably a confirmation page, try with confirm=t
                url = f"{base_url}&id={file_id}&confirm=t"
                request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
                response = opener.open(request)
            else:
                # Small file, already got the content
                with open(dest_path, 'wb') as f:
                    f.write(content)
                return

    if confirm_token:
        url = f"{base_url}&id={file_id}&confirm={confirm_token}"
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        response = opener.open(request)

    # Get total size if available
    total_size = response.headers.get('Content-Length')
    total_size = int(total_size) if total_size else None

    downloaded = 0
    with open(dest_path, 'wb') as f:
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if progress_callback:
                progress_callback(downloaded, total_size)


def merge_folder(src_path, dest_path, log, indent=0):
    """Recursively merge src_path into dest_path.

    For each item in src:
    - If it's a directory and exists in dest, recurse into it (merge).
    - If it's a directory and doesn't exist in dest, copy it entirely.
    - If it's a file, overwrite the destination file.
    Items in dest that are not in src are left untouched.
    """
    prefix = "  " * (indent + 1)
    name = os.path.basename(src_path)

    if not os.path.exists(dest_path):
        log(f"{prefix}Copying new folder: {name}/")
        shutil.copytree(src_path, dest_path)
        return

    log(f"{prefix}Merging into: {name}/")
    for item_name in os.listdir(src_path):
        s = os.path.join(src_path, item_name)
        d = os.path.join(dest_path, item_name)

        if os.path.isdir(s):
            merge_folder(s, d, log, indent + 1)
        else:
            if os.path.exists(d):
                log(f"{prefix}  Overwriting: {item_name}")
            else:
                log(f"{prefix}  Copying: {item_name}")
            shutil.copy2(s, d)


def install_mods(gdrive_url: str, target_dir: str, log_callback=None, progress_callback=None):
    """Main installation logic."""
    def log(msg):
        if log_callback:
            log_callback(msg)

    # Extract file ID
    log("Extracting file ID from URL...")
    file_id = extract_file_id(gdrive_url)
    log(f"File ID: {file_id}")

    # Create temp directory
    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_path = os.path.join(tmp_dir, "mods.zip")

        # Download
        log("Downloading from Google Drive...")
        download_from_gdrive(file_id, zip_path, progress_callback)
        log("Download complete.")

        # Verify it's a valid zip
        if not zipfile.is_zipfile(zip_path):
            raise RuntimeError("Downloaded file is not a valid ZIP archive. The Google Drive link may be incorrect or the file may have been removed.")

        # Extract
        extract_dir = os.path.join(tmp_dir, "extracted")
        log("Extracting ZIP archive...")
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)
        log("Extraction complete.")

        # Find the folders to copy. If the zip contains a single root folder,
        # we want the contents of that folder. Otherwise, copy everything.
        items = os.listdir(extract_dir)
        src_dir = extract_dir
        if len(items) == 1:
            single_item = os.path.join(extract_dir, items[0])
            if os.path.isdir(single_item):
                src_dir = single_item
                log(f"Found root folder in archive: {items[0]}")

        # Ensure target directory exists
        os.makedirs(target_dir, exist_ok=True)

        # Copy folders from src to target
        src_items = os.listdir(src_dir)
        if not src_items:
            raise RuntimeError("The extracted archive is empty.")

        log(f"Installing to: {target_dir}")
        for item_name in src_items:
            src_path = os.path.join(src_dir, item_name)
            dest_path = os.path.join(target_dir, item_name)

            if os.path.isdir(src_path):
                merge_folder(src_path, dest_path, log, indent=1)
            else:
                if os.path.exists(dest_path):
                    log(f"  Removing existing: {item_name}")
                    os.remove(dest_path)
                log(f"  Copying file: {item_name}")
                shutil.copy2(src_path, dest_path)

        log("Installation complete!")


class ModInstallerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("WoT Mod Installer")
        self.root.resizable(False, False)
        self.root.geometry("620x520")

        self._build_ui()
        self._center_window()

        # Auto-fetch version from YouTube on startup
        self._fetch_version()

    def _center_window(self):
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() // 2) - (w // 2)
        y = (self.root.winfo_screenheight() // 2) - (h // 2)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self):
        main_frame = ttk.Frame(self.root, padding=15)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Version info
        ver_frame = ttk.Frame(main_frame)
        ver_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(ver_frame, text="Mod Version:").pack(side=tk.LEFT)
        self.version_var = tk.StringVar(value="Fetching...")
        self.version_label = ttk.Label(ver_frame, textvariable=self.version_var, font=("Segoe UI", 10, "bold"))
        self.version_label.pack(side=tk.LEFT, padx=(5, 0))
        self.refresh_btn = ttk.Button(ver_frame, text="\u21bb", width=3, command=self._fetch_version)
        self.refresh_btn.pack(side=tk.LEFT, padx=(8, 0))

        # YouTube URL
        ttk.Label(main_frame, text="YouTube URL (source for Google Drive link):").pack(anchor=tk.W)
        self.yt_url_var = tk.StringVar(value=DEFAULT_YOUTUBE_URL)
        ttk.Entry(main_frame, textvariable=self.yt_url_var, width=80).pack(fill=tk.X, pady=(0, 10))

        # Google Drive URL
        ttk.Label(main_frame, text="Google Drive URL:").pack(anchor=tk.W)
        self.url_var = tk.StringVar(value=DEFAULT_GDRIVE_URL)
        ttk.Entry(main_frame, textvariable=self.url_var, width=80).pack(fill=tk.X, pady=(0, 10))

        # Target directory
        ttk.Label(main_frame, text="Target Directory:").pack(anchor=tk.W)
        dir_frame = ttk.Frame(main_frame)
        dir_frame.pack(fill=tk.X, pady=(0, 10))
        self.dir_var = tk.StringVar(value=DEFAULT_TARGET_DIR)
        ttk.Entry(dir_frame, textvariable=self.dir_var, width=70).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(dir_frame, text="Browse...", command=self._browse_dir).pack(side=tk.RIGHT, padx=(5, 0))

        # Progress bar
        self.progress = ttk.Progressbar(main_frame, mode='indeterminate')
        self.progress.pack(fill=tk.X, pady=(0, 10))

        # Log area
        ttk.Label(main_frame, text="Log:").pack(anchor=tk.W)
        log_frame = ttk.Frame(main_frame)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        self.log_text = tk.Text(log_frame, height=12, state=tk.DISABLED, wrap=tk.WORD, font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Install button
        self.install_btn = ttk.Button(main_frame, text="Download & Install", command=self._start_install)
        self.install_btn.pack(pady=(0, 5))

    def _browse_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get())
        if d:
            self.dir_var.set(d)

    def _fetch_version(self):
        """Fetch mod version and Google Drive URL from YouTube in background."""
        self.refresh_btn.configure(state=tk.DISABLED)
        self.version_var.set("Fetching...")
        thread = threading.Thread(target=self._run_fetch_version, daemon=True)
        thread.start()

    def _run_fetch_version(self):
        try:
            yt_url = self.yt_url_var.get().strip()
            if not yt_url:
                raise ValueError("YouTube URL is empty.")
            version, gdrive_url = fetch_gdrive_url_from_youtube(yt_url)
            self.root.after(0, self._on_version_fetched, version, gdrive_url)
        except Exception as e:
            self.root.after(0, self._on_version_error, str(e))

    def _on_version_fetched(self, version, gdrive_url):
        self.version_var.set(version)
        self.url_var.set(gdrive_url)
        self.refresh_btn.configure(state=tk.NORMAL)
        self._log(f"Fetched from YouTube: {version}")
        self._log(f"Google Drive URL: {gdrive_url}")

    def _on_version_error(self, error_msg):
        self.version_var.set("Failed to fetch")
        self.refresh_btn.configure(state=tk.NORMAL)
        self._log(f"YouTube fetch error: {error_msg}")
        self._log("Using default Google Drive URL.")

    def _log(self, msg):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _log_threadsafe(self, msg):
        self.root.after(0, self._log, msg)

    def _progress_callback(self, downloaded, total):
        if total:
            pct = downloaded / total * 100
            self.root.after(0, self._update_progress, pct, total)

    def _update_progress(self, pct, total):
        if self.progress['mode'] != 'determinate':
            self.progress.stop()
            self.progress.configure(mode='determinate', maximum=100)
        self.progress['value'] = pct

    def _start_install(self):
        url = self.url_var.get().strip()
        target = self.dir_var.get().strip()

        if not url:
            messagebox.showerror("Error", "Please enter a Google Drive URL.")
            return
        if not target:
            messagebox.showerror("Error", "Please enter a target directory.")
            return

        self.install_btn.configure(state=tk.DISABLED)
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)
        self.progress.configure(mode='indeterminate')
        self.progress.start(15)

        thread = threading.Thread(target=self._run_install, args=(url, target), daemon=True)
        thread.start()

    def _run_install(self, url, target):
        try:
            install_mods(url, target, log_callback=self._log_threadsafe, progress_callback=self._progress_callback)
            self.root.after(0, self._on_success)
        except Exception as e:
            self.root.after(0, self._on_error, str(e))

    def _on_success(self):
        self.progress.stop()
        self.progress.configure(mode='determinate', maximum=100)
        self.progress['value'] = 100
        self.install_btn.configure(state=tk.NORMAL)
        messagebox.showinfo("Success", "Mods installed successfully!")

    def _on_error(self, error_msg):
        self.progress.stop()
        self.progress.configure(mode='determinate', maximum=100)
        self.progress['value'] = 0
        self.install_btn.configure(state=tk.NORMAL)
        self._log(f"ERROR: {error_msg}")
        messagebox.showerror("Error", f"Installation failed:\n{error_msg}")


def main():
    root = tk.Tk()
    ModInstallerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
