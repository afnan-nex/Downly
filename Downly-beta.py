"""
================================================================================
  Downly -- IDM-Style Download Manager
================================================================================

  Required Python packages:
    pip install customtkinter aria2p pillow

  aria2 (download engine) -- install one of:
    Windows (Chocolatey): choco install aria2 -y
    Windows (Scoop):      scoop install aria2
    Manual:               https://github.com/aria2/aria2/releases

  Run:
    python download_manager.py
================================================================================
"""

import os, sys, json, queue, shutil, logging, hashlib, platform
import threading, subprocess, webbrowser, urllib.parse, time, ctypes

# Hide the Python console window immediately (Windows only)
try:
    ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
except Exception:
    pass
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Callable

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

MISSING_PACKAGES: list = []

try:
    import customtkinter as ctk
    from customtkinter import CTkFont
except ImportError:
    MISSING_PACKAGES.append("customtkinter")

try:
    import aria2p
except ImportError:
    MISSING_PACKAGES.append("aria2p")

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

if MISSING_PACKAGES:
    import tkinter as tk
    import tkinter.messagebox as mb
    _r = tk.Tk(); _r.withdraw()
    mb.showerror("Missing Dependencies",
        "The following Python packages are not installed:\n\n" +
        "\n".join(f"  - {p}" for p in MISSING_PACKAGES) +
        f"\n\nInstall with:\n  pip install {' '.join(MISSING_PACKAGES)}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging  (no files, no console output -- pure in-memory / null)
# ---------------------------------------------------------------------------

logging.getLogger("Downly").addHandler(logging.NullHandler())
log = logging.getLogger("Downly")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME    = "Downly"
APP_VERSION = "1.0.0"
ARIA2_RPC_PORT    = 6800
ARIA2_RPC_SECRET  = ""
POLL_INTERVAL_MS  = 800
CLIPBOARD_POLL_MS = 1500

DOWNLOADABLE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm",
    ".iso", ".img", ".bin",
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".mp3", ".flac", ".aac", ".ogg", ".wav",
    ".pdf", ".epub", ".mobi", ".apk", ".ipa",
}

C = {
    "bg_dark":       "#0D1117",
    "bg_panel":      "#161B22",
    "bg_card":       "#1C2128",
    "bg_card_hover": "#21262D",
    "border":        "#30363D",
    "accent":        "#2F81F7",
    "accent_hover":  "#388BFD",
    "accent_dim":    "#1F4F8A",
    "success":       "#3FB950",
    "warning":       "#D29922",
    "error":         "#F85149",
    "text_primary":  "#E6EDF3",
    "text_secondary": "#8B949E",
    "text_muted":    "#6E7681",
    "pause_btn":     "#886C2B",
    "cancel_btn":    "#6E3030",
    "progress_bg":   "#21262D",
    "progress_fill": "#2F81F7",
}

STATUS_COLOURS = {
    "Queued":      C["text_secondary"],
    "Downloading": C["accent"],
    "Paused":      C["warning"],
    "Completed":   C["success"],
    "Failed":      C["error"],
    "Cancelled":   C["text_muted"],
    "Waiting":     C["text_secondary"],
}

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def format_size(n: int) -> str:
    if n < 0:       return "--"
    if n < 1024:    return f"{n} B"
    if n < 1024**2: return f"{n/1024:.1f} KB"
    if n < 1024**3: return f"{n/1024**2:.1f} MB"
    return f"{n/1024**3:.2f} GB"

def format_speed(bps: int) -> str:
    return "--" if bps <= 0 else f"{format_size(bps)}/s"

def format_eta(seconds: int) -> str:
    if seconds <= 0 or seconds > 86400 * 7: return "--"
    h, rem = divmod(int(seconds), 3600)
    m, s   = divmod(rem, 60)
    if h:  return f"{h}h {m:02d}m"
    if m:  return f"{m}m {s:02d}s"
    return f"{s}s"

def is_valid_url(url: str) -> bool:
    try:
        r = urllib.parse.urlparse(url.strip())
        return r.scheme in ("http", "https") and bool(r.netloc)
    except Exception:
        return False

def looks_downloadable(url: str) -> bool:
    try:
        path = urllib.parse.urlparse(url).path.lower()
        return Path(path).suffix in DOWNLOADABLE_EXTENSIONS
    except Exception:
        return False

def guess_filename(url: str) -> str:
    try:
        path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        name = Path(path).name
        if name and "." in name: return name
    except Exception:
        pass
    return ""

def send_notification(title: str, message: str) -> None:
    pass

def aria2c_in_path() -> bool:
    return shutil.which("aria2c") is not None

# ---------------------------------------------------------------------------
# SettingsManager
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "download_folder": str(Path.home() / "Downloads"),
    "max_concurrent": 3, "split": 8, "max_conn_per_server": 8,
    "speed_limit": 0, "timeout": 60, "connect_timeout": 30,
    "max_tries": 0, "retry_wait": 3, "theme": "dark",
    "clipboard_monitor": True, "notify_complete": True,
}

class SettingsManager:
    """In-memory settings only -- no files created."""
    def __init__(self):
        self.data = dict(DEFAULT_SETTINGS)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value  # memory only, no save

# ---------------------------------------------------------------------------
# HistoryManager
# ---------------------------------------------------------------------------

class HistoryManager:
    """In-memory history only -- no files created."""
    def __init__(self):
        self.records: List[dict] = []

    def upsert(self, item):
        record = {
            "id": item.uid, "filename": item.filename, "url": item.url,
            "save_path": str(item.save_dir), "status": item.status,
            "date": item.added_at, "file_size": item.total_size,
            "gid": item.gid or "",
        }
        for i, r in enumerate(self.records):
            if r.get("id") == item.uid:
                self.records[i] = record
                return
        self.records.insert(0, record)

    def remove(self, uid: str):
        self.records = [r for r in self.records if r.get("id") != uid]

    def all(self) -> List[dict]:
        return list(self.records)

# ---------------------------------------------------------------------------
# DownloadItem
# ---------------------------------------------------------------------------

