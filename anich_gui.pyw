# -*- coding: utf-8 -*-
"""AniCh 动漫下载器 GUI(Codex 风格现代深色界面,纯标准库 tkinter)"""
import ctypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import anich_dl as api

if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")

BROWSER_UA = api.BROWSER_UA
REFERER = "https://anich.emmmm.eu.org/"

BG = "#1f1f1f"
PANEL = "#262626"
FIELD = "#2b2b2b"
BUTTON = "#333333"
BUTTON_HOVER = "#414141"
BORDER = "#3c3c3c"
FG = "#e5e5e5"
SUB = "#9c9c9c"
ACCENT = "#4da3ff"
ACCENT_HOVER = "#66b1ff"
ACCENT_DARK = "#3b8fd9"
GREEN = "#4ade80"
RED = "#f87171"
FONT = "Microsoft YaHei UI"


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(name)).strip() or "output"


def place_file(tmp_path, out_path):
    """把已下载的临时文件移动到最终文件名;自动清除只读属性,失败时给出明确原因。"""
    api.make_writable(out_path)
    try:
        os.replace(tmp_path, out_path)
    except PermissionError as exc:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise RuntimeError(
            f"无法写入文件: {out_path}\n"
            f"可能原因:文件正被占用(播放器/杀毒扫描)、只读、或无权限。\n"
            f"请关闭占用该文件的程序,或换个保存目录后重试。({exc})")



def enable_dark_titlebar(root):
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


class DownloadWorker(threading.Thread):
    """后台下载线程:网络操作都在这里,通过 queue 回传进度到 GUI。"""

    def __init__(self, q, bangumi_id, episodes, line_index, out_dir):
        super().__init__(daemon=True)
        self.q = q
        self.bangumi_id = bangumi_id
        self.episodes = episodes
        self.line_index = line_index
        self.out_dir = out_dir
        self.stop = threading.Event()

    def log(self, text):
        self.q.put(("log", text))

    def prog(self, fraction, text=""):
        self.q.put(("prog", fraction, text))

    def run(self):
        try:
            total = len(self.episodes)
            for idx, (sort, title) in enumerate(self.episodes, 1):
                if self.stop.is_set():
                    self.log("已取消。")
                    break
                self.log(f"[{idx}/{total}] 第 {sort} 集《{title}》获取线路...")
                try:
                    items = api.load_vod_items(self.bangumi_id, sort)
                except Exception as exc:
                    self.log(f"  获取线路失败: {exc},跳过本集。")
                    continue
                if not items:
                    self.log("  无可用线路,跳过本集。")
                    continue
                name = sanitize(title) if title else f"E{sort}"
                ok = False
                order = list(range(len(items)))
                order = order[self.line_index:] + order[:self.line_index]
                for attempt, li in enumerate(order, 1):
                    if self.stop.is_set():
                        break
                    item = items[li]
                    url = item.get("url") or ""
                    if not url.startswith("http"):
                        continue
                    label = f"{item.get('caption') or ''} {item.get('type') or ''}".strip()
                    self.log(f"  尝试线路 {li + 1}: {label or '-'} | {url[:70]}...")
                    try:
                        if ".m3u8" in url:
                            self._download_hls(url, self.out_dir, name)
                        else:
                            self._download_direct(url, os.path.join(self.out_dir, name + ".mp4"))
                        ok = True
                        self.log(f"  第 {sort} 集下载完成 -> {name}")
                        break
                    except Exception as exc:
                        self.log(f"  线路 {li + 1} 失败: {exc}(自动换下一条 {attempt}/{len(order)})")
                if not ok and not self.stop.is_set():
                    self.log(f"  第 {sort} 集所有线路均失败,已跳过。")
            self.q.put(("done",))
        except Exception as exc:
            self.q.put(("error", f"下载线程异常: {exc}"))

    def _download_direct(self, url, out_path):
        headers = {"User-Agent": BROWSER_UA, "Referer": REFERER}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=45) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if ctype.startswith("text/") or ctype.startswith("application/json"):
                raise RuntimeError("返回的不是视频数据(可能是防盗链页面)")
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            tmp_path = out_path + ".part"
            try:
                with open(tmp_path, "wb") as fh:
                    while True:
                        if self.stop.is_set():
                            return
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if total:
                            self.prog(done / total, f"{done / 1048576:.1f} / {total / 1048576:.1f} MB")
                if total and done < total:
                    raise RuntimeError("下载不完整")
                place_file(tmp_path, out_path)
            except Exception:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
                raise

    def _download_hls(self, url, out_dir, name):
        yt = shutil.which("yt-dlp")
        if not yt:
            raise RuntimeError("未安装 yt-dlp,无法下载 m3u8(请 pip install yt-dlp,或选 mp4 线路)")
        out_tmpl = os.path.join(out_dir, name + ".%(ext)s")
        cmd = [yt, "--newline", "-o", out_tmpl,
               "--add-header", "User-Agent: " + BROWSER_UA,
               "--referer", REFERER, url]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=api.NO_WINDOW_FLAGS)
        assert proc.stdout is not None
        for line in proc.stdout:
            if self.stop.is_set():
                proc.terminate()
                break
            m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
            if m:
                self.prog(float(m.group(1)) / 100, line.strip())
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"yt-dlp 失败(退出码 {proc.returncode})")