class DownloadItem:
    def __init__(self, url: str, save_dir: str, filename: str = "",
                 username: str = "", password: str = "",
                 user_agent: str = "", referer: str = "", uid: str = ""):
        self.url       = url
        self.save_dir  = Path(save_dir)
        guessed = guess_filename(url)
        if filename and filename != guessed:
            self.filename = filename
            self.is_custom_filename = True
        else:
            self.filename = guessed or ("download_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
            self.is_custom_filename = False
        self.username  = username
        self.password  = password
        self.user_agent = user_agent
        self.referer   = referer
        self.uid       = uid or hashlib.md5(
            f"{url}{datetime.now().isoformat()}".encode()
        ).hexdigest()[:12]
        self.gid: Optional[str] = None
        self.status    = "Queued"
        self.progress: float = 0.0
        self.downloaded: int = 0
        self.total_size: int = 0
        self.speed: int = 0
        self.eta: int = 0
        self.error_msg = ""
        self.added_at  = datetime.now().isoformat(timespec="seconds")
        self.completed_at = ""

    @property
    def file_path(self) -> Path:
        return self.save_dir / self.filename

# ---------------------------------------------------------------------------
# Aria2Manager
# ---------------------------------------------------------------------------

class Aria2Manager:
    def __init__(self, settings: SettingsManager):
        self.settings = settings
        self._proc: Optional[subprocess.Popen] = None
        self._client: Optional[aria2p.API] = None

    def start(self) -> bool:
        if not aria2c_in_path():
            log.error("aria2c not found on PATH.")
            return False
        if self._client and self._is_alive():
            return True
        s = self.settings
        args = [
            "aria2c", "--enable-rpc",
            f"--rpc-listen-port={ARIA2_RPC_PORT}",
            "--rpc-allow-origin-all=true",
            "--daemon=false", "--quiet=true",
            f"--max-concurrent-downloads={s.get('max_concurrent', 3)}",
            f"--split={s.get('split', 8)}",
            f"--max-connection-per-server={s.get('max_conn_per_server', 8)}",
            "--continue=true",
            f"--max-tries={s.get('max_tries', 5)}",
            f"--retry-wait={s.get('retry_wait', 3)}",
            f"--timeout={s.get('timeout', 60)}",
            f"--connect-timeout={s.get('connect_timeout', 30)}",
            "--disk-cache=32M", "--file-allocation=prealloc",
            "--allow-overwrite=true", "--auto-file-renaming=false",
        ]
        spd = s.get("speed_limit", 0)
        if spd and spd > 0:
            args.append(f"--max-overall-download-limit={spd}K")
        log.info("Launching aria2c...")
        try:
            flags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
            self._proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except FileNotFoundError:
            log.error("aria2c not found.")
            return False
        except Exception as e:
            log.error("Failed to start aria2c: %s", e)
            return False
        for attempt in range(20):
            time.sleep(0.4)
            try:
                self._client = aria2p.API(
                    aria2p.Client(host="http://localhost",
                                  port=ARIA2_RPC_PORT, secret=ARIA2_RPC_SECRET)
                )
                self._client.get_stats()
                log.info("Connected to aria2 RPC after %d attempt(s).", attempt + 1)
                return True
            except Exception:
                pass
        log.error("aria2 RPC did not become available.")
        return False

    def stop(self):
        if self._proc and self._proc.poll() is None:
            log.info("Stopping aria2c (PID %d).", self._proc.pid)
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try: self._proc.kill()
                except Exception: pass
        self._proc = None
        self._client = None
        log.info("aria2c stopped.")

    def _is_alive(self) -> bool:
        try: self._client.get_stats(); return True
        except Exception: return False

    def add_download(self, item: DownloadItem) -> Optional[str]:
        if not self._client: return None
        options = {
            "dir": str(item.save_dir), "continue": "true",
        }
        if getattr(item, "is_custom_filename", False):
            options["out"] = item.filename
        if item.user_agent: options["user-agent"] = item.user_agent
        if item.referer:    options["referer"]     = item.referer
        if item.username:   options["http-user"]   = item.username
        if item.password:   options["http-passwd"] = item.password
        try:
            dl = self._client.add_uris([item.url], options=options)
            log.info("Download added: GID=%s  URL=%s", dl.gid, item.url)
            return dl.gid
        except Exception as e:
            log.error("Failed to add download: %s", e)
            return None

    def pause(self, gid: str) -> bool:
        if not self._client: return False
        try:
            self._client.pause([self._client.get_download(gid)])
            return True
        except Exception as e:
            log.warning("Pause failed: %s", e)
            return False

    def resume(self, gid: str) -> bool:
        if not self._client: return False
        try:
            self._client.resume([self._client.get_download(gid)])
            return True
        except Exception as e:
            log.warning("Resume failed: %s", e)
            return False

    def cancel(self, gid: str) -> bool:
        if not self._client: return False
        try:
            self._client.remove([self._client.get_download(gid)])
            return True
        except Exception as e:
            log.warning("Cancel failed: %s", e)
            return False

    def remove_result(self, gid: str):
        if not self._client: return
        try: self._client.remove_download_result([self._client.get_download(gid)])
        except Exception: pass

    def retry(self, item: DownloadItem) -> Optional[str]:
        if item.gid:
            try: self.cancel(item.gid)
            except Exception: pass
        return self.add_download(item)

    def get_status(self, gid: str) -> Optional[dict]:
        if not self._client: return None
        try:
            dl = self._client.get_download(gid)
            status_map = {
                "active":   "Downloading", "waiting": "Queued",
                "paused":   "Paused",      "error":   "Failed",
                "complete": "Completed",   "removed": "Cancelled",
            }
            status   = status_map.get(dl.status, dl.status.capitalize())
            dl_bytes = dl.completed_length or 0
            total    = dl.total_length or 0
            speed    = dl.download_speed or 0
            eta_s    = ((total - dl_bytes) // speed) if speed > 0 and total > dl_bytes else 0
            filename = ""
            try:
                if dl.files: filename = Path(dl.files[0].path).name
            except Exception: pass
            error = ""
            try: error = dl.error_message or ""
            except Exception: pass
            return {
                "status": status, "downloaded": dl_bytes, "total": total,
                "speed": speed, "eta": eta_s, "filename": filename, "error": error,
            }
        except Exception as e:
            log.debug("get_status failed GID=%s: %s", gid, e)
            return None

    def pause_all(self):
        if self._client:
            try: self._client.pause_all()
            except Exception as e: log.warning("pause_all: %s", e)

    def resume_all(self):
        if self._client:
            try: self._client.resume_all()
            except Exception as e: log.warning("resume_all: %s", e)

    @property
    def available(self) -> bool:
        return self._client is not None and self._is_alive()

# ---------------------------------------------------------------------------
# ClipboardMonitor
# ---------------------------------------------------------------------------

class ClipboardMonitor:
    def __init__(self, root, callback):
        self._root = root
        self._callback = callback
        self._last_seen = ""
        self._active = False

    def start(self):
        self._active = True
        self._poll()

    def stop(self):
        self._active = False

    def _poll(self):
        if not self._active: return
        try:
            text = self._root.clipboard_get().strip()
            if text != self._last_seen and is_valid_url(text) and looks_downloadable(text):
                self._last_seen = text
                self._callback(text)
        except Exception: pass
        self._root.after(CLIPBOARD_POLL_MS, self._poll)

# ---------------------------------------------------------------------------
# DownloadCard
# ---------------------------------------------------------------------------

class DownloadCard(ctk.CTkFrame):
    def __init__(self, parent, item: DownloadItem, manager, **kwargs):
        super().__init__(parent, fg_color=C["bg_card"], corner_radius=10,
                         border_width=1, border_color=C["border"], **kwargs)
        self.item = item
        self.manager = manager
        self._build()

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        top.grid_columnconfigure(0, weight=1)

        self._lbl_name = ctk.CTkLabel(
            top, text=self.item.filename,
            font=CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=C["text_primary"], anchor="w",
        )
        self._lbl_name.grid(row=0, column=0, sticky="w")

        self._lbl_status = ctk.CTkLabel(
            top, text=self.item.status,
            font=CTkFont(family="Segoe UI", size=11),
            text_color=STATUS_COLOURS.get(self.item.status, C["text_secondary"]),
            anchor="e",
        )
        self._lbl_status.grid(row=0, column=1, sticky="e", padx=(8, 0))

        url_short = self.item.url[:77] + "..." if len(self.item.url) > 80 else self.item.url
        self._lbl_url = ctk.CTkLabel(self, text=url_short,
                                      font=CTkFont(family="Segoe UI", size=10),
                                      text_color=C["text_muted"], anchor="w")
        self._lbl_url.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 6))

        self._progress = ctk.CTkProgressBar(
            self, height=8, corner_radius=4,
            fg_color=C["progress_bg"], progress_color=C["progress_fill"],
        )
        self._progress.set(0)
        self._progress.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 6))

        stats = ctk.CTkFrame(self, fg_color="transparent")
        stats.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 4))
        stats.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self._lbl_size  = ctk.CTkLabel(stats, text="--",
                                        font=CTkFont(family="Segoe UI", size=11),
                                        text_color=C["text_secondary"], anchor="w")
        self._lbl_size.grid(row=0, column=0, sticky="w")

        self._lbl_speed = ctk.CTkLabel(stats, text="",
                                        font=CTkFont(family="Segoe UI", size=11),
                                        text_color=C["accent"], anchor="w")
        self._lbl_speed.grid(row=0, column=1, sticky="w")

        self._lbl_pct   = ctk.CTkLabel(stats, text="0%",
                                        font=CTkFont(family="Segoe UI", size=11, weight="bold"),
                                        text_color=C["text_primary"], anchor="center")
        self._lbl_pct.grid(row=0, column=2, sticky="ew")

        self._lbl_eta   = ctk.CTkLabel(stats, text="",
                                        font=CTkFont(family="Segoe UI", size=11),
                                        text_color=C["text_secondary"], anchor="e")
        self._lbl_eta.grid(row=0, column=3, sticky="e")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=4, column=0, sticky="ew", padx=14, pady=(4, 12))
        bc = dict(height=26, corner_radius=6,
                  font=CTkFont(family="Segoe UI", size=11), border_width=1)

        self._btn_pause = ctk.CTkButton(btns, text="Pause", width=80,
            fg_color="#2D2A1E", hover_color=C["pause_btn"],
            border_color=C["pause_btn"], text_color=C["warning"],
            command=lambda: self.manager.pause_download(self.item), **bc)
        self._btn_pause.grid(row=0, column=0, padx=(0, 5))

        self._btn_resume = ctk.CTkButton(btns, text="Resume", width=80,
            fg_color=C["accent_dim"], hover_color=C["accent"],
            border_color=C["accent"], text_color=C["text_primary"],
            command=lambda: self.manager.resume_download(self.item), **bc)
        self._btn_resume.grid(row=0, column=1, padx=(0, 5))

        self._btn_cancel = ctk.CTkButton(btns, text="Cancel", width=80,
            fg_color="#2A1B1B", hover_color=C["cancel_btn"],
            border_color=C["cancel_btn"], text_color=C["error"],
            command=lambda: self.manager.cancel_download(self.item), **bc)
        self._btn_cancel.grid(row=0, column=2, padx=(0, 5))

        self._btn_open_file = ctk.CTkButton(btns, text="Open File", width=90,
            fg_color=C["bg_panel"], hover_color=C["bg_card_hover"],
            border_color=C["border"], text_color=C["text_secondary"],
            command=self._open_file, **bc)
        self._btn_open_file.grid(row=0, column=3, padx=(0, 5))

        self._btn_folder = ctk.CTkButton(btns, text="Folder", width=75,
            fg_color=C["bg_panel"], hover_color=C["bg_card_hover"],
            border_color=C["border"], text_color=C["text_secondary"],
            command=self._open_folder, **bc)
        self._btn_folder.grid(row=0, column=4, padx=(0, 5))

        self._btn_retry = ctk.CTkButton(btns, text="Retry", width=70,
            fg_color=C["bg_panel"], hover_color=C["bg_card_hover"],
            border_color=C["border"], text_color=C["text_secondary"],
            command=lambda: self.manager.retry_download(self.item), **bc)
        self._btn_retry.grid(row=0, column=5, padx=(0, 5))

        self._btn_remove = ctk.CTkButton(btns, text="Remove", width=75,
            fg_color=C["bg_panel"], hover_color=C["cancel_btn"],
            border_color=C["border"], text_color=C["text_muted"],
            command=lambda: self.manager.remove_download(self.item, False), **bc)
        self._btn_remove.grid(row=0, column=6)

        for w in (self, self._lbl_name):
            w.bind("<Button-3>", self._ctx_menu)
        self.bind("<Double-Button-1>",
                  lambda e: self._open_file() if self.item.status == "Completed" else None)
        self.refresh()

    def refresh(self):
        item = self.item
        s    = item.status
        self._lbl_name.configure(text=item.filename)
        self._lbl_status.configure(text=s,
            text_color=STATUS_COLOURS.get(s, C["text_secondary"]))
        self._progress.set(min(item.progress / 100.0, 1.0))
        pc = {"Completed": C["success"], "Paused": C["warning"],
              "Failed": C["error"], "Cancelled": C["text_muted"]}.get(s, C["progress_fill"])
        self._progress.configure(progress_color=pc)
        if item.total_size > 0:
            size_str = f"{format_size(item.downloaded)} / {format_size(item.total_size)}"
        elif item.downloaded > 0:
            size_str = format_size(item.downloaded)
        else:
            size_str = "--"
        self._lbl_size.configure(text=size_str)
        self._lbl_speed.configure(
            text=format_speed(item.speed) if s == "Downloading" else "")
        self._lbl_pct.configure(text=f"{item.progress:.1f}%")
        self._lbl_eta.configure(
            text=f"ETA {format_eta(item.eta)}" if s == "Downloading" and item.eta > 0 else "")
        active    = s in ("Downloading", "Queued", "Waiting")
        paused    = s == "Paused"
        completed = s == "Completed"
        failed    = s in ("Failed", "Cancelled")
        self._btn_pause.configure(state="normal" if active else "disabled")
        self._btn_resume.configure(state="normal" if paused else "disabled")
        self._btn_cancel.configure(state="normal" if (active or paused) else "disabled")
        self._btn_open_file.configure(state="normal" if completed else "disabled")
        self._btn_retry.configure(state="normal" if failed else "disabled")

    def _open_file(self):
        p = self.item.file_path
        if p.exists(): os.startfile(str(p))
        else: self.manager.show_error("File not found", str(p))

    def _open_folder(self):
        f = self.item.save_dir
        if f.exists(): os.startfile(str(f))
        else: self.manager.show_error("Folder not found", str(f))

    def _ctx_menu(self, event):
        import tkinter as tk
        m = tk.Menu(self, tearoff=False,
                    bg=C["bg_panel"], fg=C["text_primary"],
                    activebackground=C["accent"], activeforeground=C["text_primary"],
                    relief="flat", bd=0)
        s      = self.item.status
        active = s in ("Downloading", "Queued", "Waiting")
        paused = s == "Paused"
        completed = s == "Completed"
        failed = s in ("Failed", "Cancelled")
        m.add_command(label="Pause",
                      state="normal" if active else "disabled",
                      command=lambda: self.manager.pause_download(self.item))
        m.add_command(label="Resume",
                      state="normal" if paused else "disabled",
                      command=lambda: self.manager.resume_download(self.item))
        m.add_command(label="Cancel",
                      state="normal" if (active or paused) else "disabled",
                      command=lambda: self.manager.cancel_download(self.item))
        m.add_command(label="Retry",
                      state="normal" if failed else "disabled",
                      command=lambda: self.manager.retry_download(self.item))
        m.add_separator()
        m.add_command(label="Open File",
                      state="normal" if completed else "disabled",
                      command=self._open_file)
        m.add_command(label="Open Folder", command=self._open_folder)
        m.add_separator()
        m.add_command(label="Copy URL", command=self._copy_url)
        m.add_separator()
        m.add_command(label="Remove from List",
                      command=lambda: self.manager.remove_download(self.item, False))
        m.add_command(label="Delete File",
                      command=lambda: self.manager.remove_download(self.item, True))
        m.tk_popup(event.x_root, event.y_root)

    def _copy_url(self):
        self.clipboard_clear()
        self.clipboard_append(self.item.url)

# ---------------------------------------------------------------------------
# NewDownloadDialog
# ---------------------------------------------------------------------------

class NewDownloadDialog(ctk.CTkToplevel):
    def __init__(self, parent, settings: SettingsManager,
                 initial_url="", callback=None):
        super().__init__(parent)
        self.settings = settings
        self.callback = callback
        self._adv_open = False
        self.title("New Download")
        self.geometry("640x560")
        self.resizable(False, False)
        self.configure(fg_color=C["bg_dark"])
        self.transient(parent)
        self.grab_set()
        self.lift()
        self.focus_force()
        self._build(initial_url)

    def _build(self, url0):
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text="New Download",
                     font=CTkFont(family="Segoe UI", size=18, weight="bold"),
                     text_color=C["text_primary"]).grid(
            row=0, column=0, padx=24, pady=(24, 4), sticky="w")

        ctk.CTkLabel(self, text="Download URL",
                     font=CTkFont(family="Segoe UI", size=12),
                     text_color=C["text_secondary"]).grid(
            row=1, column=0, padx=24, pady=(10, 2), sticky="w")

        self._url_var = ctk.StringVar(value=url0)
        self._url_e = ctk.CTkEntry(
            self, textvariable=self._url_var, height=38,
            placeholder_text="https://example.com/file.zip",
            fg_color=C["bg_panel"], border_color=C["border"],
            text_color=C["text_primary"], font=CTkFont(family="Segoe UI", size=12))
        self._url_e.grid(row=2, column=0, padx=24, sticky="ew")
        self._url_e.bind("<Return>", lambda e: self._submit())

        ctk.CTkLabel(self, text="Save to",
                     font=CTkFont(family="Segoe UI", size=12),
                     text_color=C["text_secondary"]).grid(
            row=3, column=0, padx=24, pady=(12, 2), sticky="w")

        fr = ctk.CTkFrame(self, fg_color="transparent")
        fr.grid(row=4, column=0, padx=24, sticky="ew")
        fr.grid_columnconfigure(0, weight=1)
        self._folder_var = ctk.StringVar(value=self.settings.get("download_folder"))
        ctk.CTkEntry(fr, textvariable=self._folder_var, height=38,
                     fg_color=C["bg_panel"], border_color=C["border"],
                     text_color=C["text_primary"],
                     font=CTkFont(family="Segoe UI", size=12)).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(fr, text="Browse", width=80, height=38,
                      fg_color=C["bg_panel"], hover_color=C["bg_card_hover"],
                      border_width=1, border_color=C["border"],
                      text_color=C["text_secondary"],
                      command=self._browse).grid(row=0, column=1, padx=(6, 0))

        self._adv_btn = ctk.CTkButton(
            self, text="+ Advanced Options",
            fg_color="transparent", hover_color=C["bg_card"],
            text_color=C["text_secondary"], anchor="w",
            font=CTkFont(family="Segoe UI", size=12),
            command=self._toggle_adv)
        self._adv_btn.grid(row=5, column=0, padx=18, pady=(10, 0), sticky="w")

        self._adv_frame = ctk.CTkFrame(self, fg_color=C["bg_panel"],
                                       corner_radius=8, border_width=1,
                                       border_color=C["border"])
        self._adv_frame.grid_columnconfigure(1, weight=1)
        pad = {"padx": 14, "pady": 4}
        for row_n, (lbl, show) in enumerate([
            ("Username",   ""),
            ("Password",   "*"),
            ("User-Agent", ""),
            ("Referer",    ""),
        ]):
            ctk.CTkLabel(self._adv_frame, text=lbl,
                         font=CTkFont(family="Segoe UI", size=11),
                         text_color=C["text_secondary"], anchor="w",
                         width=100).grid(row=row_n, column=0, sticky="w", **pad)
            e = ctk.CTkEntry(self._adv_frame, height=30,
                             fg_color=C["bg_card"], border_color=C["border"],
                             text_color=C["text_primary"],
                             font=CTkFont(family="Segoe UI", size=11))
            if show: e.configure(show=show)
            e.grid(row=row_n, column=1, sticky="ew", **pad)
            setattr(self, f"_e_{row_n}", e)

        br = ctk.CTkFrame(self, fg_color="transparent")
        br.grid(row=7, column=0, padx=24, pady=(16, 24), sticky="e")
        ctk.CTkButton(br, text="Cancel", width=100, height=38,
                      fg_color=C["bg_panel"], hover_color=C["bg_card_hover"],
                      border_width=1, border_color=C["border"],
                      text_color=C["text_secondary"],
                      font=CTkFont(family="Segoe UI", size=13),
                      command=self.destroy).grid(row=0, column=0, padx=(0, 10))
        ctk.CTkButton(br, text="Download", width=120, height=38,
                      fg_color=C["accent"], hover_color=C["accent_hover"],
                      text_color="white",
                      font=CTkFont(family="Segoe UI", size=13, weight="bold"),
                      command=self._submit).grid(row=0, column=1)

    def _toggle_adv(self):
        if self._adv_open:
            self._adv_frame.grid_forget()
            self._adv_btn.configure(text="+ Advanced Options")
            self.geometry("640x560")
            self._adv_open = False
        else:
            self._adv_frame.grid(row=6, column=0, padx=24, sticky="ew")
            self._adv_btn.configure(text="- Advanced Options")
            self.geometry("640x680")
            self._adv_open = True

    def _browse(self):
        from tkinter import filedialog
        f = filedialog.askdirectory(initialdir=self._folder_var.get())
        if f:
            self._folder_var.set(f)
            self.settings.set("download_folder", f)

    def _submit(self):
        url = self._url_var.get().strip()
        if not is_valid_url(url):
            if hasattr(self, "_err"):
                self._err.configure(text="Please enter a valid http:// or https:// URL.")
            else:
                self._err = ctk.CTkLabel(self, text="Please enter a valid http:// or https:// URL.",
                    font=CTkFont(family="Segoe UI", size=11), text_color=C["error"])
                self._err.grid(row=8, column=0, padx=24, pady=(0, 4), sticky="w")
            return
        folder = self._folder_var.get().strip() or str(Path.home() / "Downloads")
        item = DownloadItem(
            url=url, save_dir=folder,
            username=self._e_0.get().strip() if self._adv_open else "",
            password=self._e_1.get()         if self._adv_open else "",
            user_agent=self._e_2.get().strip() if self._adv_open else "",
            referer=self._e_3.get().strip()   if self._adv_open else "",
        )
        self.destroy()
        if self.callback:
            self.callback(item)