class App:
    def __init__(self, root):
        self.root = root
        root.title("AniCh 动漫下载器")
        root.geometry("1180x780")
        root.minsize(980, 660)
        root.configure(bg=BG)
        enable_dark_titlebar(root)

        self.results = []
        self.episodes = []
        self.lines = []
        self.q = queue.Queue()
        self.worker = None

        self._setup_style()
        self._build_ui()
        self.root.after(120, self.poll_queue)

    def _setup_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=BG, foreground=FG, font=(FONT, 11))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG, font=(FONT, 11))
        style.configure("Sub.TLabel", background=BG, foreground=SUB, font=(FONT, 10))
        style.configure("Title.TLabel", background=BG, foreground=FG, font=(FONT, 19, "bold"))

        style.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        padding=9, relief="flat")
        style.map("TEntry",
                  bordercolor=[("focus", ACCENT), ("hover", "#4a4a4a")],
                  lightcolor=[("focus", ACCENT), ("hover", "#4a4a4a")],
                  darkcolor=[("focus", ACCENT), ("hover", "#4a4a4a")])

        style.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG,
                        arrowcolor=SUB, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        padding=8, relief="flat")
        style.map("TCombobox",
                  fieldbackground=[("readonly", FIELD)],
                  foreground=[("readonly", FG)],
                  selectbackground=[("readonly", FIELD)],
                  selectforeground=[("readonly", FG)],
                  bordercolor=[("focus", ACCENT), ("hover", "#4a4a4a")],
                  lightcolor=[("focus", ACCENT), ("hover", "#4a4a4a")],
                  darkcolor=[("focus", ACCENT), ("hover", "#4a4a4a")])

        style.configure("TButton", background=BUTTON, foreground=FG, relief="flat",
                        bordercolor=BUTTON, lightcolor=BUTTON, darkcolor=BUTTON,
                        padding=(18, 9), focuscolor=BUTTON)
        style.map("TButton",
                  background=[("active", BUTTON_HOVER), ("pressed", "#2b2b2b")],
                  foreground=[("disabled", "#6a6a6a")],
                  bordercolor=[("active", BUTTON_HOVER)],
                  lightcolor=[("active", BUTTON_HOVER)],
                  darkcolor=[("active", BUTTON_HOVER)])

        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", relief="flat",
                        bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                        padding=(20, 10), font=(FONT, 11, "bold"))
        style.map("Accent.TButton",
                  background=[("active", ACCENT_HOVER), ("pressed", ACCENT_DARK)],
                  bordercolor=[("active", ACCENT_HOVER)],
                  lightcolor=[("active", ACCENT_HOVER)],
                  darkcolor=[("active", ACCENT_HOVER)])

        style.configure("TLabelframe", background=BG, bordercolor=BORDER, relief="solid")
        style.configure("TLabelframe.Label", background=BG, foreground=FG, font=(FONT, 12, "bold"))

        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                        bordercolor=PANEL, rowheight=34, font=(FONT, 11))
        style.configure("Treeview.Heading", background="#2e2e2e", foreground=SUB,
                        bordercolor="#2e2e2e", font=(FONT, 10, "bold"), padding=8, relief="flat")
        style.map("Treeview",
                  background=[("selected", "#365880")],
                  foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading", background=[("active", "#343434")])

        style.configure("TProgressbar", background=ACCENT, troughcolor=PANEL,
                        bordercolor=PANEL, lightcolor=ACCENT, darkcolor=ACCENT, thickness=10)
        style.configure("TScrollbar", background="#3a3a3a", troughcolor=BG,
                        bordercolor=BG, arrowcolor=SUB, lightcolor="#3a3a3a", darkcolor="#3a3a3a")
        style.map("TScrollbar", background=[("active", "#4a4a4a")])

        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#365880")
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 11))

    def _build_ui(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=18, pady=(16, 10))
        tk.Label(header, text="AniCh 动漫下载器", bg=BG, fg=FG, font=(FONT, 19, "bold")).pack(anchor="w")
        tk.Label(header, text="搜索番剧 · 选择线路 · 一键批量下载", bg=BG, fg=SUB, font=(FONT, 10)).pack(anchor="w", pady=(3, 0))

        searchbar = ttk.Frame(self.root, padding=(18, 0))
        searchbar.pack(fill="x")
        self.kw = ttk.Entry(searchbar)
        self.kw.pack(side="left", fill="x", expand=True, ipady=2)
        self.kw.bind("<Return>", lambda e: self.search())
        ttk.Button(searchbar, text="搜索", style="Accent.TButton", command=self.search).pack(side="left", padx=(10, 0))
        ttk.Button(searchbar, text="获取剧集", command=self.load_episodes).pack(side="left", padx=(10, 0))

        mid = ttk.Panedwindow(self.root, orient="horizontal")
        mid.pack(fill="both", expand=True, padx=18, pady=(12, 10))

        lf_res = ttk.Labelframe(mid, text="  搜索结果(双击加载剧集)  ")
        lf_eps = ttk.Labelframe(mid, text="  剧集(可 Ctrl/Shift 多选)  ")
        mid.add(lf_res, weight=1)
        mid.add(lf_eps, weight=1)

        self.tree_res = ttk.Treeview(lf_res, columns=("title", "id", "eps", "status"), show="headings", selectmode="browse")
        for col, text, width, anchor in (
            ("title", "标题", 260, "w"),
            ("id", "ID", 70, "center"),
            ("eps", "集数", 80, "center"),
            ("status", "状态", 70, "center"),
        ):
            self.tree_res.heading(col, text=text)
            self.tree_res.column(col, width=width, anchor=anchor, stretch=(col == "title"))
        rs = ttk.Scrollbar(lf_res, orient="vertical", command=self.tree_res.yview)
        self.tree_res.configure(yscrollcommand=rs.set)
        self.tree_res.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(6, 10))
        rs.pack(side="left", fill="y", pady=(6, 10))
        self.tree_res.bind("<Double-Button-1>", lambda e: self.load_episodes())

        self.tree_eps = ttk.Treeview(lf_eps, columns=("sort", "title"), show="headings", selectmode="extended")
        self.tree_eps.heading("sort", text="集数")
        self.tree_eps.heading("title", text="标题")
        self.tree_eps.column("sort", width=80, anchor="center", stretch=False)
        self.tree_eps.column("title", width=300, anchor="w", stretch=True)
        es = ttk.Scrollbar(lf_eps, orient="vertical", command=self.tree_eps.yview)
        self.tree_eps.configure(yscrollcommand=es.set)
        self.tree_eps.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(6, 10))
        es.pack(side="left", fill="y", pady=(6, 10))

        linef = ttk.Labelframe(self.root, text="  线路选择  ")
        linef.pack(fill="x", padx=18, pady=(0, 10))
        self.cmb = ttk.Combobox(linef, state="readonly")
        self.cmb.pack(side="left", fill="x", expand=True, padx=10, pady=10)
        ttk.Button(linef, text="刷新线路", command=self.load_lines).pack(side="left", padx=(0, 10))

        ctrl = ttk.Frame(self.root, padding=(18, 0))
        ctrl.pack(fill="x")
        ttk.Button(ctrl, text="全选", command=lambda: self.tree_eps.selection_set(self.tree_eps.get_children())).pack(side="left")
        ttk.Button(ctrl, text="清空", command=lambda: self.tree_eps.selection_remove(self.tree_eps.get_children())).pack(side="left", padx=(10, 0))
        ttk.Label(ctrl, text="保存到:", style="Sub.TLabel").pack(side="left", padx=(22, 0))
        self.dir = ttk.Entry(ctrl)
        self.dir.insert(0, os.getcwd())
        self.dir.pack(side="left", fill="x", expand=True, padx=10)
        ttk.Button(ctrl, text="浏览...", command=self.pick_dir).pack(side="left")

        progf = ttk.Frame(self.root, padding=(18, 12))
        progf.pack(fill="x")
        self.bar = ttk.Progressbar(progf, maximum=1000)
        self.bar.pack(side="left", fill="x", expand=True)
        ttk.Button(progf, text="开始下载", style="Accent.TButton", command=self.start_download).pack(side="left", padx=(12, 0))
        ttk.Button(progf, text="取消", command=self.cancel).pack(side="left", padx=(10, 0))
        self.status = ttk.Label(progf, text="就绪", style="Sub.TLabel", width=26, anchor="e")
        self.status.pack(side="left", padx=(12, 0))

        logf = ttk.Labelframe(self.root, text="  日志  ")
        logf.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self.logbox = tk.Text(logf, height=8, state="disabled", wrap="word", relief="flat",
                              bg="#1b1b1b", fg="#d4d4d4", insertbackground=FG,
                              font=("Consolas", 10), padx=12, pady=10)
        ls = ttk.Scrollbar(logf, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=ls.set)
        self.logbox.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(6, 10))
        ls.pack(side="left", fill="y", pady=(6, 10))
        self.logbox.tag_configure("err", foreground=RED)
        self.logbox.tag_configure("ok", foreground=GREEN)
        self.logbox.tag_configure("dim", foreground=SUB)
        self.logbox.tag_configure("accent", foreground=ACCENT_HOVER)

    def append_log(self, text):
        self.logbox.configure(state="normal")
        if "失败" in text or "错误" in text or "跳过" in text:
            tag = "err"
        elif "完成" in text or "任务结束" in text:
            tag = "ok"
        elif text.startswith("  尝试线路") or text.startswith("["):
            tag = "dim"
        else:
            tag = None
        self.logbox.insert("end", text + "\n", tag or ())
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def search(self):
        kw = self.kw.get().strip()
        if not kw:
            messagebox.showwarning("提示", "请输入番剧名。")
            return
        self.append_log(f"搜索: {kw}")
        threading.Thread(target=self._search_worker, args=(kw,), daemon=True).start()

    def _search_worker(self, kw):
        try:
            query = urllib.parse.urlencode({"keyword": kw, "skip": 0})
            items, prev, nxt = api.parse_bangumi_list(api.api_get("/bangumi/search?" + query))
            self.q.put(("results", items))
        except Exception as exc:
            self.q.put(("error", f"搜索失败: {exc}"))

    def load_episodes(self):
        sel = self.tree_res.selection()
        if not sel:
            messagebox.showwarning("提示", "请先在左侧选中一个番剧。")
            return
        bangumi_id = self.results[int(sel[0])]["id"]
        self.append_log(f"加载剧集列表: ID {bangumi_id}")
        threading.Thread(target=self._episodes_worker, args=(bangumi_id,), daemon=True).start()

    def _episodes_worker(self, bangumi_id):
        try:
            items = api.parse_bangumi_episodes(api.api_get(f"/bangumi/episodes/{bangumi_id}"))
            items.sort(key=lambda x: (x.get("sort") is None, x.get("sort") or 0))
            self.q.put(("episodes", items))
        except Exception as exc:
            self.q.put(("error", f"获取剧集失败: {exc}"))

    def load_lines(self):
        eps_sel = self.tree_eps.selection()
        if not eps_sel:
            messagebox.showwarning("提示", "请先在右侧选中一集。")
            return
        res_sel = self.tree_res.selection()
        if not res_sel:
            messagebox.showwarning("提示", "请先在左侧选中番剧。")
            return
        bangumi_id = self.results[int(res_sel[0])]["id"]
        sort = self.episodes[int(eps_sel[0])][0]
        self.append_log(f"获取第 {sort} 集的线路...")
        threading.Thread(target=self._lines_worker, args=(bangumi_id, sort), daemon=True).start()

    def _lines_worker(self, bangumi_id, sort):
        try:
            items = api.load_vod_items(bangumi_id, sort)
            self.q.put(("lines", items))
        except Exception as exc:
            self.q.put(("error", f"获取线路失败: {exc}"))

    def pick_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.dir.get() or os.getcwd())
        if chosen:
            self.dir.delete(0, "end")
            self.dir.insert(0, chosen)

    def start_download(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "正在下载中,请先等待完成或点取消。")
            return
        res_sel = self.tree_res.selection()
        eps_sel = self.tree_eps.selection()
        if not res_sel:
            messagebox.showwarning("提示", "请先在左侧选中番剧。")
            return
        if not eps_sel:
            messagebox.showwarning("提示", "请先在右侧选择要下载的集数(可多选)。")
            return
        out_dir = self.dir.get().strip() or os.getcwd()
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("错误", f"无法创建输出目录: {exc}")
            return
        # 提前验证目录可写,避免下载时每条线路都报 Permission denied
        try:
            probe = os.path.join(out_dir, f".write_test_{os.getpid()}.tmp")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
        except Exception as exc:
            messagebox.showerror(
                "错误",
                f"输出目录不可写: {out_dir}\n{exc}\n\n"
                f"可能原因:目录只读/被占用/无权限,或磁盘已满。\n请换个保存位置后重试。")
            return
        line_index = 0
        if self.lines:
            try:
                line_index = max(0, self.cmb.current())
            except Exception:
                line_index = 0
        bangumi_id = self.results[int(res_sel[0])]["id"]
        eps = [self.episodes[int(i)] for i in eps_sel]
        self.append_log(f"开始下载 {len(eps)} 集(首选线路 {line_index + 1},失败自动换线)。")
        self.status.configure(text="下载中...")
        self.worker = DownloadWorker(self.q, bangumi_id, eps, line_index, out_dir)
        self.worker.start()

    def cancel(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop.set()
            self.append_log("正在取消...(当前文件完成后停止)")
        else:
            messagebox.showinfo("提示", "当前没有下载任务。")

    def poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.append_log(msg[1])
                elif kind == "prog":
                    self.bar["value"] = msg[1] * 1000
                    if msg[2]:
                        self.status.configure(text=msg[2][:36])
                elif kind == "results":
                    items = msg[1]
                    self.results = items
                    res_children = self.tree_res.get_children()
                    if res_children:
                        self.tree_res.delete(*res_children)
                    for i, it in enumerate(items):
                        self.tree_res.insert("", "end", iid=str(i), values=(
                            it.get("title") or "", it.get("id"), f"{it.get('episode')}/{it.get('episodes_total')}",
                            it.get("status") or "-"))
                    if not items:
                        self.append_log("没有找到结果。")
                elif kind == "episodes":
                    items = msg[1]
                    self.episodes = [(it.get("sort"), it.get("title") or "") for it in items]
                    eps_children = self.tree_eps.get_children()
                    if eps_children:
                        self.tree_eps.delete(*eps_children)
                    for i, (sort, title) in enumerate(self.episodes):
                        self.tree_eps.insert("", "end", iid=str(i), values=(f"第 {sort} 集", title))
                    self.append_log(f"共 {len(self.episodes)} 集。")
                elif kind == "lines":
                    items = msg[1]
                    self.lines = items
                    entries = []
                    for i, it in enumerate(items, 1):
                        url = it.get("url") or ""
                        host = ""
                        m = re.match(r"https?://([^/]+)", url)
                        if m:
                            host = m.group(1)
                        kind2 = "m3u8" if ".m3u8" in url else "mp4"
                        label = f"{it.get('caption') or ''} {it.get('type') or ''}".strip()
                        entries.append(f"[{i}] {kind2} | {label or '-'} | {host}")
                    self.cmb["values"] = entries
                    self.cmb.current(0)
                    self.append_log(f"共 {len(items)} 条线路,默认选第 1 条,失败自动换线。")
                elif kind == "done":
                    self.bar["value"] = 0
                    self.status.configure(text="全部完成")
                    self.append_log("任务结束。")
                elif kind == "error":
                    self.bar["value"] = 0
                    self.status.configure(text="出错")
                    self.append_log(msg[1])
                    messagebox.showerror("错误", msg[1])
        except queue.Empty:
            pass
        self.root.after(120, self.poll_queue)


def main():
    try:
        enable_dpi_awareness()
        root = tk.Tk()
        App(root)
        root.mainloop()
    except Exception:
        import traceback
        crash_log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash.log")
        with open(crash_log, "w", encoding="utf-8") as fh:
            fh.write(traceback.format_exc())
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("启动失败", "程序遇到错误,详情已写入 crash.log:\n\n" + traceback.format_exc()[-600:])
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