# ---------------------------------------------------------------------------
# SettingsWindow
# ---------------------------------------------------------------------------

class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, parent, settings: SettingsManager, on_close=None):
        super().__init__(parent)
        self.settings = settings
        self.on_close = on_close
        self.title("Settings")
        self.geometry("560x640")
        self.resizable(False, False)
        self.configure(fg_color=C["bg_dark"])
        self.transient(parent)
        self.grab_set()
        self.lift()
        self.focus_force()
        self._build()

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text="Settings",
                     font=CTkFont(family="Segoe UI", size=20, weight="bold"),
                     text_color=C["text_primary"]).grid(
            row=0, column=0, padx=24, pady=(24, 16), sticky="w")

        tabs = ctk.CTkTabview(
            self, fg_color=C["bg_panel"],
            segmented_button_fg_color=C["bg_dark"],
            segmented_button_selected_color=C["accent"],
            segmented_button_selected_hover_color=C["accent_hover"],
            segmented_button_unselected_color=C["bg_panel"],
            segmented_button_unselected_hover_color=C["bg_card"],
            text_color=C["text_primary"],
            border_color=C["border"], border_width=1,
        )
        tabs.grid(row=1, column=0, padx=24, sticky="nsew")
        self.grid_rowconfigure(1, weight=1)

        dt = tabs.add("Downloads")
        dt.grid_columnconfigure(1, weight=1)

        # Folder row
        ctk.CTkLabel(dt, text="Download Folder",
                     font=CTkFont(family="Segoe UI", size=12),
                     text_color=C["text_secondary"], anchor="w",
                     width=220).grid(row=0, column=0, padx=14, pady=8, sticky="w")
        self._s_folder = ctk.StringVar(value=self.settings.get("download_folder"))
        frr = ctk.CTkFrame(dt, fg_color="transparent")
        frr.grid(row=0, column=1, padx=14, pady=8, sticky="ew")
        frr.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(frr, textvariable=self._s_folder, height=30,
                     fg_color=C["bg_card"], border_color=C["border"],
                     text_color=C["text_primary"],
                     font=CTkFont(family="Segoe UI", size=12)).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(frr, text="...", width=36, height=30,
                      fg_color=C["bg_card"], hover_color=C["bg_card_hover"],
                      border_width=1, border_color=C["border"],
                      text_color=C["text_secondary"],
                      command=self._browse_folder).grid(row=0, column=1, padx=(4, 0))

        def sl_row(tab, label, row_n, key, frm=1, to=10, steps=9):
            ctk.CTkLabel(tab, text=label,
                         font=CTkFont(family="Segoe UI", size=12),
                         text_color=C["text_secondary"], anchor="w",
                         width=220).grid(row=row_n, column=0, padx=14, pady=8, sticky="w")
            var = ctk.IntVar(value=int(self.settings.get(key, frm)))
            fw  = ctk.CTkFrame(tab, fg_color="transparent")
            fw.grid(row=row_n, column=1, padx=14, pady=8, sticky="ew")
            ctk.CTkSlider(fw, from_=frm, to=to, number_of_steps=steps, variable=var,
                          button_color=C["accent"], button_hover_color=C["accent_hover"],
                          progress_color=C["accent"], fg_color=C["border"]).pack(
                side="left", fill="x", expand=True)
            ctk.CTkLabel(fw, textvariable=var, width=30,
                         font=CTkFont(family="Segoe UI", size=12),
                         text_color=C["text_primary"]).pack(side="left", padx=(6, 0))
            return var

        self._s_concurrent = sl_row(dt, "Max Simultaneous Downloads", 1, "max_concurrent", 1, 10, 9)
        self._s_split       = sl_row(dt, "Connections per Download",   2, "split",          1, 16, 15)
        self._s_conn        = sl_row(dt, "Max Connections per Server",  3, "max_conn_per_server", 1, 16, 15)

        self._s_notify = ctk.BooleanVar(value=self.settings.get("notify_complete", True))
        ctk.CTkLabel(dt, text="Notify on Completion",
                     font=CTkFont(family="Segoe UI", size=12),
                     text_color=C["text_secondary"]).grid(row=4, column=0, padx=14, pady=8, sticky="w")
        ctk.CTkSwitch(dt, variable=self._s_notify, text="",
                      button_color=C["accent"],
                      progress_color=C["accent"]).grid(row=4, column=1, padx=14, pady=8, sticky="w")

        self._s_clip = ctk.BooleanVar(value=self.settings.get("clipboard_monitor", True))
        ctk.CTkLabel(dt, text="Monitor Clipboard",
                     font=CTkFont(family="Segoe UI", size=12),
                     text_color=C["text_secondary"]).grid(row=5, column=0, padx=14, pady=8, sticky="w")
        ctk.CTkSwitch(dt, variable=self._s_clip, text="",
                      button_color=C["accent"],
                      progress_color=C["accent"]).grid(row=5, column=1, padx=14, pady=8, sticky="w")

        nt = tabs.add("Network")
        nt.grid_columnconfigure(1, weight=1)

        def ent_row(tab, label, row_n, key):
            ctk.CTkLabel(tab, text=label,
                         font=CTkFont(family="Segoe UI", size=12),
                         text_color=C["text_secondary"], anchor="w",
                         width=220).grid(row=row_n, column=0, padx=14, pady=8, sticky="w")
            var = ctk.StringVar(value=str(self.settings.get(key, "")))
            ctk.CTkEntry(tab, textvariable=var, height=30,
                         fg_color=C["bg_card"], border_color=C["border"],
                         text_color=C["text_primary"],
                         font=CTkFont(family="Segoe UI", size=12)).grid(
                row=row_n, column=1, padx=14, pady=8, sticky="ew")
            return var

        self._s_speed    = ent_row(nt, "Speed Limit (KB/s, 0=unlimited)", 0, "speed_limit")
        self._s_timeout  = ent_row(nt, "Timeout (seconds)",               1, "timeout")
        self._s_ctimeout = ent_row(nt, "Connect Timeout (seconds)",       2, "connect_timeout")
        self._s_tries    = ent_row(nt, "Max Retries (0=infinite)",        3, "max_tries")
        self._s_wait     = ent_row(nt, "Retry Wait (seconds)",            4, "retry_wait")

        at = tabs.add("Appearance")
        at.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(at, text="Theme",
                     font=CTkFont(family="Segoe UI", size=12),
                     text_color=C["text_secondary"]).grid(
            row=0, column=0, padx=14, pady=14, sticky="w")
        self._s_theme = ctk.StringVar(value=self.settings.get("theme", "dark").capitalize())
        ctk.CTkSegmentedButton(
            at, values=["Dark", "Light", "System"], variable=self._s_theme,
            selected_color=C["accent"], selected_hover_color=C["accent_hover"],
            unselected_color=C["bg_card"], unselected_hover_color=C["bg_card_hover"],
            text_color=C["text_primary"],
            font=CTkFont(family="Segoe UI", size=12),
        ).grid(row=0, column=1, padx=14, pady=14, sticky="w")

        ctk.CTkButton(self, text="Save Settings", height=40,
                      fg_color=C["accent"], hover_color=C["accent_hover"],
                      text_color="white",
                      font=CTkFont(family="Segoe UI", size=13, weight="bold"),
                      command=self._save).grid(row=2, column=0, padx=24, pady=(12, 24), sticky="e")

    def _browse_folder(self):
        from tkinter import filedialog
        f = filedialog.askdirectory(initialdir=self._s_folder.get())
        if f: self._s_folder.set(f)

    def _save(self):
        s = self.settings
        s.set("download_folder", self._s_folder.get())
        s.set("max_concurrent", self._s_concurrent.get())
        s.set("split", self._s_split.get())
        s.set("max_conn_per_server", self._s_conn.get())
        s.set("notify_complete", self._s_notify.get())
        s.set("clipboard_monitor", self._s_clip.get())
        def _int(v, d):
            try: return int(v.get())
            except: return d
        s.set("speed_limit",     _int(self._s_speed, 0))
        s.set("timeout",         _int(self._s_timeout, 60))
        s.set("connect_timeout", _int(self._s_ctimeout, 30))
        s.set("max_tries",       _int(self._s_tries, 5))
        s.set("retry_wait",      _int(self._s_wait, 3))
        s.set("theme", self._s_theme.get().lower())
        ctk.set_appearance_mode(self._s_theme.get().lower())
        if self.on_close: self.on_close()
        self.destroy()

# ---------------------------------------------------------------------------
# DownloadManager (orchestrator)
# ---------------------------------------------------------------------------

class DownloadManager:
    def __init__(self, settings: SettingsManager, history: HistoryManager,
                 aria2: Aria2Manager, on_update):
        self.settings = settings
        self.history  = history
        self.aria2    = aria2
        self.on_update = on_update
        self.items: List[DownloadItem] = []
        self._lock    = threading.Lock()
        self._running = False
        self._q: queue.Queue = queue.Queue()

    def start_polling(self):
        self._running = True
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def stop_polling(self):
        self._running = False

    def add_item(self, item: DownloadItem, start=True):
        with self._lock: self.items.append(item)
        self.history.upsert(item)
        log.info("Added: %s", item.filename)
        if start and self.aria2.available: self._start_item(item)
        self.on_update()

    def _start_item(self, item):
        gid = self.aria2.add_download(item)
        if gid:
            item.gid, item.status = gid, "Queued"
        else:
            item.status = "Failed"
            item.error_msg = "Failed to add to aria2."
        self.history.upsert(item)
        self.on_update()

    def pause_download(self, item):
        if item.gid and self.aria2.pause(item.gid):
            item.status = "Paused"
            log.info("Paused: %s", item.filename)
            self.history.upsert(item); self.on_update()

    def resume_download(self, item):
        if item.gid and self.aria2.resume(item.gid):
            item.status = "Downloading"
            log.info("Resumed: %s", item.filename)
            self.history.upsert(item); self.on_update()

    def cancel_download(self, item):
        if item.gid: self.aria2.cancel(item.gid)
        item.status = "Cancelled"
        log.info("Cancelled: %s", item.filename)
        self.history.upsert(item); self.on_update()

    def retry_download(self, item):
        item.status, item.progress, item.downloaded, item.error_msg = "Queued", 0.0, 0, ""
        gid = self.aria2.retry(item)
        if gid:
            item.gid = gid
            log.info("Retrying: %s", item.filename)
        else:
            item.status, item.error_msg = "Failed", "Retry failed."
        self.history.upsert(item); self.on_update()

    def remove_download(self, item, delete_file=False):
        if delete_file and item.file_path.exists():
            try: item.file_path.unlink(); log.info("Deleted: %s", item.file_path)
            except Exception as e: log.warning("Delete failed: %s", e)
        if item.gid:
            try: self.aria2.cancel(item.gid); self.aria2.remove_result(item.gid)
            except Exception: pass
        with self._lock: self.items = [i for i in self.items if i.uid != item.uid]
        self.history.remove(item.uid)
        self.on_update()

    def pause_all(self):
        self.aria2.pause_all()
        for i in self.items:
            if i.status in ("Downloading", "Queued", "Waiting"): i.status = "Paused"
        self.on_update()

    def resume_all(self):
        self.aria2.resume_all()
        for i in self.items:
            if i.status == "Paused": i.status = "Downloading"
        self.on_update()

    def cancel_all(self):
        for i in list(self.items):
            if i.status not in ("Completed", "Cancelled"): self.cancel_download(i)

    def _poll_loop(self):
        while self._running:
            updates: Dict[str, dict] = {}
            with self._lock: snap = list(self.items)
            for item in snap:
                if not item.gid: continue
                if item.status in ("Completed", "Cancelled", "Failed"): continue
                info = self.aria2.get_status(item.gid)
                if info: updates[item.uid] = info
            self._q.put(updates)
            time.sleep(POLL_INTERVAL_MS / 1000.0)

    def consume_updates(self, notify_cb):
        while not self._q.empty():
            updates = self._q.get_nowait()
            for item in self.items:
                info = updates.get(item.uid)
                if not info: continue
                prev = item.status
                item.status     = info["status"]
                item.downloaded = info["downloaded"]
                item.total_size = info["total"]
                item.speed      = info["speed"]
                item.eta        = info["eta"]
                if item.total_size > 0:
                    item.progress = (item.downloaded / item.total_size) * 100.0
                resolved = info.get("filename", "")
                if resolved and resolved != item.filename: item.filename = resolved
                if info.get("error"): item.error_msg = info["error"]
                if item.status == "Completed" and prev != "Completed":
                    item.completed_at = datetime.now().isoformat(timespec="seconds")
                    item.progress = 100.0
                    log.info("Completed: %s", item.filename)
                    self.history.upsert(item)
                    if self.settings.get("notify_complete", True): notify_cb(item)
                if item.status == "Failed" and prev != "Failed":
                    log.error("Failed: %s -- %s", item.filename, item.error_msg)
                    self.history.upsert(item)

    def show_error(self, title, message):
        log.error("%s: %s", title, message)

    def restore_from_history(self):
        for r in self.history.all():
            item = DownloadItem(
                url=r.get("url", ""),
                save_dir=r.get("save_path", str(Path.home() / "Downloads")),
                filename=r.get("filename", ""),
                uid=r.get("id", ""),
            )
            item.gid        = r.get("gid") or None
            item.status     = r.get("status", "Unknown")
            item.total_size = r.get("file_size", 0)
            if item.status in ("Downloading", "Queued", "Waiting"):
                item.status = "Paused"
            with self._lock: self.items.append(item)

# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------

class MainWindow:
    def __init__(self, root, dm: DownloadManager, settings: SettingsManager):
        self.root     = root
        self.dm       = dm
        self.settings = settings
        dm.show_error = self.show_error
        self._cards: Dict[str, DownloadCard] = {}
        self._clipboard_monitor = None
        self._clipboard_dialog_open = False
        self._build()
        self._start_clipboard_monitor()
        self._schedule_gui_poll()
        self._bind_shortcuts()
        if not aria2c_in_path():
            self.root.after(300, self._show_aria2_warning)

    def _build(self):
        self.root.title(f"{APP_NAME} -- Download Manager")
        self.root.minsize(900, 500)
        self.root.geometry("1100x550")
        self.root.configure(fg_color=C["bg_dark"])
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(2, weight=1)
        self._build_header()
        self._build_url_bar()
        self._build_list()
        self._build_status_bar()

    def _build_header(self):
        hdr = ctk.CTkFrame(self.root, fg_color=C["bg_panel"], corner_radius=0, height=60)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_columnconfigure(2, weight=1)
        hdr.grid_propagate(False)

        ctk.CTkLabel(hdr, text=f"  {APP_NAME}",
                     font=CTkFont(family="Segoe UI", size=20, weight="bold"),
                     text_color=C["accent"]).grid(row=0, column=0, padx=(16, 24), pady=14, sticky="w")

        bc = dict(height=34, corner_radius=8,
                  font=CTkFont(family="Segoe UI", size=12, weight="bold"), border_width=1)

        btns = [
            ("+ New Download", 150, C["accent"], C["accent_hover"], C["accent"], "white",
             self.open_new_download_dialog),
            ("Pause All",  100, C["bg_card"], C["pause_btn"],  C["border"], C["warning"],
             self.dm.pause_all),
            ("Resume All", 110, C["bg_card"], C["accent_dim"], C["border"], C["accent"],
             self.dm.resume_all),
            ("Cancel All", 110, C["bg_card"], C["cancel_btn"], C["border"], C["error"],
             self.dm.cancel_all),
            ("Settings",    90, C["bg_card"], C["bg_card_hover"], C["border"], C["text_secondary"],
             self.open_settings),
        ]
        for col, (text, w, fg, hov, bdr, tc, cmd) in enumerate(btns, start=1):
            if col == 2: col = 3  # leave column 2 empty for spacer
            elif col > 2: col += 1
            ctk.CTkButton(hdr, text=text, width=w, fg_color=fg, hover_color=hov,
                          border_color=bdr, text_color=tc, command=cmd, **bc).grid(
                row=0, column=col, padx=4 if col != 1 else 6,
                padx2=(4, 16) if col == 5 else None,
                pady=12)

    def _build_header(self):
        hdr = ctk.CTkFrame(self.root, fg_color=C["bg_panel"], corner_radius=0, height=60)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_columnconfigure(2, weight=1)
        hdr.grid_propagate(False)

        ctk.CTkLabel(hdr, text=f"  {APP_NAME}",
                     font=CTkFont(family="Segoe UI", size=20, weight="bold"),
                     text_color=C["accent"]).grid(
            row=0, column=0, padx=(16, 24), pady=14, sticky="w")

        bc = dict(height=34, corner_radius=8,
                  font=CTkFont(family="Segoe UI", size=12, weight="bold"), border_width=1)

        ctk.CTkButton(hdr, text="+ New Download", width=150,
                      fg_color=C["accent"], hover_color=C["accent_hover"],
                      border_color=C["accent"], text_color="white",
                      command=self.open_new_download_dialog, **bc).grid(
            row=0, column=1, padx=6, pady=12)
        ctk.CTkButton(hdr, text="Pause All", width=100,
                      fg_color=C["bg_card"], hover_color=C["pause_btn"],
                      border_color=C["border"], text_color=C["warning"],
                      command=self.dm.pause_all, **bc).grid(
            row=0, column=3, padx=4, pady=12)
        ctk.CTkButton(hdr, text="Resume All", width=110,
                      fg_color=C["bg_card"], hover_color=C["accent_dim"],
                      border_color=C["border"], text_color=C["accent"],
                      command=self.dm.resume_all, **bc).grid(
            row=0, column=4, padx=4, pady=12)
        ctk.CTkButton(hdr, text="Cancel All", width=110,
                      fg_color=C["bg_card"], hover_color=C["cancel_btn"],
                      border_color=C["border"], text_color=C["error"],
                      command=self.dm.cancel_all, **bc).grid(
            row=0, column=5, padx=4, pady=12)
        ctk.CTkButton(hdr, text="Settings", width=90,
                      fg_color=C["bg_card"], hover_color=C["bg_card_hover"],
                      border_color=C["border"], text_color=C["text_secondary"],
                      command=self.open_settings, **bc).grid(
            row=0, column=6, padx=(4, 16), pady=12)

    def _build_url_bar(self):
        bar = ctk.CTkFrame(self.root, fg_color=C["bg_panel"], corner_radius=0,
                           border_width=1, border_color=C["border"])
        bar.grid(row=1, column=0, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(bar, text="URL",
                     font=CTkFont(family="Segoe UI", size=12, weight="bold"),
                     text_color=C["text_secondary"], width=50).grid(
            row=0, column=0, padx=(16, 8), pady=14)

        self._url_var = ctk.StringVar()
        self._url_e   = ctk.CTkEntry(
            bar, textvariable=self._url_var,
            placeholder_text="Paste download URL here...",
            height=38, fg_color=C["bg_card"], border_color=C["border"],
            text_color=C["text_primary"],
            placeholder_text_color=C["text_muted"],
            font=CTkFont(family="Segoe UI", size=13))
        self._url_e.grid(row=0, column=1, padx=0, pady=14, sticky="ew")
        self._url_e.bind("<Return>", lambda e: self._quick_start())

        ctk.CTkButton(bar, text="Download", width=110, height=38,
                      fg_color=C["accent"], hover_color=C["accent_hover"],
                      text_color="white", corner_radius=8,
                      font=CTkFont(family="Segoe UI", size=13, weight="bold"),
                      command=self._quick_start).grid(row=0, column=2, padx=(8, 8), pady=14)

        ctk.CTkButton(bar, text="Advanced...", width=90, height=38,
                      fg_color=C["bg_card"], hover_color=C["bg_card_hover"],
                      border_width=1, border_color=C["border"],
                      text_color=C["text_secondary"], corner_radius=8,
                      font=CTkFont(family="Segoe UI", size=12),
                      command=lambda: self.open_new_download_dialog(
                          self._url_var.get())).grid(row=0, column=3, padx=(0, 16), pady=14)

    def _build_list(self):
        lf = ctk.CTkFrame(self.root, fg_color=C["bg_dark"], corner_radius=0)
        lf.grid(row=2, column=0, sticky="nsew")
        lf.grid_columnconfigure(0, weight=1)
        lf.grid_rowconfigure(0, weight=1)
        self._scroll = ctk.CTkScrollableFrame(
            lf, fg_color=C["bg_dark"],
            scrollbar_button_color=C["border"],
            scrollbar_button_hover_color=C["text_muted"],
            corner_radius=0)
        self._scroll.grid(row=0, column=0, sticky="nsew")
        self._scroll.grid_columnconfigure(0, weight=1)
        self._empty_lbl = ctk.CTkLabel(
            self._scroll,
            text="No downloads yet.\nPress  + New Download  or paste a URL above.",
            font=CTkFont(family="Segoe UI", size=14),
            text_color=C["text_muted"], justify="center")

    def _build_status_bar(self):
        sb = ctk.CTkFrame(self.root, fg_color=C["bg_panel"], height=28, corner_radius=0)
        sb.grid(row=3, column=0, sticky="ew")
        sb.grid_propagate(False)
        sb.grid_columnconfigure(1, weight=1)
        self._lbl_aria2 = ctk.CTkLabel(sb, text="aria2  Connecting...",
                                        font=CTkFont(family="Segoe UI", size=10),
                                        text_color=C["text_muted"])
        self._lbl_aria2.grid(row=0, column=0, padx=12, pady=4, sticky="w")
        self._lbl_counts = ctk.CTkLabel(sb, text="",
                                         font=CTkFont(family="Segoe UI", size=10),
                                         text_color=C["text_muted"])
        self._lbl_counts.grid(row=0, column=2, padx=12, pady=4, sticky="e")
        ctk.CTkLabel(sb, text=f"{APP_NAME} {APP_VERSION}",
                     font=CTkFont(family="Segoe UI", size=10),
                     text_color=C["text_muted"]).grid(row=0, column=3, padx=12, pady=4, sticky="e")

    def rebuild_list(self):
        existing = {i.uid for i in self.dm.items}
        for uid in list(self._cards.keys()):
            if uid not in existing:
                self._cards[uid].destroy(); del self._cards[uid]
        if not self.dm.items:
            self._empty_lbl.grid(row=0, column=0, pady=80)
        else:
            self._empty_lbl.grid_forget()
        for idx, item in enumerate(self.dm.items):
            if item.uid in self._cards:
                card = self._cards[item.uid]
                card.item = item; card.refresh()
            else:
                card = DownloadCard(self._scroll, item, self.dm)
                self._cards[item.uid] = card
            card.grid(row=idx, column=0, padx=12, pady=(0, 8), sticky="ew")
        total  = len(self.dm.items)
        active = sum(1 for i in self.dm.items if i.status == "Downloading")
        done   = sum(1 for i in self.dm.items if i.status == "Completed")
        self._lbl_counts.configure(
            text=f"Total: {total}  |  Downloading: {active}  |  Completed: {done}")
        if self.dm.aria2.available:
            self._lbl_aria2.configure(text="aria2  Connected", text_color=C["success"])
        else:
            self._lbl_aria2.configure(text="aria2  Offline",   text_color=C["error"])

    def _schedule_gui_poll(self):
        self.dm.consume_updates(self._on_complete)
        self.rebuild_list()
        self.root.after(POLL_INTERVAL_MS, self._schedule_gui_poll)

    def _on_complete(self, item):
        send_notification(APP_NAME, f"Download complete: {item.filename}")

    def _quick_start(self):
        url = self._url_var.get().strip()
        if not url: return
        if not is_valid_url(url):
            self.show_error("Invalid URL", "Please enter a valid http:// or https:// URL.")
            return
        self._url_var.set("")
        self._start_for_url(url)

    def _start_for_url(self, url, username="", password="", ua="", ref=""):
        if not aria2c_in_path():
            self._show_aria2_warning(); return
        if not self.dm.aria2.available:
            self.show_error("aria2 Offline",
                            "aria2 is not running. Please restart the application."); return
        folder = self.settings.get("download_folder", str(Path.home() / "Downloads"))
        item = DownloadItem(url=url, save_dir=folder, username=username,
                            password=password, user_agent=ua, referer=ref)
        if item.file_path.exists():
            self._show_duplicate_dialog(item); return
        try: item.save_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.show_error("Folder Error", str(e)); return
        self.dm.add_item(item)

    def open_new_download_dialog(self, initial_url=""):
        url = initial_url or self._url_var.get().strip()
        NewDownloadDialog(self.root, self.settings, initial_url=url,
                          callback=self._on_new_confirmed)

    def _on_new_confirmed(self, item):
        if not aria2c_in_path():
            self._show_aria2_warning(); return
        if not self.dm.aria2.available:
            self.show_error("aria2 Offline", "aria2 is not running."); return
        if item.file_path.exists():
            self._show_duplicate_dialog(item); return
        try: item.save_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.show_error("Folder Error", str(e)); return
        self.dm.add_item(item)
        self._url_var.set("")

    def _show_duplicate_dialog(self, item):
        d = ctk.CTkToplevel(self.root)
        d.title("File Already Exists")
        d.geometry("460x200")
        d.configure(fg_color=C["bg_dark"])
        d.transient(self.root); d.grab_set(); d.lift()
        ctk.CTkLabel(d, text=f"File already exists:\n{item.filename}",
                     font=CTkFont(family="Segoe UI", size=13),
                     text_color=C["text_primary"], justify="center").pack(pady=(24, 16))
        btns = ctk.CTkFrame(d, fg_color="transparent")
        btns.pack()
        bc = dict(height=36, corner_radius=8, font=CTkFont(family="Segoe UI", size=12))

        def _overwrite():
            d.destroy()
            try: item.file_path.unlink(missing_ok=True)
            except Exception: pass
            self.dm.add_item(item)

        def _rename():
            d.destroy()
            stem, suffix, n = item.file_path.stem, item.file_path.suffix, 1
            while (item.save_dir / f"{stem} ({n}){suffix}").exists(): n += 1
            item.filename = f"{stem} ({n}){suffix}"
            self.dm.add_item(item)

        ctk.CTkButton(btns, text="Overwrite", width=110,
                      fg_color=C["error"], hover_color="#C0392B",
                      text_color="white", command=_overwrite, **bc).grid(row=0, column=0, padx=8)
        ctk.CTkButton(btns, text="Rename", width=110,
                      fg_color=C["accent"], hover_color=C["accent_hover"],
                      text_color="white", command=_rename, **bc).grid(row=0, column=1, padx=8)
        ctk.CTkButton(btns, text="Cancel", width=110,
                      fg_color=C["bg_card"], hover_color=C["bg_card_hover"],
                      border_width=1, border_color=C["border"],
                      text_color=C["text_secondary"],
                      command=d.destroy, **bc).grid(row=0, column=2, padx=8)

    def _show_aria2_warning(self):
        d = ctk.CTkToplevel(self.root)
        d.title("aria2 Required"); d.geometry("520x280")
        d.configure(fg_color=C["bg_dark"])
        d.transient(self.root); d.grab_set(); d.lift()
        ctk.CTkLabel(d, text="aria2 Not Found",
                     font=CTkFont(family="Segoe UI", size=16, weight="bold"),
                     text_color=C["warning"]).pack(pady=(24, 8))
        ctk.CTkLabel(d,
                     text=("aria2c is required but was not found on your PATH.\n\n"
                           "Install it using one of these methods:\n\n"
                           "  Chocolatey:  choco install aria2 -y\n"
                           "  Scoop:       scoop install aria2\n"
                           "  Manual:      Download from the official releases page"),
                     font=CTkFont(family="Segoe UI", size=12),
                     text_color=C["text_secondary"], justify="left").pack(padx=28, pady=8, anchor="w")
        br = ctk.CTkFrame(d, fg_color="transparent"); br.pack(pady=12)
        ctk.CTkButton(br, text="Open Download Page", width=180,
                      fg_color=C["accent"], hover_color=C["accent_hover"],
                      text_color="white", height=36, corner_radius=8,
                      font=CTkFont(family="Segoe UI", size=12),
                      command=lambda: webbrowser.open(
                          "https://github.com/aria2/aria2/releases")).grid(row=0, column=0, padx=8)
        ctk.CTkButton(br, text="Close", width=90,
                      fg_color=C["bg_card"], hover_color=C["bg_card_hover"],
                      border_width=1, border_color=C["border"],
                      text_color=C["text_secondary"], height=36, corner_radius=8,
                      command=d.destroy).grid(row=0, column=1, padx=8)

    def open_settings(self):
        SettingsWindow(self.root, self.settings)

    def show_error(self, title, message):
        import tkinter.messagebox as mb
        mb.showerror(title, message, parent=self.root)
        log.error("%s: %s", title, message)

    def _start_clipboard_monitor(self):
        if self.settings.get("clipboard_monitor", True):
            self._clipboard_monitor = ClipboardMonitor(self.root, self._on_clipboard_url)
            self._clipboard_monitor.start()

    def _on_clipboard_url(self, url):
        if self._clipboard_dialog_open: return
        self._clipboard_dialog_open = True
        d = ctk.CTkToplevel(self.root)
        d.title("Download URL Detected"); d.geometry("500x190")
        d.configure(fg_color=C["bg_dark"])
        d.transient(self.root); d.lift()
        ctk.CTkLabel(d, text="Download URL detected in clipboard",
                     font=CTkFont(family="Segoe UI", size=13, weight="bold"),
                     text_color=C["text_primary"]).pack(pady=(20, 6))
        url_short = url[:67] + "..." if len(url) > 70 else url
        ctk.CTkLabel(d, text=url_short, font=CTkFont(family="Segoe UI", size=11),
                     text_color=C["accent"]).pack(pady=(0, 14))
        br = ctk.CTkFrame(d, fg_color="transparent"); br.pack()

        def _dl():
            d.destroy(); self._clipboard_dialog_open = False
            self._start_for_url(url)

        def _ignore():
            d.destroy(); self._clipboard_dialog_open = False

        ctk.CTkButton(br, text="Download", width=110, height=36,
                      fg_color=C["accent"], hover_color=C["accent_hover"],
                      text_color="white", corner_radius=8,
                      command=_dl).grid(row=0, column=0, padx=8)
        ctk.CTkButton(br, text="Ignore", width=100, height=36,
                      fg_color=C["bg_card"], hover_color=C["bg_card_hover"],
                      border_width=1, border_color=C["border"],
                      text_color=C["text_secondary"], corner_radius=8,
                      command=_ignore).grid(row=0, column=1, padx=8)
        d.protocol("WM_DELETE_WINDOW", _ignore)



    def _bind_shortcuts(self):
        self.root.bind("<Control-n>", lambda e: self.open_new_download_dialog())
        self.root.bind("<Control-N>", lambda e: self.open_new_download_dialog())
        self.root.bind("<Control-v>", lambda e: self._paste_url())
        self.root.bind("<Control-V>", lambda e: self._paste_url())
        self.root.bind("<Return>",    lambda e: self._quick_start())

    def _paste_url(self):
        try:
            text = self.root.clipboard_get().strip()
            if is_valid_url(text):
                self._url_var.set(text); self._url_e.focus_set()
        except Exception: pass

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class Application:
    def __init__(self):
        self.settings = SettingsManager()
        self.history  = HistoryManager()
        ctk.set_appearance_mode(self.settings.get("theme", "dark"))
        ctk.set_default_color_theme("blue")
        self.root = ctk.CTk()
        self.aria2  = Aria2Manager(self.settings)
        self.dm     = DownloadManager(self.settings, self.history, self.aria2,
                                      on_update=lambda: None)
        self.window = MainWindow(self.root, self.dm, self.settings)
        self.dm.on_update = self.window.rebuild_list
        threading.Thread(target=self._delayed_startup, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        log.info("GUI ready.")

    def _delayed_startup(self):
        log.info("Starting aria2...")
        ok = self.aria2.start()
        log.info("aria2 %s.", "started" if ok else "failed to start")
        self.dm.restore_from_history()
        self.dm.start_polling()
        if ok:
            for item in self.dm.items:
                if item.status == "Paused" and item.gid:
                    self.aria2.resume(item.gid)
        self.root.after(0, self.window.rebuild_list)

    def run(self):
        self.root.mainloop()

    def _on_close(self):
        log.info("Application closing.")
        self.dm.stop_polling()
        for item in self.dm.items: self.history.upsert(item)
        self.aria2.stop()
        self.root.destroy()
        log.info("Shutdown complete.")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    Application().run()
