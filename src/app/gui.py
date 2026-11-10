# -*- coding: utf-8 -*-
"""
图形界面（tkinter）

结构：
    顶部  工具条：预设 / 添加文件 / 移除 / 清空
    左侧  文件列表 + 页码范围
    右侧  参数 Notebook：打印效果 / 扫描效果 / 版式与输出
    底部  进度条 + 状态 + 开始/取消 + 打开输出目录

线程模型：tkinter 只能在主线程操作，所以渲染放在 worker 线程，
通过 queue + root.after 把进度回传主线程，避免界面卡死。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image

from . import engine, logsetup, paper, paths
from .config import (BUILTIN_PRESETS, OUTPUT_FORMATS, ORIENTATIONS,
                     PAGE_VARIATIONS, PRESET_ORDER, PRINT_MODES,
                     PRINT_PAPER_TYPES, SCALE_MODES, LayoutParams,
                     OutputParams, PrintParams, RenderJob, ScanParams)
from .logsetup import get_logger
from .paths import config_dir, default_output_dir, resource_path
from .version import APP_NAME, BUILD_DATE, __version__

log = get_logger("app.gui")

STATE_FILE = "last_state.json"

PAD = 6


# --------------------------------------------------------------------------- #
# 控件辅助
# --------------------------------------------------------------------------- #
class SliderRow:
    """一行「标签 + 滑块 + 数值」，数值实时回显"""

    def __init__(self, parent, row: int, label: str, var, lo: float, hi: float,
                 fmt: str = "{:.2f}", width_label: int = 16, tip: str = "",
                 on_change=None):
        self.var = var
        self.fmt = fmt
        self.tip = tip
        self.on_change = on_change
        lbl = ttk.Label(parent, text=label, width=width_label, anchor="w")
        lbl.grid(row=row, column=0, sticky="w", padx=(0, 4), pady=2)
        self.scale = ttk.Scale(parent, from_=lo, to=hi, orient="horizontal",
                               variable=var, command=self._on_move)
        self.scale.grid(row=row, column=1, sticky="ew", pady=2)
        self.value_lbl = ttk.Label(parent, text=self._text(), width=7, anchor="e")
        self.value_lbl.grid(row=row, column=2, sticky="e", padx=(4, 0), pady=2)
        if tip:
            lbl.configure(text=label)
            _Tooltip(lbl, tip)

    def _text(self) -> str:
        try:
            return self.fmt.format(float(self.var.get()))
        except Exception:
            return "-"

    def _on_move(self, _evt=None):
        # 先刷新数值回显，再交给外部回调 —— 否则外部覆盖 command 后数字就不动了
        self.value_lbl.configure(text=self._text())
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass

    def refresh(self):
        self.value_lbl.configure(text=self._text())


class _Tooltip:
    """鼠标悬停提示（原生 tkinter 没有，自己搓一个）"""

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _e=None):
        if self.tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 20
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            tk.Label(self.tip, text=self.text, justify="left", background="#ffffe1",
                     relief="solid", borderwidth=1, wraplength=380,
                     font=("Microsoft YaHei UI", 9)).pack(ipadx=4, ipady=2)
        except Exception:
            self.tip = None

    def _hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME}  v{__version__}")
        self._apply_initial_geometry()
        self.minsize(980, 640)

        self.files: list[Path] = []
        self.page_counts: dict[str, int] = {}
        self.worker: threading.Thread | None = None
        self.cancel_flag = threading.Event()
        self.msg_queue: queue.Queue = queue.Queue()
        self.output_paths: list[str] = []
        self._sliders: list[SliderRow] = []

        self._setup_style()
        self._set_icon()
        self._build_vars()
        self._build_ui()
        self._load_state()

        logsetup.install_tk_crash_hook(self)
        logsetup.register_crash_listener(self._on_crash)
        self.after(120, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        log.info("界面初始化完成")

    # ---------------- 样式 / 图标 ----------------
    def _apply_initial_geometry(self):
        """按屏幕可用区域决定初始窗口大小，避免在小屏上被裁到屏幕外"""
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
        except Exception:
            sw, sh = 1440, 900
        w = max(980, min(1320, sw - 60))
        h = max(640, min(820, sh - 90))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 30)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _setup_style(self):
        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except Exception:
                    continue
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Run.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Treeview", rowheight=24)

    def _set_icon(self):
        try:
            ico = resource_path("assets", "icon.ico")
            if ico.exists():
                self.iconbitmap(default=str(ico))
        except Exception:
            pass

    # ---------------- 变量 ----------------
    def _build_vars(self):
        # --- 打印 ---
        self.v_mode = tk.StringVar(value="mono_laser")
        self.v_papertype = tk.StringVar(value=PRINT_PAPER_TYPES["plain"])
        self.v_density = tk.DoubleVar(value=1.00)
        self.v_pcontrast = tk.DoubleVar(value=1.00)
        self.v_spread = tk.DoubleVar(value=0.35)
        self.v_grain = tk.DoubleVar(value=0.30)
        self.v_fog = tk.DoubleVar(value=0.10)
        self.v_halftone = tk.DoubleVar(value=0.25)
        self.v_misreg = tk.DoubleVar(value=0.50)
        self.v_tac = tk.DoubleVar(value=280)

        # --- 扫描 ---
        self.v_pvary = tk.StringVar(value=PAGE_VARIATIONS["uniform"])
        self.v_paperbase = tk.DoubleVar(value=246)
        self.v_cloud = tk.DoubleVar(value=0.35)
        self.v_noise = tk.DoubleVar(value=0.30)
        self.v_dust = tk.DoubleVar(value=0.20)
        self.v_streak = tk.DoubleVar(value=0.0)
        self.v_blur = tk.DoubleVar(value=0.45)
        self.v_scontrast = tk.DoubleVar(value=1.07)
        self.v_bright = tk.DoubleVar(value=1.00)
        self.v_skew = tk.DoubleVar(value=0.0)
        self.v_skew_random = tk.BooleanVar(value=True)
        self.v_skew_amt = tk.DoubleVar(value=0.70)
        self.v_vignette = tk.DoubleVar(value=0.25)
        self.v_jpegq = tk.DoubleVar(value=78)
        self.v_scanmode = tk.StringVar(value="gray")
        self.v_warmth = tk.DoubleVar(value=0.30)
        self.v_cast = tk.DoubleVar(value=0.18)
        self.v_sat = tk.DoubleVar(value=1.05)
        self.v_threshold = tk.DoubleVar(value=158)

        # --- 版式 / 输出 ---
        self.v_paper = tk.StringVar(value=paper.PAPER_LABELS["A4"])
        self.v_cw = tk.StringVar(value="210")
        self.v_ch = tk.StringVar(value="297")
        self.v_orient = tk.StringVar(value="auto")
        self.v_margin = tk.DoubleVar(value=8.0)
        self.v_scale = tk.StringVar(value=SCALE_MODES["fit"])
        self.v_render_dpi = tk.StringVar(value="200")
        self.v_out_dpi = tk.StringVar(value="200")
        self.v_format = tk.StringVar(value=OUTPUT_FORMATS["pdf"])
        self.v_outdir = tk.StringVar(value=str(default_output_dir()))
        self.v_outname = tk.StringVar(value="")
        self.v_suffix = tk.StringVar(value="_scan")
        self.v_seed = tk.StringVar(value="0")
        self.v_openfolder = tk.BooleanVar(value=False)

        self.v_pages = tk.StringVar(value="all")
        self.v_preset = tk.StringVar(value="标准文档（推荐）")
        self.v_status = tk.StringVar(value="就绪")
        self.v_progress = tk.DoubleVar(value=0.0)

    # ---------------- 布局 ----------------
    def _build_ui(self):
        top = ttk.Frame(self, padding=(PAD, PAD, PAD, 0))
        top.pack(fill="x")
        self._build_toolbar(top)

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=PAD, pady=(PAD, 0))
        self.body = body

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=38)
        body.add(right, weight=62)
        self.left_pane = left
        self.right_pane = right
        self._sash_job = None
        # ttk.PanedWindow 不会把窗格压到小于其「请求宽度」，两栏相加超出窗口时
        # 右栏会被裁到窗口外（数值列正好在最右侧，整列消失）。
        # 所以这里在布局变化后主动设定分隔条位置，保证右栏拿到它需要的宽度。
        body.bind("<Configure>", self._on_body_configure)

        self._build_file_panel(left)
        self._build_param_panel(right)

        bottom = ttk.Frame(self, padding=(PAD, PAD, PAD, PAD))
        bottom.pack(fill="x")
        self._build_bottom(bottom)

    # ---------------------------------------------------------- 分隔条控制
    def _on_body_configure(self, _evt=None):
        if self._sash_job:
            try:
                self.after_cancel(self._sash_job)
            except Exception:
                pass
        self._sash_job = self.after(30, self._fix_panes)

    def _fix_panes(self):
        self._sash_job = None
        try:
            total = self.body.winfo_width()
            if total < 50:
                return
            need_right = max(self.right_pane.winfo_reqwidth(), 520)
            need_left = max(self.left_pane.winfo_reqwidth(), 300)
            if need_left + need_right <= total:
                pos = need_left          # 左栏刚好够用，其余全部给右栏
            else:
                pos = max(260, total - need_right)   # 空间不足时优先保右栏完整
            try:
                cur = self.body.sashpos(0)
            except Exception:
                cur = -1
            if abs(int(pos) - int(cur)) > 2:         # 位置没变就别动，避免反复触发
                self.body.sashpos(0, int(pos))
        except Exception:
            pass

    # ---- 工具条 ----
    def _build_toolbar(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x")

        ttk.Label(bar, text="预设：").pack(side="left")
        cb = ttk.Combobox(bar, textvariable=self.v_preset, values=PRESET_ORDER,
                          state="readonly", width=24)
        cb.pack(side="left", padx=(0, PAD))
        cb.bind("<<ComboboxSelected>>", lambda e: self._apply_preset())
        _Tooltip(cb, "内置参数预设。选择后会覆盖当前「打印效果 / 扫描效果」的全部参数。")

        ttk.Button(bar, text="添加 PDF…", command=self.add_files).pack(side="left", padx=2)
        ttk.Button(bar, text="移除选中", command=self.remove_selected).pack(side="left", padx=2)
        ttk.Button(bar, text="清空列表", command=self.clear_files).pack(side="left", padx=2)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=PAD)

        ttk.Button(bar, text="效果预览", command=self.preview).pack(side="left", padx=2)
        _Tooltip(bar.winfo_children()[-1],
                 "对列表首个文件的第一页按当前参数快速渲染一张预览图，\n用于调参数，不产生输出文件。")

        ttk.Button(bar, text="关于", command=self.show_about).pack(side="right", padx=2)
        ttk.Button(bar, text="环境报告", command=self.show_environment_report).pack(
            side="right", padx=2)
        ttk.Button(bar, text="日志目录", command=self.open_log_dir).pack(side="right", padx=2)
        _Tooltip(bar.winfo_children()[-3],
                 "生成一份运行环境体检报告（系统版本、数据目录可写性、临时目录等），\n"
                 "换电脑后遇到问题时，把这个文件一起发给开发者最快定位。")

    # ---- 左：文件列表 ----
    def _build_file_panel(self, parent):
        lf = ttk.Labelframe(parent, text=" 待处理 PDF ", padding=PAD)
        lf.pack(fill="both", expand=True)

        cols = ("name", "pages", "size")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("name", text="文件名")
        self.tree.heading("pages", text="页数")
        self.tree.heading("size", text="大小")
        self.tree.column("name", width=240, anchor="w")
        self.tree.column("pages", width=56, anchor="center", stretch=False)
        self.tree.column("size", width=80, anchor="e", stretch=False)

        vsb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Double-1>", lambda e: self.preview())

        hint = ttk.Label(lf, style="Hint.TLabel",
                         text="支持一次添加多个文件批量处理；双击文件可快速预览。")
        hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        pg = ttk.Frame(lf)
        pg.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(PAD, 0))
        ttk.Label(pg, text="页码范围：").pack(side="left")
        ent = ttk.Entry(pg, textvariable=self.v_pages, width=22)
        ent.pack(side="left")
        ttk.Label(pg, style="Hint.TLabel",
                  text="all 或 1-3,5,8-10").pack(side="left", padx=(6, 0))
        _Tooltip(ent, "留空或填 all 表示全部页面。\n也可填 1-3,5,8-10 这样的区间，从 1 开始计数。")

        self.lbl_filecount = ttk.Label(lf, text="共 0 个文件", style="Hint.TLabel")
        self.lbl_filecount.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

    # ---- 右：参数面板 ----
    def _build_param_panel(self, parent):
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True)
        self.nb = nb

        t1 = ttk.Frame(nb, padding=PAD)
        t2 = ttk.Frame(nb, padding=PAD)
        t3 = ttk.Frame(nb, padding=PAD)
        nb.add(t1, text="  打印效果  ")
        nb.add(t2, text="  扫描效果  ")
        nb.add(t3, text="  版式与输出  ")

        self._build_print_tab(t1)
        self._build_scan_tab(t2)
        self._build_layout_tab(t3)

    def _build_print_tab(self, p):
        p.columnconfigure(1, weight=1)
        r = 0

        ttk.Label(p, text="打印引擎", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        box = ttk.Frame(p)
        box.grid(row=r, column=1, columnspan=2, sticky="ew", pady=2)
        for i, (key, label) in enumerate(PRINT_MODES.items()):
            ttk.Radiobutton(box, text=label, value=key, variable=self.v_mode,
                            command=self._on_mode_change).pack(anchor="w")
        r += 1

        ttk.Label(p, style="Hint.TLabel", wraplength=400, justify="left",
                  text="黑白激光：黑字是真黑、字口锐利，灰阶靠网点。\n"
                       "彩色激光：黑是 CMY 合成黑，灰度化后发灰；四色套印不准会有彩边。\n"
                       "彩色喷墨：墨量更足但暗部发闷、偏青。"
                  ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, PAD))
        r += 1

        ttk.Label(p, text="纸张类型", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        self.cb_papertype = ttk.Combobox(
            p, textvariable=self.v_papertype, state="readonly", width=26,
            values=[PRINT_PAPER_TYPES[k] for k in PRINT_PAPER_TYPES])
        self.cb_papertype.grid(row=r, column=1, sticky="w", pady=2)
        _Tooltip(self.cb_papertype,
                 "同一种墨印在不同纸上差别很大：\n"
                 "· 铜版/涂布纸表面光滑，墨点被「封」在表面 → 扩散小、颗粒细、字口实\n"
                 "· 再生纸/草纸纤维粗长 → 毛细渗透严重、掉粉多、边缘发毛\n"
                 "这一项会同时影响墨点扩散与颗粒感两项的实际强度。")
        r += 1
        ttk.Label(p, style="Hint.TLabel", wraplength=400, justify="left",
                  text="纸张只改变「墨在纸上怎么散」，不改变打印机的墨量。"
                  ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, PAD))
        r += 1

        ttk.Separator(p, orient="horizontal").grid(row=r, column=0, columnspan=3,
                                                   sticky="ew", pady=PAD)
        r += 1

        specs = [
            ("浓度（碳粉/墨量）", self.v_density, 0.60, 1.45, "{:.2f}",
             "越大印得越黑越实。小于 1.0 为淡印，大于 1.0 为浓印。"),
            ("阶调对比", self.v_pcontrast, 0.70, 1.40, "{:.2f}",
             "打印机成像的对比度，越大明暗拉得越开。"),
            ("墨点扩散", self.v_spread, 0.00, 1.00, "{:.2f}",
             "油墨在纸纤维上的毛细渗透程度。越大越糊，边缘越软。\n"
             "实际强度还会乘上「纸张类型」的扩散系数。"),
            ("颗粒感", self.v_grain, 0.00, 1.00, "{:.2f}",
             "碳粉/墨滴转印的随机性，产生颗粒噪声与偶发掉粉脏点。\n"
             "实际强度还会乘上「纸张类型」的颗粒系数。"),
            ("底灰（白底浮墨）", self.v_fog, 0.00, 1.00, "{:.2f}",
             "定影不净 / 四色版留底时，白纸会浮一层极淡的墨雾。\n"
             "幅度很小（最多 3 个灰阶），但这一层「不干净的白」是真实感的关键。\n"
             "调成 0 会得到一张过于纯净的纸，反而不像打印出来的。"),
            ("半色调网点", self.v_halftone, 0.00, 1.00, "{:.2f}",
             "打印机用网点表现灰阶。中间调会出现网点纹理。"),
        ]
        for label, var, lo, hi, fmt, tip in specs:
            self._add_slider(p, r, label, var, lo, hi, fmt, tip)
            r += 1

        ttk.Separator(p, orient="horizontal").grid(row=r, column=0, columnspan=3,
                                                   sticky="ew", pady=PAD)
        r += 1
        ttk.Label(p, text="仅彩色引擎生效", style="Hint.TLabel").grid(
            row=r, column=1, sticky="w")
        r += 1
        self.sl_misreg = self._add_slider(
            p, r, "四色套印不准", self.v_misreg, 0.0, 1.0, "{:.2f}",
            "CMYK 四个色版对位偏差，产生彩边。灰度化后边缘会发虚、发脏。")
        r += 1
        self.sl_tac = self._add_slider(
            p, r, "总墨量上限 (%)", self.v_tac, 150, 360, "{:.0f}",
            "四色叠印的总覆盖率上限。越低暗部越糊、越闷。常见值 240~300。")
        r += 1
        self._on_mode_change()

    def _build_scan_tab(self, p):
        p.columnconfigure(1, weight=1)
        r = 0

        # ---- 页面间一致性（放在最前：这是「输出好不好看」的第一决定项）----
        ttk.Label(p, text="页面间效果", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        self.cb_vary = ttk.Combobox(
            p, textvariable=self.v_pvary, state="readonly", width=26,
            values=[PAGE_VARIATIONS[k] for k in PAGE_VARIATIONS])
        self.cb_vary.grid(row=r, column=1, sticky="w", pady=2)
        _Tooltip(self.cb_vary,
                 "真实的一叠扫描件来自同一批纸、同一次过机：\n"
                 "纸底纹理、传感器噪点、偏色方向、灰尘位置几乎完全一样，\n"
                 "只有进纸歪斜会有几十分之一度的差别。\n\n"
                 "整份统一：所有页面共用同一套纸张/传感器指纹，只有倾斜角可能微抖。\n"
                 "　　　　　这是绝大多数场合的正确选择，输出文件翻页时观感一致。\n"
                 "仅倾斜微抖：纹理仍然统一，但每页的进纸角度不同。\n"
                 "逐页随机：每页都重新抽纹理与偏色，翻页像十几份不同扫描件，\n"
                 "　　　　　只在刻意要「杂乱旧档」的感觉时才用。")
        r += 1
        ttk.Label(p, style="Hint.TLabel", wraplength=400, justify="left",
                  text="默认整份统一 —— 同一份 PDF 的每一页共用一套纸张与传感器指纹，"
                       "翻页时不会忽明忽暗、忽左歪忽右歪。"
                  ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, PAD))
        r += 1

        ttk.Separator(p, orient="horizontal").grid(row=r, column=0, columnspan=3,
                                                   sticky="ew", pady=PAD)
        r += 1

        ttk.Label(p, style="Hint.TLabel", wraplength=400, justify="left",
                  text="模拟扫描仪把打印件读回来的过程：纸底反射率 × 墨迹反射率，"
                       "再叠加光学模糊、传感器噪点、进纸倾斜与边缘照度衰减。"
                  ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, PAD))
        r += 1

        for label, var, lo, hi, fmt, tip in [
            ("纸张底色灰度", self.v_paperbase, 215, 255, "{:.0f}",
             "真实纸基不是纯白。255 为理想白，实际扫描件多在 240~250。"),
            ("纸底不均匀度", self.v_cloud, 0.0, 1.0, "{:.2f}",
             "纸张定量不匀 + 扫描灯管不均造成的底色起伏。调大会有「脏底」感。"),
            ("传感器噪点", self.v_noise, 0.0, 1.0, "{:.2f}",
             "暗部信噪比差，噪声集中在深色区域，白纸几乎干净。"),
            ("灰尘 / 污渍", self.v_dust, 0.0, 1.0, "{:.2f}",
             "成块的斑点，不是逐像素抖动：纸面粉尘、复印机鼓面残留、\n"
             "盖板玻璃上的毛屑。斑点大小按物理尺寸算，与分辨率无关。\n"
             "这是「旧」的主要来源，但也是「脏」的主要来源，默认值刻意压低。"),
            ("纵向条纹", self.v_streak, 0.0, 1.0, "{:.2f}",
             "CIS/CCD 传感器逐列增益标定不均、走纸划痕、灯管脏污。\n"
             "老式扫描仪最典型的毛病，条纹方向永远是走纸方向（竖直）。\n"
             "默认 0（干净），需要「旧机器味」时再调大。"),
            ("光学模糊", self.v_blur, 0.0, 2.0, "{:.2f}",
             "镜头景深、走纸运动模糊。>1.2 会追加方向性模糊。"),
            ("扫描对比度", self.v_scontrast, 0.80, 1.45, "{:.2f}",
             "模拟扫描仪自动曝光的阶调拉伸。"),
            ("亮度", self.v_bright, 0.85, 1.15, "{:.2f}", "整体亮度增益。"),
            ("边缘暗角", self.v_vignette, 0.0, 1.0, "{:.2f}",
             "边缘照度衰减 + 单侧盖板/装订阴影。"),
            ("JPEG 质量", self.v_jpegq, 25, 100, "{:.0f}",
             "越低块效应越明显、数码味越重。二值模式自动改用无损 PNG。"),
        ]:
            self._add_slider(p, r, label, var, lo, hi, fmt, tip)
            r += 1

        # 倾斜
        sk = ttk.Frame(p)
        sk.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(PAD, 0))
        ttk.Checkbutton(sk, text="倾斜角度随机生成", variable=self.v_skew_random,
                        command=self._on_skew_toggle).pack(side="left")
        r += 1
        self.sl_skew = self._add_slider(p, r, "固定倾斜角 (°)", self.v_skew, -2.0, 2.0,
                                        "{:+.2f}", "进纸歪斜。正数顺时针，负数逆时针。")
        r += 1
        self.sl_skew_amt = self._add_slider(p, r, "随机倾斜幅度 (°)", self.v_skew_amt,
                                            0.0, 2.0, "{:.2f}",
                                            "每页在 ±该幅度内随机倾斜，模拟进纸不齐。\n"
                                            "页面间效果为「整份统一」时，整份共用一个角度。")
        r += 1
        self._on_skew_toggle()

        ttk.Separator(p, orient="horizontal").grid(row=r, column=0, columnspan=3,
                                                   sticky="ew", pady=PAD)
        r += 1
        ttk.Label(p, text="输出模式", anchor="w").grid(row=r, column=0, sticky="w")
        box = ttk.Frame(p)
        box.grid(row=r, column=1, columnspan=2, sticky="ew")
        ttk.Radiobutton(box, text="灰度扫描（推荐）", value="gray",
                        variable=self.v_scanmode).pack(side="left")
        ttk.Radiobutton(box, text="黑白二值", value="binary",
                        variable=self.v_scanmode).pack(side="left", padx=(PAD, 0))
        ttk.Radiobutton(box, text="彩色扫描", value="color",
                        variable=self.v_scanmode).pack(side="left", padx=(PAD, 0))
        _Tooltip(box, "灰度：扫描仪按亮度采样，最接近真实黑白扫描件。\n"
                      "二值：只有纯黑与纯白，字口硬化，文件体积最小。\n"
                      "彩色：保留彩色，输出 RGB。配合彩色打印机使用效果最好 ——\n"
                      "　　　黑白打印机只有灰墨，彩色扫描出来依然接近中性灰。")
        r += 1
        self.sl_threshold = self._add_slider(
            p, r, "二值化阈值", self.v_threshold, 60, 235, "{:.0f}",
            "仅二值模式生效。阈值越高，越多灰阶被判为黑。")
        r += 1

        ttk.Separator(p, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", pady=PAD)
        r += 1
        ttk.Label(p, text="彩色扫描专用", anchor="w",
                  style="Section.TLabel").grid(row=r, column=0, columnspan=3,
                                               sticky="w", pady=(0, 2))
        r += 1
        self.sl_warmth = self._add_slider(
            p, r, "纸张暖调", self.v_warmth, 0.0, 1.0, "{:.2f}",
            "纸张与扫描灯管的偏暖程度。真实白纸彩色扫描后不会是中性灰，\n"
            "纸底通常偏暖黄。0 = 理想中性白，0.3 ≈ 普通复印纸。")
        r += 1
        self.sl_cast = self._add_slider(
            p, r, "白平衡偏差", self.v_cast, 0.0, 1.0, "{:.2f}",
            "扫描仪三通道增益不一致造成的整体偏色（青/品红/黄随机一侧）。\n"
            "完全为 0 会让彩色扫描件「太准」，反而不像真实扫描的。")
        r += 1
        self.sl_sat = self._add_slider(
            p, r, "饱和度增益", self.v_sat, 0.5, 1.6, "{:.2f}",
            "扫描仪自动校色的结果。1.0 = 原样，>1 更艳，<1 更灰。")
        r += 1
        self._on_scanmode_change()
        self.v_scanmode.trace_add("write", lambda *a: self._on_scanmode_change())

    def _build_layout_tab(self, p):
        p.columnconfigure(1, weight=1)
        r = 0

        ttk.Label(p, text="纸张尺寸", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        self.cb_paper = ttk.Combobox(
            p, textvariable=self.v_paper, state="readonly", width=26,
            values=[paper.PAPER_LABELS[k] for k in paper.PAPER_ORDER])
        self.cb_paper.grid(row=r, column=1, sticky="w", pady=2)
        self.cb_paper.bind("<<ComboboxSelected>>", lambda e: self._on_paper_change())
        r += 1

        cf = ttk.Frame(p)
        cf.grid(row=r, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(cf, text="自定义尺寸（mm）：").pack(side="left")
        self.e_cw = ttk.Entry(cf, textvariable=self.v_cw, width=8)
        self.e_cw.pack(side="left")
        ttk.Label(cf, text=" × ").pack(side="left")
        self.e_ch = ttk.Entry(cf, textvariable=self.v_ch, width=8)
        self.e_ch.pack(side="left")
        r += 1

        ttk.Label(p, text="页面方向", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        of = ttk.Frame(p)
        of.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)
        for k, label in ORIENTATIONS.items():
            ttk.Radiobutton(of, text=label.split("（")[0], value=k,
                            variable=self.v_orient).pack(side="left", padx=(0, PAD))
        r += 1

        self._add_slider(p, r, "页边距 (mm)", self.v_margin, 0.0, 30.0, "{:.1f}",
                         "四边留白，同时模拟打印机不可打印边距与扫描裁边。")
        r += 1

        ttk.Label(p, text="缩放方式", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        cb = ttk.Combobox(p, textvariable=self.v_scale, state="readonly", width=40,
                          values=[SCALE_MODES[k] for k in ("fit", "fill", "actual")])
        cb.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)
        cb.bind("<<ComboboxSelected>>", lambda e: self._sync_scale())
        r += 1

        ttk.Separator(p, orient="horizontal").grid(row=r, column=0, columnspan=3,
                                                   sticky="ew", pady=PAD)
        r += 1

        ttk.Label(p, text="打印分辨率 (DPI)", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(p, textvariable=self.v_render_dpi, state="readonly", width=12,
                     values=["100", "150", "200", "300", "400"]).grid(
            row=r, column=1, sticky="w", pady=2)
        r += 1
        ttk.Label(p, text="扫描分辨率 (DPI)", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(p, textvariable=self.v_out_dpi, state="readonly", width=12,
                     values=["100", "150", "200", "300", "400"]).grid(
            row=r, column=1, sticky="w", pady=2)
        r += 1
        ttk.Label(p, style="Hint.TLabel", wraplength=400, justify="left",
                  text="打印分辨率是「打印机成像」的精度，扫描分辨率是「扫描仪采样」的精度。\n"
                       "扫描分辨率低于打印分辨率时，细节会被真正丢掉（更接近现实）。"
                  ).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1

        ttk.Label(p, text="输出格式", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        cb = ttk.Combobox(p, textvariable=self.v_format, state="readonly", width=26,
                          values=[OUTPUT_FORMATS[k] for k in OUTPUT_FORMATS])
        cb.grid(row=r, column=1, sticky="w", pady=2)
        cb.bind("<<ComboboxSelected>>", lambda e: self._sync_format())
        r += 1

        ttk.Label(p, text="文件名后缀", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(p, textvariable=self.v_suffix, width=18).grid(row=r, column=1, sticky="w", pady=2)
        r += 1

        ttk.Label(p, text="随机种子", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        sf = ttk.Frame(p)
        sf.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)
        e_seed = ttk.Entry(sf, textvariable=self.v_seed, width=14)
        e_seed.pack(side="left")
        ttk.Label(sf, style="Hint.TLabel",
                  text="  0 = 每次随机；填固定值可完全复现同一效果").pack(side="left")
        _Tooltip(e_seed, "噪点、纸底云斑、倾斜角都由随机数生成。\n"
                         "填 0：每次运行结果都不同（推荐）。\n"
                         "填固定数字：结果 100% 可复现，便于对比参数或复现问题。")
        r += 1

        ttk.Label(p, text="输出目录", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        od = ttk.Frame(p)
        od.grid(row=r, column=1, columnspan=2, sticky="ew", pady=2)
        od.columnconfigure(0, weight=1)
        ttk.Entry(od, textvariable=self.v_outdir).grid(row=0, column=0, sticky="ew")
        ttk.Button(od, text="浏览…", width=8,
                   command=self.pick_outdir).grid(row=0, column=1, padx=(4, 0))
        _Tooltip(od, "处理结果保存到哪。留空则使用 exe 同目录的 output 文件夹。")
        r += 1

        ttk.Label(p, text="输出文件名", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        on = ttk.Frame(p)
        on.grid(row=r, column=1, columnspan=2, sticky="ew", pady=2)
        on.columnconfigure(0, weight=1)
        ttk.Entry(on, textvariable=self.v_outname).grid(row=0, column=0, sticky="ew")
        ttk.Button(on, text="另存为…", width=8,
                   command=self.pick_outname).grid(row=0, column=1, padx=(4, 0))
        ttk.Button(on, text="清除", width=6,
                   command=lambda: self.v_outname.set("")).grid(row=0, column=2, padx=(4, 0))
        _Tooltip(on, "留空 = 按源文件名自动生成（如「合同_scan.pdf」）。\n"
                     "填了则用这个名字；批量多个文件时自动加序号避免互相覆盖。\n"
                     "点「另存为…」可直接指定完整路径，扩展名按输出格式自动修正。")
        r += 1

        ttk.Label(p, style="Hint.TLabel", wraplength=400, justify="left",
                  text="完整输出路径 = 输出目录 + 输出文件名 + 自动扩展名\n"
                       "（同名文件不会被覆盖，会自动追加序号）").grid(
            row=r, column=1, columnspan=2, sticky="w", pady=(0, PAD))
        r += 1

        ttk.Checkbutton(p, text="处理完成后自动打开输出目录",
                        variable=self.v_openfolder).grid(
            row=r, column=1, sticky="w", pady=(PAD, 0))

        self._sync_format()
        self._on_paper_change()
        # 输出路径变化时在状态栏给出「最终会写到哪里」的即时反馈
        for v in (self.v_outdir, self.v_outname):
            try:
                v.trace_add("write", lambda *a: self._trace_output_target())
            except Exception:
                pass

    # ---- 底部 ----
    def _build_bottom(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x")

        self.pb = ttk.Progressbar(row, variable=self.v_progress, maximum=100.0,
                                  mode="determinate", length=520)
        self.pb.pack(side="left", fill="x", expand=True)

        self.btn_cancel = ttk.Button(row, text="取消", command=self.cancel, state="disabled",
                                     width=10)
        self.btn_cancel.pack(side="left", padx=(PAD, 0))
        self.btn_run = ttk.Button(row, text="开始处理", command=self.start, width=14,
                                  style="Run.TButton")
        self.btn_run.pack(side="left", padx=(4, 0))

        row2 = ttk.Frame(parent)
        row2.pack(fill="x", pady=(4, 0))
        ttk.Label(row2, textvariable=self.v_status, anchor="w").pack(side="left")
        ttk.Label(row2, text=f"v{__version__}  ·  {BUILD_DATE}",
                  style="Hint.TLabel").pack(side="right")

    # ---------------- 滑块工厂 ----------------
    def _add_slider(self, parent, row, label, var, lo, hi, fmt, tip="") -> SliderRow:
        s = SliderRow(parent, row, label, var, lo, hi, fmt, tip=tip)
        self._sliders.append(s)
        return s

    # ---------------- 联动 ----------------
    def _on_scanmode_change(self):
        """按扫描模式灰掉无关参数，避免用户调了没反应以为程序坏了"""
        mode = self.v_scanmode.get()
        # 二值化阈值只对 binary 有意义
        thr = getattr(self, "sl_threshold", None)
        if thr is not None:
            thr.scale.configure(state="normal" if mode == "binary" else "disabled")
        # 三个彩色参数只对 color 有意义
        for attr in ("sl_warmth", "sl_cast", "sl_sat"):
            s = getattr(self, attr, None)
            if s is not None:
                s.scale.configure(state="normal" if mode == "color" else "disabled")

    def _on_skew_toggle(self):
        random_on = self.v_skew_random.get()
        self.sl_skew.scale.configure(state="disabled" if random_on else "normal")
        self.sl_skew_amt.scale.configure(state="normal" if random_on else "disabled")

    def _on_mode_change(self):
        """彩色参数对黑白引擎无效：灰掉滑块但保留数值，切回彩色不丢"""
        mono = self.v_mode.get() == "mono_laser"
        state = "disabled" if mono else "normal"
        for attr in ("sl_misreg", "sl_tac"):
            s = getattr(self, attr, None)
            if s is not None:
                s.scale.configure(state=state)

    def _on_paper_change(self):
        key = self._paper_key()
        is_custom = key == "custom"
        state = "normal" if is_custom else "disabled"
        self.e_cw.configure(state=state)
        self.e_ch.configure(state=state)

    def _paper_key(self) -> str:
        cur = self.v_paper.get()
        for k in paper.PAPER_ORDER:
            if paper.PAPER_LABELS[k] == cur:
                return k
        return cur if cur in paper.PAPER_ORDER else "A4"

    def _papertype_key(self) -> str:
        cur = self.v_papertype.get()
        for k, v in PRINT_PAPER_TYPES.items():
            if v == cur:
                return k
        return cur if cur in PRINT_PAPER_TYPES else "plain"

    def _pvary_key(self) -> str:
        cur = self.v_pvary.get()
        for k, v in PAGE_VARIATIONS.items():
            if v == cur:
                return k
        return cur if cur in PAGE_VARIATIONS else "uniform"

    def _sync_format(self):
        cur = self.v_format.get()
        for k, v in OUTPUT_FORMATS.items():
            if v == cur:
                self._format_key = k
                return
        self._format_key = "pdf"

    def _sync_scale(self):
        cur = self.v_scale.get()
        for k, v in SCALE_MODES.items():
            if v == cur:
                self._scale_key = k
                return
        self._scale_key = "fit"

    def _scale_key_now(self) -> str:
        cur = self.v_scale.get()
        for k, v in SCALE_MODES.items():
            if v == cur:
                return k
        return "fit"

    # ---------------- 预设 ----------------
    def _apply_preset(self):
        name = self.v_preset.get()
        data = BUILTIN_PRESETS.get(name)
        if not data:
            return
        pr = data.get("print_params", {})
        sc = data.get("scan_params", {})
        la = data.get("layout_params", {})

        if "mode" in pr:
            self.v_mode.set(pr["mode"])
        if "paper_type" in pr:
            self.v_papertype.set(PRINT_PAPER_TYPES.get(pr["paper_type"],
                                                       self.v_papertype.get()))
        for key, var in (("density", self.v_density), ("contrast", self.v_pcontrast),
                         ("spread", self.v_spread), ("grain", self.v_grain),
                         ("fog", self.v_fog),
                         ("halftone", self.v_halftone), ("misregister", self.v_misreg),
                         ("tac_limit", self.v_tac)):
            if key in pr:
                var.set(pr[key])

        mapping = {
            "paper_base": self.v_paperbase, "paper_cloud": self.v_cloud,
            "noise": self.v_noise, "dust": self.v_dust, "streak": self.v_streak,
            "blur": self.v_blur, "contrast": self.v_scontrast,
            "brightness": self.v_bright, "vignette": self.v_vignette,
            "jpeg_quality": self.v_jpegq, "binary_threshold": self.v_threshold,
        }
        for key, var in mapping.items():
            if key in sc:
                var.set(sc[key])
        if "page_variation" in sc:
            self.v_pvary.set(PAGE_VARIATIONS.get(sc["page_variation"],
                                                 self.v_pvary.get()))
        if "out_mode" in sc:
            self.v_scanmode.set(sc["out_mode"])

        if "paper" in la:
            self.v_paper.set(paper.PAPER_LABELS.get(la["paper"], la["paper"]))
        if "margin_mm" in la:
            self.v_margin.set(la["margin_mm"])
        if "scale_mode" in la:
            self.v_scale.set(SCALE_MODES.get(la["scale_mode"], self.v_scale.get()))
        if "orientation" in la:
            self.v_orient.set(la["orientation"])

        for s in self._sliders:
            s.refresh()
        self._on_paper_change()
        self._on_mode_change()
        self._on_scanmode_change()
        self._on_skew_toggle()
        self.v_status.set(f"已应用预设：{name}")
        log.info("应用预设: %s", name)

    # ---------------- 文件操作 ----------------
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择 PDF 文件",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")])
        if not paths:
            return
        self.add_paths(paths)

    def add_paths(self, paths):
        """把一批路径加进列表（对话框之外也能调用，便于测试/拖放）"""
        added = 0
        for p in paths:
            pth = Path(p)
            if pth in self.files:
                continue
            n = engine.pdf_page_count(pth)
            if n <= 0:
                messagebox.showwarning("无法读取",
                                       f"{pth.name}\n不是有效的 PDF 或已加密，已跳过。")
                log.warning("跳过无法读取的文件: %s", pth)
                continue
            self.files.append(pth)
            self.page_counts[str(pth)] = n
            self.tree.insert("", "end", iid=str(pth), values=(
                pth.name, n, self._human_size(pth.stat().st_size)))
            added += 1
        self._refresh_count()
        self.v_status.set(f"已添加 {added} 个文件")
        log.info("添加文件 %d 个，列表共 %d 个", added, len(self.files))
        return added

    def remove_selected(self):
        sel = list(self.tree.selection())
        if not sel:
            return
        for iid in sel:
            self.tree.delete(iid)
            p = Path(iid)
            if p in self.files:
                self.files.remove(p)
            self.page_counts.pop(iid, None)
        self._refresh_count()

    def clear_files(self):
        self.tree.delete(*self.tree.get_children())
        self.files.clear()
        self.page_counts.clear()
        self._refresh_count()

    def _refresh_count(self):
        total_pages = sum(self.page_counts.get(str(f), 0) for f in self.files)
        self.lbl_filecount.configure(text=f"共 {len(self.files)} 个文件 / {total_pages} 页")

    @staticmethod
    def _human_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} GB"

    def pick_outdir(self):
        d = filedialog.askdirectory(title="选择输出目录",
                                    initialdir=self.v_outdir.get() or str(default_output_dir()))
        if d:
            self.v_outdir.set(d)

    def pick_outname(self):
        """「另存为…」：一次性指定完整输出路径（目录 + 文件名）"""
        fmt_key = self.v_format.get()
        ext = {"pdf": ".pdf", "png": ".png", "jpg": ".jpg",
               "pdf_img": ".pdf"}.get(fmt_key, ".pdf")
        d = self.v_outdir.get().strip() or str(default_output_dir())
        src = self.tree.selection()
        stem = Path(src[0]).stem if src else "扫描结果"
        initial = str(Path(d) / f"{stem}_scan{ext}")
        chosen = filedialog.asksaveasfilename(
            title="指定输出文件（目录与文件名会分别填入）",
            initialdir=d, initialfile=Path(initial).name,
            defaultextension=ext,
            filetypes=[("PDF 文件", "*.pdf"), ("PNG 图片", "*.png"),
                       ("JPEG 图片", "*.jpg")])
        if chosen:
            p = Path(chosen)
            self.v_outdir.set(str(p.parent))
            self.v_outname.set(p.stem)     # 扩展名由输出格式决定，不用手填
            self.v_status.set(f"输出路径：{p.parent}\\{p.stem}{ext}")

    def _preview_output_path(self) -> str:
        """给界面/提示用的「最终会写到哪里」预览文本"""
        try:
            r = engine.resolve_output_targets(
                OutputParams(out_format=self.v_format.get().strip() or "pdf",
                             out_dir=self.v_outdir.get().strip(),
                             out_name=self.v_outname.get().strip()),
                Path(self.tree.selection()[0]) if self.tree.selection()
                else Path("示例.pdf"))
            if r["pdf"]:
                return str(r["pdf"])
            if r["want_img"]:
                return str(r["dir"] / f"{r['base']}_p001{r['img_ext']}") + " …"
        except Exception:
            pass
        return self.v_outdir.get().strip() or str(default_output_dir())

    def _trace_output_target(self):
        """跟踪实际写入目标（日志里能看到），并顺手更新状态栏"""
        try:
            tgt = self.v_outname.get().strip()
            if tgt:
                d = self.v_outdir.get().strip() or "默认 output 目录"
                msg = f"将输出到：{d}\\{tgt}"
            else:
                msg = f"将输出到：{self._preview_output_path()}"
            self.v_status.set(msg)
        except Exception:
            pass

    def open_log_dir(self):
        self._open_path(str(logsetup.paths.log_dir()))

    @staticmethod
    def _open_path(path: str):
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            log.exception("打开路径失败: %s", path)

    # ---------------- 参数收集 ----------------
    def collect_job(self) -> RenderJob:
        self._sync_format()
        mode_key = self._format_key
        scan_mode = self.v_scanmode.get()

        pp = PrintParams(
            mode=self.v_mode.get(),
            paper_type=self._papertype_key(),
            density=round(self.v_density.get(), 3),
            contrast=round(self.v_pcontrast.get(), 3),
            spread=round(self.v_spread.get(), 3),
            grain=round(self.v_grain.get(), 3),
            fog=round(self.v_fog.get(), 3),
            halftone=round(self.v_halftone.get(), 3),
            misregister=round(self.v_misreg.get(), 3),
            tac_limit=round(self.v_tac.get(), 1),
        )

        if self.v_skew_random.get():
            skew = None
            skew_rand = round(self.v_skew_amt.get(), 3)
        else:
            skew = round(self.v_skew.get(), 3)
            skew_rand = 0.0

        sp = ScanParams(
            page_variation=self._pvary_key(),
            paper_base=round(self.v_paperbase.get(), 1),
            paper_cloud=round(self.v_cloud.get(), 3),
            noise=round(self.v_noise.get(), 3),
            dust=round(self.v_dust.get(), 3),
            streak=round(self.v_streak.get(), 3),
            blur=round(self.v_blur.get(), 3),
            contrast=round(self.v_scontrast.get(), 3),
            brightness=round(self.v_bright.get(), 3),
            skew_deg=skew,
            skew_random=skew_rand,
            vignette=round(self.v_vignette.get(), 3),
            jpeg_quality=int(round(self.v_jpegq.get())),
            out_mode=scan_mode,
            binary_threshold=int(round(self.v_threshold.get())),
            paper_warmth=round(self.v_warmth.get(), 3),
            color_cast=round(self.v_cast.get(), 3),
            sat_boost=round(self.v_sat.get(), 3),
        )

        try:
            cw = float(self.v_cw.get())
            ch = float(self.v_ch.get())
        except ValueError:
            cw, ch = 210.0, 297.0
            self.v_cw.set("210")
            self.v_ch.set("297")

        lp = LayoutParams(
            paper=self._paper_key(),
            custom_w_mm=cw,
            custom_h_mm=ch,
            orientation=self.v_orient.get(),
            margin_mm=round(self.v_margin.get(), 2),
            scale_mode=self._scale_key_now(),
        )

        try:
            seed = int(float(self.v_seed.get() or 0))
        except ValueError:
            seed = 0
            self.v_seed.set("0")

        op = OutputParams(
            render_dpi=int(self.v_render_dpi.get()),
            out_dpi=int(self.v_out_dpi.get()),
            out_format=mode_key,
            out_dir=self.v_outdir.get().strip(),
            out_name=self.v_outname.get().strip(),
            name_suffix=self.v_suffix.get().strip(),
            jpeg_in_pdf=True,
            open_folder_after=bool(self.v_openfolder.get()),
            seed=seed,
        )

        return RenderJob(pp, sp, lp, op, pages=self.v_pages.get().strip() or "all")

    # ---------------- 开始处理 ----------------
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.files:
            messagebox.showinfo("提示", "请先添加至少一个 PDF 文件。")
            return

        job = self.collect_job()

        # ① 页码范围**语法**校验。用 validate_page_range（只解析不展开），
        #    绝不能用 parse_page_range(spec, 假 total) 来校验 —— 那会真的去分配
        #    一个巨大的列表，见 engine.validate_page_range 的注释。
        ok, why = engine.validate_page_range(job.pages)
        if not ok:
            messagebox.showwarning(
                "页码范围无效",
                f"{why}\n\n"
                f"当前填的是：{job.pages}\n\n"
                "正确写法：\n"
                "· 留空 或 all  → 全部页面\n"
                "· 1-3,5,8-10    → 第1~3页、第5页、第8~10页（从 1 开始计数）")
            return

        # ② 范围与文件实际页码是否有交集 —— 提前告知，别等跑完才发现"处理了 0 页"
        spec = job.pages.strip().lower()
        if spec not in ("", "all", "全部", "*"):
            def _hits_any() -> bool:
                for f in self.files:
                    n = self.page_counts.get(str(f), 0)
                    if n > 0 and engine.parse_page_range(job.pages, n):
                        return True
                return False

            if not _hits_any():
                detail = "\n".join(
                    f"· {Path(f).name}：共 {self.page_counts.get(str(f), 0)} 页"
                    for f in self.files[:5])
                if len(self.files) > 5:
                    detail += f"\n… 另有 {len(self.files) - 5} 个文件"
                messagebox.showwarning(
                    "页码范围与文件页数不匹配",
                    f"页码范围「{job.pages}」在所选文件里一页都取不到。\n\n"
                    f"{detail}\n\n"
                    "请调整页码范围，或把该字段留空 / 填 all 表示全部页面。")
                return

        outdir = Path(job.output_params.out_dir) if job.output_params.out_dir \
            else default_output_dir()
        try:
            outdir.mkdir(parents=True, exist_ok=True)
            probe = outdir / ".wb_write_test"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
        except Exception as e:
            messagebox.showerror("输出目录不可写",
                                 f"{outdir}\n\n{e}\n\n请更换输出目录。")
            return

        self._save_state()
        self.cancel_flag.clear()
        self.output_paths.clear()
        self.v_progress.set(0.0)
        self.btn_run.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.v_status.set("正在处理…")
        log.info("=" * 70)
        log.info("开始批量处理: %d 个文件", len(self.files))

        self.worker = threading.Thread(
            target=self._run_worker, args=(list(self.files), job, outdir),
            name="render", daemon=True)
        self.worker.start()

    def _run_worker(self, files: list[Path], job: RenderJob, outdir: Path):
        q = self.msg_queue
        total_pages = 0
        per_file_pages: dict[str, list[int]] = {}
        for f in files:
            try:
                n = engine.pdf_page_count(f)
                per_file_pages[str(f)] = engine.parse_page_range(job.pages, n)
                total_pages += len(per_file_pages[str(f)])
            except Exception:
                per_file_pages[str(f)] = []
        total_pages = max(1, total_pages)

        done_pages = 0
        ok_files = 0
        fail_files: list[str] = []
        t_all = time.perf_counter()

        for i, f in enumerate(files, 1):
            if self.cancel_flag.is_set():
                break
            n_this = max(1, len(per_file_pages.get(str(f), [])))
            try:
                sub_job = RenderJob.from_dict(job.to_dict())
                sub_job.pages = job.pages

                def progress(d, t, m, _f=f, _i=i, _n=n_this):
                    overall = (done_pages + d) / total_pages * 100.0
                    q.put(("progress", overall,
                           f"[{_i}/{len(files)}] {_f.name}  {m}"))

                res = engine.process_pdf(f, sub_job, out_dir=outdir,
                                         file_index=i, file_count=len(files),
                                         progress=progress,
                                         should_cancel=self.cancel_flag.is_set)
                if res.ok:
                    ok_files += 1
                    self.output_paths.extend(res.outputs)
                    q.put(("log", None,
                           f"完成 {f.name}: {res.pages_done} 页, {res.elapsed:.1f}s"))
                else:
                    fail_files.append(f"{f.name}: {res.error}")
                    q.put(("log", None, f"失败 {f.name}: {res.error}"))
            except engine.Cancelled:
                q.put(("log", None, "已取消"))
                break
            except Exception as e:
                log.exception("文件处理异常: %s", f)
                fail_files.append(f"{f.name}: {type(e).__name__}: {e}")
            done_pages += n_this
            q.put(("progress", done_pages / total_pages * 100.0,
                   f"[{i}/{len(files)}] {f.name} 完成"))

        q.put(("done", (ok_files, fail_files, time.perf_counter() - t_all),
               self.cancel_flag.is_set()))

    # ---------------- 主线程消息泵 ----------------
    def _pump(self):
        try:
            while True:
                kind, a, b = self.msg_queue.get_nowait()
                if kind == "progress":
                    self.v_progress.set(float(a or 0))
                    self.v_status.set(str(b))
                elif kind == "log":
                    self.v_status.set(str(b))
                elif kind == "done":
                    self._on_done(a, b)
        except queue.Empty:
            pass
        except Exception:
            log.exception("消息泵异常")
        finally:
            self.after(120, self._pump)

    def _on_done(self, info, cancelled: bool):
        ok_files, fail_files, elapsed = info
        self.btn_run.configure(state="normal")
        self.btn_cancel.configure(state="disabled")

        if cancelled:
            self.v_status.set("已取消")
            messagebox.showinfo("已取消", "处理被用户中断。")
            return

        if not fail_files:
            self.v_progress.set(100.0)
            self.v_status.set(f"全部完成：{ok_files} 个文件，用时 {elapsed:.1f}s")
            msg = (f"处理完成！\n\n成功：{ok_files} 个文件\n"
                   f"用时：{elapsed:.1f} 秒\n输出目录：\n{self.v_outdir.get()}")
            if self.v_openfolder.get():
                self._open_path(self.v_outdir.get())
            messagebox.showinfo("完成", msg)
        else:
            self.v_status.set(f"完成 {ok_files} 个，失败 {len(fail_files)} 个")
            detail = "\n".join(fail_files[:8])
            more = f"\n… 另有 {len(fail_files) - 8} 个" if len(fail_files) > 8 else ""
            messagebox.showwarning(
                "部分失败",
                f"成功 {ok_files} 个，失败 {len(fail_files)} 个。\n\n{detail}{more}\n\n"
                f"详细原因已写入日志：\n{logsetup.paths.log_dir()}")

    def cancel(self):
        self.cancel_flag.set()
        self.v_status.set("正在取消…")
        log.info("用户请求取消")

    # ---------------- 预览 ----------------
    def preview(self):
        if not self.files:
            messagebox.showinfo("提示", "请先添加至少一个 PDF 文件。")
            return
        sel = self.tree.selection()
        target = Path(sel[0]) if sel else self.files[0]

        job = self.collect_job()
        try:
            idx = engine.parse_page_range(job.pages, engine.pdf_page_count(target))
            page_index = idx[0] if idx else 0
        except Exception:
            page_index = 0

        log.info("打开预览窗口: %s 第 %d 页", target.name, page_index + 1)
        try:
            PreviewWindow(self, target, job, page_index)
        except Exception as e:
            log.exception("预览失败")
            messagebox.showerror("预览失败", f"{type(e).__name__}: {e}\n\n详情见日志。")

    def _save_preview(self, img, src: Path, page_index: int):
        p = filedialog.asksaveasfilename(
            title="保存预览图", defaultextension=".png",
            initialfile=f"{src.stem}_preview_p{page_index + 1}.png",
            filetypes=[("PNG 图片", "*.png"), ("JPEG 图片", "*.jpg")])
        if not p:
            return
        try:
            img.save(p)
            self.v_status.set(f"预览图已保存：{p}")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    # ---------------- 关于 / 崩溃 ----------------
    def show_about(self):
        # 部署体检：把跨机使用最容易出问题的几项直接摆在用户面前，
        # 免得"日志在哪""为什么双击没反应"这类问题还要靠猜。
        import platform
        import sys as _sys
        mode = "单文件 exe（绿色版）" if paths.is_frozen() else "源码运行"
        root_ok, root_why = paths.writable_root_status()
        tmp_ok, tmp_why = paths.temp_dir_status()
        arch = platform.machine()
        bits = "64 位" if _sys.maxsize > 2 ** 32 else "32 位"

        messagebox.showinfo(
            "关于",
            f"{APP_NAME}\n版本 {__version__}（{BUILD_DATE}）\n\n"
            f"把 PDF 模拟成「打印后扫描」的黑白扫描件。\n\n"
            f"· 黑白激光 / 彩色激光 / 彩色喷墨 三种打印引擎\n"
            f"· 纸底、云斑、噪点、模糊、倾斜、暗角 全套扫描劣化\n"
            f"· 可指定 A3/A4/A5/A6/B4/B5/16开/Letter 等输出纸张\n"
            f"· 可指定输出目录与文件名\n"
            f"· 全量运行日志与崩溃日志，便于排查问题\n"
            f"· 崩溃时自动弹出日志位置\n\n"
            f"──────────  运行环境  ──────────\n"
            f"运行模式  ：{mode}\n"
            f"系统      ：{platform.system()} {platform.release()}（{arch} {bits}）\n"
            f"Python    ：{platform.python_version()}\n\n"
            f"──────────  数据与日志  ──────────\n"
            f"{'✓' if root_ok else '✗'} 数据目录：{root_why}\n"
            f"{'✓' if tmp_ok else '✗'} 临时目录：{tmp_why}\n\n"
            f"程序目录：{paths.app_dir()}\n"
            f"日志目录：{logsetup.paths.log_dir()}\n"
            f"输出目录：{self.v_outdir.get().strip() or default_output_dir()}")

    def show_environment_report(self):
        """把运行环境与体检结果写到日志文件（出问题时让用户直接发这个文件）"""
        import platform
        root_ok, root_why = paths.writable_root_status()
        tmp_ok, tmp_why = paths.temp_dir_status()
        txt = "\n".join([
            "=" * 60,
            f"{APP_NAME} v{__version__} 运行环境报告",
            "=" * 60,
            f"时间        ：{_dt.datetime.now():%Y-%m-%d %H:%M:%S}",
            f"运行模式    ：{'打包 exe' if paths.is_frozen() else '源码'}",
            f"可执行文件  ：{sys.executable}",
            f"程序目录    ：{paths.app_dir()}",
            f"数据根目录  ：{paths.writable_root()}",
            f"日志目录    ：{logsetup.paths.log_dir()}",
            f"输出目录    ：{self.v_outdir.get().strip() or default_output_dir()}",
            f"绿色模式    ：{'是' if paths.is_portable() else '否（程序目录不可写）'}",
            f"操作系统    ：{platform.system()} {platform.release()} ({platform.version()})",
            f"架构        ：{platform.machine()}",
            f"Python      ：{platform.python_version()}",
            f"数据目录体检：{'OK  ' if root_ok else 'FAIL'} {root_why}",
            f"临时目录体检：{'OK  ' if tmp_ok else 'FAIL'} {tmp_why}",
            "=" * 60,
        ])
        p = logsetup.paths.log_dir() / f"env_{_dt.datetime.now():%Y-%m-%d_%H%M%S}.txt"
        p.write_text(txt, encoding="utf-8")
        log.info("已生成运行环境报告: %s", p)
        self.v_status.set(f"运行环境报告已保存：{p.name}")
        messagebox.showinfo("运行环境报告", f"已保存到：\n{p}\n\n"
                                          f"发问题时请把这个文件一起附上。")
        return p

    def _on_crash(self, log_path: Path, exc_text: str):
        """崩溃回调（在异常现场被调用），把日志路径告诉用户"""
        def _show():
            try:
                messagebox.showerror(
                    "程序遇到错误",
                    f"发生了未预期的错误，详细信息已写入崩溃日志：\n\n{log_path}\n\n"
                    f"请把该文件发送给开发者以便定位问题。\n\n"
                    f"错误摘要：\n{exc_text.strip().splitlines()[-1][:300]}")
            except Exception:
                pass
        try:
            self.after(0, _show)
        except Exception:
            pass

    # ---------------- 状态持久化 ----------------
    def _state_path(self) -> Path:
        return config_dir() / STATE_FILE

    def _save_state(self):
        try:
            data = {
                "job": self.collect_job().to_dict(),
                "files": [str(f) for f in self.files],
                "preset": self.v_preset.get(),
            }
            self._state_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            log.exception("保存状态失败")

    def _load_state(self):
        p = self._state_path()
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            job = RenderJob.from_dict(data.get("job", {}))
            self._apply_job_to_ui(job)
            if data.get("preset") in PRESET_ORDER:
                self.v_preset.set(data["preset"])
            log.info("已恢复上次的参数设置")
        except Exception:
            log.exception("读取上次状态失败（忽略）")

    def _apply_job_to_ui(self, job: RenderJob):
        pp, sp, lp, op = job.print_params, job.scan_params, job.layout_params, job.output_params
        self.v_mode.set(pp.mode)
        self.v_papertype.set(PRINT_PAPER_TYPES.get(pp.paper_type, self.v_papertype.get()))
        self.v_density.set(pp.density)
        self.v_pcontrast.set(pp.contrast)
        self.v_spread.set(pp.spread)
        self.v_grain.set(pp.grain)
        self.v_fog.set(pp.fog)
        self.v_halftone.set(pp.halftone)
        self.v_misreg.set(pp.misregister)
        self.v_tac.set(pp.tac_limit)

        self.v_pvary.set(PAGE_VARIATIONS.get(sp.page_variation, self.v_pvary.get()))
        self.v_paperbase.set(sp.paper_base)
        self.v_cloud.set(sp.paper_cloud)
        self.v_noise.set(sp.noise)
        self.v_dust.set(sp.dust)
        self.v_streak.set(sp.streak)
        self.v_blur.set(sp.blur)
        self.v_scontrast.set(sp.contrast)
        self.v_bright.set(sp.brightness)
        if sp.skew_deg is None:
            self.v_skew_random.set(True)
            self.v_skew_amt.set(sp.skew_random)
        else:
            self.v_skew_random.set(False)
            self.v_skew.set(sp.skew_deg)
            self.v_skew_amt.set(sp.skew_random)
        self.v_vignette.set(sp.vignette)
        self.v_jpegq.set(sp.jpeg_quality)
        self.v_scanmode.set(sp.out_mode)
        self.v_threshold.set(sp.binary_threshold)
        self.v_warmth.set(sp.paper_warmth)
        self.v_cast.set(sp.color_cast)
        self.v_sat.set(sp.sat_boost)
        self._on_scanmode_change()

        self.v_paper.set(paper.PAPER_LABELS.get(lp.paper, lp.paper))
        self.v_cw.set(str(lp.custom_w_mm))
        self.v_ch.set(str(lp.custom_h_mm))
        self.v_orient.set(lp.orientation)
        self.v_margin.set(lp.margin_mm)
        self.v_scale.set(SCALE_MODES.get(lp.scale_mode, self.v_scale.get()))

        self.v_render_dpi.set(str(op.render_dpi))
        self.v_out_dpi.set(str(op.out_dpi))
        self.v_format.set(OUTPUT_FORMATS.get(op.out_format, self.v_format.get()))
        if op.out_dir:
            self.v_outdir.set(op.out_dir)
        self.v_suffix.set(op.name_suffix)
        self.v_outname.set(op.out_name)
        self.v_seed.set(str(op.seed))
        self.v_openfolder.set(op.open_folder_after)
        self.v_pages.set(job.pages)

        for s in self._sliders:
            s.refresh()
        self._on_paper_change()
        self._on_skew_toggle()
        self._on_mode_change()
        self._on_scanmode_change()
        self._sync_format()

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel("正在处理", "任务尚未完成，确定要退出吗？"):
                return
            self.cancel_flag.set()
        self._save_state()
        log.info("程序退出")
        try:
            self.destroy()
        except Exception:
            pass


class ScrollFrame(ttk.Frame):
    """
    可滚动的容器。

    参数很多时，固定高度的面板会把底部的按钮挤出可视区，
    所以左栏统一走这个容器：内容超出高度时自动出现滚动条。
    鼠标滚轮只在指针进入本区域时生效，避免影响其它窗口。
    """

    def __init__(self, parent, width: int = 320):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, width=width)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")

        try:
            bg = ttk.Style().lookup("TFrame", "background") or self.cget("background")
            self.canvas.configure(background=bg)
        except Exception:
            pass

        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)
        self.inner.bind("<Enter>", self._bind_wheel)
        self.inner.bind("<Leave>", self._unbind_wheel)

    def _on_inner(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, e):
        # 内层宽度跟随画布，保证内容横向铺满
        self.canvas.itemconfigure(self._win, width=e.width)

    def _bind_wheel(self, _e=None):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel, add="+")

    def _unbind_wheel(self, _e=None):
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, e):
        try:
            self.canvas.yview_scroll(int(-e.delta / 120), "units")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 预览窗口（实时调参）
# --------------------------------------------------------------------------- #
class PreviewWindow(tk.Toplevel):
    """
    效果预览 + 实时调参。

    左侧是一个 Notebook，三页分别对应主界面的**打印效果 / 扫描效果 / 版式与输出**。
    拖动任意滑块，预览图立刻跟着变，不用「改参数 → 关窗 → 重开 → 再看」。
    调满意了点「应用到主界面」，三页参数一并写回主窗口。

    三级重算（见 engine.PreviewSession）：
        · 只改扫描参数 → 只重跑扫描阶段（最快，滑动时近乎实时）
        · 改打印参数   → 只重跑打印阶段（排版结果已缓存）
        · 改版式 / 渲染分辨率 → 重建会话（纸张尺寸变了，排版结果作废）
    整个会话用固定随机种子，拖动滑块时噪点图案不会乱跳；
    拖动过程中先出低分辨率快图（QUICK_SCALE），停手 0.5 秒后自动精修。
    """

    QUICK_SCALE = 0.60        # 拖动时的快速预览倍率

    # 这些字段是整数（滑块给的是 float）
    INT_FIELDS = {"jpeg_quality", "binary_threshold", "tac_limit"}
    LEVEL_ORDER = {"scan": 0, "print": 1, "rebuild": 2}

    def __init__(self, master: "App", src: Path, job: RenderJob, page_index: int = 0):
        super().__init__(master)
        self.app = master
        self.src = Path(src)
        self.job = job
        self.page_index = page_index

        self.title(f"效果预览 · 实时调参 —— {self.src.name}")
        self.geometry("1330x930")
        self.minsize(1080, 740)
        self.transient(master)

        # ---- 本地可变参数（各复制一份，不直接改主界面）----
        self.pp = PrintParams(**vars(job.print_params))
        self.sp = ScanParams(**vars(job.scan_params))
        self.lp = LayoutParams(**vars(job.layout_params))
        self.op = OutputParams(**vars(job.output_params))

        self.skew_random = tk.BooleanVar(value=(self.sp.skew_deg is None))
        if self.sp.skew_deg is None:
            self.sp.skew_deg = 0.0

        # ---- 下拉框 / 单选按钮绑定的变量 ----
        self.pmode = tk.StringVar(value=self.pp.mode)
        self.papertype = tk.StringVar(
            value=PRINT_PAPER_TYPES.get(self.pp.paper_type, PRINT_PAPER_TYPES["plain"]))
        self.scanmode = tk.StringVar(value=self.sp.out_mode)
        self.pvary = tk.StringVar(
            value=PAGE_VARIATIONS.get(self.sp.page_variation, PAGE_VARIATIONS["uniform"]))
        self.papervar = tk.StringVar(
            value=paper.PAPER_LABELS.get(self.lp.paper, self.lp.paper))
        self.orientvar = tk.StringVar(value=self.lp.orientation)
        self.scalevar = tk.StringVar(
            value=SCALE_MODES.get(self.lp.scale_mode, SCALE_MODES["fit"]))
        self.rendervar = tk.StringVar(value=str(self.op.render_dpi))
        self.cwvar = tk.StringVar(value=f"{self.lp.custom_w_mm:g}")
        self.chvar = tk.StringVar(value=f"{self.lp.custom_h_mm:g}")

        # ---- 线程与请求队列 ----
        self.jobs: queue.Queue = queue.Queue()
        self.results: queue.Queue = queue.Queue()
        self.session: engine.PreviewSession | None = None
        self.token = 0
        self.shown_token = -1
        self.last_image = None            # 最近一帧扫描结果（用于保存）
        self.last_cost = 0.0
        self.last_scale = 1.0
        self._pending_level = "scan"      # scan < print < rebuild
        self._ready = False               # 首次会话构建完成前不接受渲染请求
        self._debounce_id = None
        self._quality_id = None
        self._resize_id = None
        self._closing = False
        self._p_sliders: list[tuple[str, str, SliderRow]] = []
        self._by_field: dict[str, SliderRow] = {}

        self._build_ui()
        self._start_worker()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 初始化：后台构建预览会话
        self._status("正在准备预览…")
        self.jobs.put(("init", self.page_index, 0, self._make_job()))
        self.after(50, self._poll)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        outer = ttk.Frame(self, padding=PAD)
        outer.pack(fill="both", expand=True)

        left = ttk.Frame(outer, width=392)
        left.pack(side="left", fill="y", padx=(0, PAD))
        left.pack_propagate(False)
        right = ttk.Frame(outer)
        right.pack(side="left", fill="both", expand=True)

        self._build_left(left)
        self._build_right(right)

    def _build_left(self, parent):
        # ---- 信息 ----
        info = ttk.Labelframe(parent, text=" 当前设置 ", padding=PAD)
        info.pack(fill="x")
        self.lbl_info = ttk.Label(info, style="Hint.TLabel", justify="left",
                                  text="加载中…", wraplength=350)
        self.lbl_info.pack(anchor="w")

        # ---- 三个参数页，与主界面一一对应 ----
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True, pady=(PAD, 0))
        self.nb = nb

        t1 = ScrollFrame(nb, width=360)
        t2 = ScrollFrame(nb, width=360)
        t3 = ScrollFrame(nb, width=360)
        nb.add(t1, text="  打印效果  ")
        nb.add(t2, text="  扫描效果  ")
        nb.add(t3, text="  版式与输出  ")

        self._build_p_print(t1.inner)
        self._build_p_scan(t2.inner)
        self._build_p_layout(t3.inner)

        # ---- 按钮 ----
        btns = ttk.Frame(parent)
        btns.pack(fill="x", pady=(PAD, 0))
        ttk.Button(btns, text="应用到主界面", command=self._apply_to_main).pack(fill="x")
        row = ttk.Frame(btns)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="恢复初始参数", command=self._reset).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="保存预览图…", command=self._save).pack(
            side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Button(btns, text="关闭", command=self._on_close).pack(fill="x", pady=(6, 0))

    # ---- 打印效果页 ----
    def _build_p_print(self, p):
        p.columnconfigure(1, weight=1)
        r = 0

        ttk.Label(p, text="打印引擎", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        box = ttk.Frame(p)
        box.grid(row=r, column=1, columnspan=2, sticky="ew", pady=2)
        for key, label in PRINT_MODES.items():
            ttk.Radiobutton(box, text=label, value=key, variable=self.pmode,
                            command=self._on_pmode).pack(anchor="w")
        r += 1

        ttk.Label(p, text="纸张类型", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        cb = ttk.Combobox(p, textvariable=self.papertype, state="readonly", width=24,
                          values=[PRINT_PAPER_TYPES[k] for k in PRINT_PAPER_TYPES])
        cb.grid(row=r, column=1, sticky="w", pady=2)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._on_change("print"))
        _Tooltip(cb, "同一种墨印在不同纸上差别很大：\n"
                     "· 涂布纸表面光滑，墨被「封」在表面 → 扩散小、颗粒细\n"
                     "· 再生纸纤维粗长 → 渗透严重、掉粉多、边缘发毛\n"
                     "这一项会同时乘到「墨点扩散」与「颗粒感」上。")
        r += 1
        ttk.Label(p, style="Hint.TLabel", wraplength=350, justify="left",
                  text="纸张只改变墨在纸上怎么散，不改变打印机的墨量。"
                  ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, PAD))
        r += 1

        ttk.Separator(p, orient="horizontal").grid(row=r, column=0, columnspan=3,
                                                   sticky="ew", pady=PAD)
        r += 1

        for label, field, lo, hi, fmt, tip in [
            ("浓度", "density", 0.60, 1.45, "{:.2f}",
             "碳粉/墨量。越大越黑越实。"),
            ("阶调对比", "contrast", 0.70, 1.40, "{:.2f}",
             "打印机成像对比度。"),
            ("墨点扩散", "spread", 0.0, 1.0, "{:.2f}",
             "毛细渗透程度，越大边缘越软越糊。\n实际强度再乘纸张扩散系数。"),
            ("颗粒感", "grain", 0.0, 1.0, "{:.2f}",
             "转印随机性：颗粒噪声 + 偶发掉粉。"),
            ("底灰", "fog", 0.0, 1.0, "{:.2f}",
             "白底浮墨。幅度很小（最多 3 个灰阶），\n但没有它白纸会「太干净」，不像打印出来的。"),
            ("半色调网点", "halftone", 0.0, 1.0, "{:.2f}",
             "中间调的网点纹理。"),
        ]:
            self._p_slider(p, r, "print", field, label, lo, hi, fmt, tip)
            r += 1

        ttk.Separator(p, orient="horizontal").grid(row=r, column=0, columnspan=3,
                                                   sticky="ew", pady=PAD)
        r += 1
        ttk.Label(p, text="仅彩色引擎生效", style="Hint.TLabel").grid(
            row=r, column=1, sticky="w")
        r += 1
        self._p_slider(p, r, "print", "misregister", "四色套印不准", 0.0, 1.0, "{:.2f}",
                       "CMYK 四版对位偏差，产生彩边。\n灰度化后边缘会发虚。")
        r += 1
        self._p_slider(p, r, "print", "tac_limit", "总墨量上限 (%)", 150, 360, "{:.0f}",
                       "四色叠印总覆盖率上限。越低暗部越闷。常见 240~300。")
        r += 1
        self._on_pmode()

    # ---- 扫描效果页 ----
    def _build_p_scan(self, p):
        p.columnconfigure(1, weight=1)
        r = 0

        ttk.Label(p, text="页面间效果", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        cb = ttk.Combobox(p, textvariable=self.pvary, state="readonly", width=24,
                          values=[PAGE_VARIATIONS[k] for k in PAGE_VARIATIONS])
        cb.grid(row=r, column=1, sticky="w", pady=2)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._on_change("scan"))
        _Tooltip(cb, "整份统一：所有页面共用同一套纸张/传感器指纹，\n"
                     "只有倾斜角可能微抖。这是绝大多数场合的正确选择。\n"
                     "仅倾斜微抖：纹理统一，每页进纸角度不同。\n"
                     "逐页随机：每页重抽纹理与偏色，翻页像十几份不同扫描件。\n\n"
                     "提示：这里只影响**当前这一页**的显示；\n"
                     "翻页后可以看到同一套指纹在别的页上的样子。")
        r += 1
        ttk.Label(p, style="Hint.TLabel", wraplength=350, justify="left",
                  text="默认整份统一，翻页时不会忽明忽暗、忽左歪忽右歪。"
                  ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, PAD))
        r += 1

        ttk.Separator(p, orient="horizontal").grid(row=r, column=0, columnspan=3,
                                                   sticky="ew", pady=PAD)
        r += 1

        for label, field, lo, hi, fmt, tip in [
            ("纸底灰度", "paper_base", 215, 255, "{:.0f}",
             "纸基不是纯白。实际扫描件多在 240~250。"),
            ("纸底不均匀", "paper_cloud", 0.0, 1.0, "{:.2f}",
             "纸张定量不匀 + 灯管不均造成的底色起伏。"),
            ("传感器噪点", "noise", 0.0, 1.0, "{:.2f}",
             "暗部信噪比差，噪声集中在深色区域。"),
            ("灰尘 / 污渍", "dust", 0.0, 1.0, "{:.2f}",
             "成块斑点：纸面粉尘、鼓面残留、玻璃毛屑。\n斑点大小按物理尺寸算，与分辨率无关。"),
            ("纵向条纹", "streak", 0.0, 1.0, "{:.2f}",
             "传感器逐列增益不均、走纸划痕。老扫描仪最典型的毛病。"),
            ("光学模糊", "blur", 0.0, 2.0, "{:.2f}",
             "景深 + 走纸运动模糊。>1.2 带方向性拖影。"),
            ("扫描对比度", "contrast", 0.80, 1.45, "{:.2f}",
             "自动曝光的阶调拉伸。"),
            ("亮度", "brightness", 0.85, 1.15, "{:.2f}", "整体亮度增益。"),
            ("边缘暗角", "vignette", 0.0, 1.0, "{:.2f}",
             "边缘照度衰减 + 单侧盖板阴影。"),
            ("JPEG 质量", "jpeg_quality", 25, 100, "{:.0f}",
             "越低块效应越明显。二值模式自动改用无损 PNG。"),
        ]:
            self._p_slider(p, r, "scan", field, label, lo, hi, fmt, tip)
            r += 1

        ttk.Separator(p, orient="horizontal").grid(row=r, column=0, columnspan=3,
                                                   sticky="ew", pady=PAD)
        r += 1

        ttk.Checkbutton(p, text="倾斜角度随机生成（每页不同）",
                        variable=self.skew_random,
                        command=self._on_skew_toggle).grid(
            row=r, column=0, columnspan=3, sticky="w"); r += 1
        self.sl_skew = self._p_slider(p, r, "scan", "skew_deg", "倾斜角 (°)",
                                      -2.0, 2.0, "{:+.2f}",
                                      "进纸歪斜。正数顺时针，负数逆时针。")
        r += 1
        self.sl_skew_amt = self._p_slider(p, r, "scan", "skew_random", "随机倾斜幅度 (°)",
                                          0.0, 2.0, "{:.2f}",
                                          "每页在 ±该幅度内随机倾斜。\n"
                                          "页面间效果为「整份统一」时整份共用一个角度。")
        r += 1

        ttk.Separator(p, orient="horizontal").grid(row=r, column=0, columnspan=3,
                                                   sticky="ew", pady=PAD)
        r += 1

        # ---- 扫描色彩 ----
        ttk.Label(p, text="扫描色彩", anchor="w").grid(row=r, column=0, sticky="w")
        crow = ttk.Frame(p)
        crow.grid(row=r, column=1, columnspan=2, sticky="w")
        for text, val in (("灰度", "gray"), ("二值", "binary"), ("彩色", "color")):
            ttk.Radiobutton(crow, text=text, value=val, variable=self.scanmode,
                            command=self._on_scanmode).pack(side="left", padx=(0, PAD))
        r += 1

        self.sl_p_thr = self._p_slider(p, r, "scan", "binary_threshold", "二值化阈值",
                                       60, 235, "{:.0f}", "只对二值模式生效。")
        r += 1
        for label, field, lo, hi, fmt in (
                ("纸张暖调", "paper_warmth", 0.0, 1.0, "{:.2f}"),
                ("白平衡偏差", "color_cast", 0.0, 1.0, "{:.2f}"),
                ("饱和度增益", "sat_boost", 0.5, 1.6, "{:.2f}")):
            self._p_slider(p, r, "scan", field, label, lo, hi, fmt,
                           "只对彩色模式生效。")
            r += 1

        ttk.Label(p, style="Hint.TLabel", wraplength=350, justify="left",
                  text="模糊、噪点、灰尘、条纹、倾斜角都已做 DPI 补偿，\n"
                       "与最终输出一致；颗粒与网点的纹理粗细受预览分辨率限制，\n"
                       "会与最终输出略有差异。"
                  ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(PAD, 0))
        r += 1

        self._on_skew_toggle()
        self._on_scanmode()

    # ---- 版式与输出页 ----
    def _build_p_layout(self, p):
        p.columnconfigure(1, weight=1)
        r = 0

        ttk.Label(p, text="纸张", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        cb = ttk.Combobox(p, textvariable=self.papervar, state="readonly", width=24,
                          values=[paper.PAPER_LABELS[k] for k in paper.PAPER_ORDER])
        cb.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._on_paper_change())
        r += 1

        ttk.Label(p, text="自定义尺寸", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        box = ttk.Frame(p)
        box.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)
        self.e_cw = ttk.Entry(box, textvariable=self.cwvar, width=7)
        self.e_cw.pack(side="left")
        ttk.Label(box, text=" × ").pack(side="left")
        self.e_ch = ttk.Entry(box, textvariable=self.chvar, width=7)
        self.e_ch.pack(side="left")
        ttk.Label(box, text=" mm", style="Hint.TLabel").pack(side="left")
        for e in (self.e_cw, self.e_ch):
            e.bind("<Return>", lambda _e: self._on_paper_change())
            e.bind("<FocusOut>", lambda _e: self._on_paper_change())
        r += 1

        ttk.Label(p, text="页面方向", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        box = ttk.Frame(p)
        box.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)
        for val, text in (("auto", "自动"), ("portrait", "纵向"), ("landscape", "横向")):
            ttk.Radiobutton(box, text=text, value=val, variable=self.orientvar,
                            command=lambda: self._on_change("rebuild")).pack(
                side="left", padx=(0, PAD))
        r += 1

        ttk.Label(p, text="缩放方式", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        cb = ttk.Combobox(p, textvariable=self.scalevar, state="readonly", width=24,
                          values=[SCALE_MODES[k] for k in SCALE_MODES])
        cb.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._on_change("rebuild"))
        r += 1

        ttk.Label(p, text="页边距", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        self.sl_margin = SliderRow(p, r, "mm", tk.DoubleVar(value=self.lp.margin_mm),
                                   0.0, 30.0, "{:.1f}", width_label=12,
                                   on_change=self._on_margin)
        r += 1

        ttk.Separator(p, orient="horizontal").grid(row=r, column=0, columnspan=3,
                                                   sticky="ew", pady=PAD)
        r += 1

        ttk.Label(p, text="渲染分辨率", anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        cb = ttk.Combobox(p, textvariable=self.rendervar, state="readonly", width=24,
                          values=["100", "150", "200", "300", "400"])
        cb.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._on_change("rebuild"))
        _Tooltip(cb, "生成页面时的工作分辨率，决定墨点扩散、颗粒、网点这些\n"
                     "按像素定义的量的实际精细程度。\n"
                     "改它需要重新渲染 PDF 页，预览会重建一次（稍慢）。")
        r += 1

        ttk.Label(p, style="Hint.TLabel", wraplength=350, justify="left",
                  text="版式与渲染分辨率只影响预览的取景与精度，不会改变主界面的输出设置；"
                       "点「应用到主界面」才会写回。"
                  ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, PAD))
        r += 1

        self._on_paper_change()

    def _build_right(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x")
        ttk.Button(bar, text="◀ 上一页", width=10,
                   command=lambda: self._goto(self.page_index - 1)).pack(side="left")
        ttk.Button(bar, text="下一页 ▶", width=10,
                   command=lambda: self._goto(self.page_index + 1)).pack(side="left", padx=4)
        self.lbl_page = ttk.Label(bar, text="", style="Hint.TLabel")
        self.lbl_page.pack(side="left", padx=PAD)
        self.lbl_cost = ttk.Label(bar, text="", style="Hint.TLabel")
        self.lbl_cost.pack(side="right")

        holder = ttk.Frame(parent, relief="sunken", borderwidth=1)
        holder.pack(fill="both", expand=True, pady=(PAD, 0))
        # 关掉尺寸传播：否则「图撑大 Label → Label 撑大容器 → 下一帧渲更大的图」
        # 会形成反馈循环，图片越滚越大最后超出窗口。
        holder.pack_propagate(False)
        self._holder = holder
        self.lbl_img = tk.Label(holder, background="#7a7a7a", text="正在准备预览…",
                                foreground="#ffffff", font=("Microsoft YaHei UI", 11))
        self.lbl_img.pack(fill="both", expand=True)
        holder.bind("<Configure>", self._on_resize)

        self.lbl_status = ttk.Label(parent, text="", style="Hint.TLabel",
                                    anchor="w", wraplength=820)
        self.lbl_status.pack(fill="x", pady=(4, 0))

    # ------------------------------------------------------- 滑块工厂（本地）
    def _p_slider(self, parent, row, group, field_name, label, lo, hi, fmt,
                  tip="") -> SliderRow:
        target = self.pp if group == "print" else self.sp
        var = tk.DoubleVar(value=getattr(target, field_name))
        level = "print" if group == "print" else "scan"
        s = SliderRow(parent, row, label, var, lo, hi, fmt, width_label=13, tip=tip,
                      on_change=lambda: self._on_change(level))
        self._p_sliders.append((group, field_name, s))
        self._by_field[field_name] = s
        return s

    def _read_sliders(self):
        for group, field, s in self._p_sliders:
            v = float(s.var.get())
            if field in self.INT_FIELDS:
                v = int(round(v))
            elif field == "paper_base":
                v = round(v, 1)
            else:
                v = round(v, 3)
            setattr(self.pp if group == "print" else self.sp, field, v)
        self.sp.skew_deg = None if self.skew_random.get() else round(
            self.sp.skew_deg or 0.0, 3)

    @staticmethod
    def _key_of(mapping: dict, label: str, default: str) -> str:
        for k, v in mapping.items():
            if v == label:
                return k
        return label if label in mapping else default

    def _paper_key(self) -> str:
        cur = self.papervar.get()
        for k in paper.PAPER_ORDER:
            if paper.PAPER_LABELS[k] == cur:
                return k
        return cur if cur in paper.PAPER_ORDER else "A4"

    def _make_job(self) -> RenderJob:
        """把当前 UI 状态装配成一份完整 RenderJob（供预览会话使用）"""
        self._read_sliders()
        self.pp.mode = self.pmode.get()
        self.pp.paper_type = self._key_of(PRINT_PAPER_TYPES, self.papertype.get(),
                                          "plain")
        self.sp.out_mode = self.scanmode.get()
        self.sp.page_variation = self._key_of(PAGE_VARIATIONS, self.pvary.get(),
                                             "uniform")
        self.lp.paper = self._paper_key()
        self.lp.orientation = self.orientvar.get()
        self.lp.scale_mode = self._key_of(SCALE_MODES, self.scalevar.get(), "fit")
        self.lp.margin_mm = round(float(self.sl_margin.var.get()), 2)
        try:
            self.lp.custom_w_mm = float(self.cwvar.get())
        except ValueError:
            pass
        try:
            self.lp.custom_h_mm = float(self.chvar.get())
        except ValueError:
            pass
        try:
            self.op.render_dpi = int(float(self.rendervar.get()))
        except ValueError:
            pass
        return RenderJob(PrintParams(**vars(self.pp)), ScanParams(**vars(self.sp)),
                         LayoutParams(**vars(self.lp)), OutputParams(**vars(self.op)),
                         pages=self.job.pages)

    # ------------------------------------------------------------ 联动
    def _on_pmode(self):
        """彩色参数对黑白引擎无效：灰掉但保留数值"""
        mono = self.pmode.get() == "mono_laser"
        state = "disabled" if mono else "normal"
        for field in ("misregister", "tac_limit"):
            s = self._by_field.get(field)
            if s is not None:
                s.scale.configure(state=state)

    def _on_skew_toggle(self):
        self.sl_skew.scale.configure(
            state="disabled" if self.skew_random.get() else "normal")
        self.sl_skew_amt.scale.configure(
            state="normal" if self.skew_random.get() else "disabled")
        self._on_change("scan")

    def _on_scanmode(self):
        mode = self.scanmode.get()
        self.sp.out_mode = mode
        thr = self._by_field.get("binary_threshold")
        if thr is not None:
            thr.scale.configure(state="normal" if mode == "binary" else "disabled")
        for field in ("paper_warmth", "color_cast", "sat_boost"):
            s = self._by_field.get(field)
            if s is not None:
                s.scale.configure(state="normal" if mode == "color" else "disabled")
        self._on_change("scan")

    def _on_paper_change(self):
        is_custom = self._paper_key() == "custom"
        state = "normal" if is_custom else "disabled"
        self.e_cw.configure(state=state)
        self.e_ch.configure(state=state)
        self._on_change("rebuild")

    def _on_margin(self):
        self._on_change("rebuild")

    # ------------------------------------------------------------ 变更调度
    def _on_change(self, level: str = "scan"):
        if self._closing:
            return
        self._read_sliders()
        self._show_summary()
        if self.LEVEL_ORDER.get(level, 0) > self.LEVEL_ORDER.get(self._pending_level, 0):
            self._pending_level = level
        self._cancel_timers()
        # 两级渲染：先出低分辨率快图保证手感，停手后再自动精修一版
        self._debounce_id = self.after(55, lambda: self._request_render(quick=True))
        self._quality_id = self.after(520, lambda: self._request_render(quick=False))

    def _cancel_timers(self):
        for attr in ("_debounce_id", "_quality_id"):
            tid = getattr(self, attr, None)
            if tid:
                try:
                    self.after_cancel(tid)
                except Exception:
                    pass
                setattr(self, attr, None)

    def _request_render(self, quick: bool = False):
        if quick:
            self._debounce_id = None
        else:
            self._quality_id = None
        if self._closing:
            return
        if not self._ready:
            return          # 首次会话还没构建完成，等 init 回来再说
        self.token += 1
        scale = self.QUICK_SCALE if quick else 1.0
        level = self._pending_level
        if not quick:
            self._pending_level = "scan"
        self.jobs.put(("render", self.token, self._make_job(), scale, level))

    # ------------------------------------------------------------ 后台线程
    def _start_worker(self):
        self.worker = threading.Thread(target=self._worker_loop, name="preview",
                                       daemon=True)
        self.worker.start()

    def _worker_loop(self):
        while True:
            try:
                msg = self.jobs.get()
            except Exception:
                return
            if msg is None:
                return
            kind = msg[0]
            if kind == "init":
                _, page_index, token, job = msg
                try:
                    s = engine.PreviewSession(self.src, job, page_index=page_index,
                                              max_side=1100)
                    self.results.put(("init", token, s, None))
                except Exception as e:
                    log.exception("预览会话构建失败")
                    self.results.put(("init", token, None, e))
                continue

            # kind == "render"
            _, token, job, scale, level = msg
            # 合并积压请求：参数取最新的那一份，重算级别取最高的那一个
            # （否则拖动打印滑块后紧接着拖动扫描滑块，会把「重跑打印」这一步丢掉）
            while True:
                try:
                    nx = self.jobs.get_nowait()
                except queue.Empty:
                    break
                if nx is None:
                    return
                if nx[0] == "render":
                    _, token, job, scale, level = nx
                else:
                    self.jobs.put(nx)
                    break

            try:
                s = self.session
                if s is None or level == "rebuild":
                    s = engine.PreviewSession(self.src, job,
                                              page_index=self.page_index,
                                              max_side=1100)
                    self.session = s
                    self.results.put(("session", token, s, None))
                elif level == "print":
                    s.reprint(job.print_params)

                t0 = time.perf_counter()
                img = s.rescan(job.scan_params, scale=scale)
                cost = time.perf_counter() - t0
                self.results.put(("render", token, (img, cost, scale), None))
            except Exception as e:
                log.exception("预览渲染失败")
                self.results.put(("render", token, None, e))

    # --------------------------------------------------------------- 轮询
    def _poll(self):
        if self._closing:
            return
        try:
            while True:
                try:
                    kind, token, payload, err = self.results.get_nowait()
                except queue.Empty:
                    break

                if kind in ("init", "session"):
                    if err is not None:
                        self._status(f"预览准备失败：{err}")
                        messagebox.showerror("预览失败", f"{type(err).__name__}: {err}",
                                             parent=self)
                        continue
                    self.session = payload
                    self._ready = True
                    self._update_info()
                    self._update_page_label()
                    if kind == "init":
                        self._pending_level = "scan"
                        self._request_render(quick=False)
                else:
                    if err is not None:
                        self._status(f"渲染失败：{err}")
                        continue
                    img, cost, scale = payload
                    if token < self.shown_token:
                        continue          # 过期的结果，丢弃
                    self.shown_token = token
                    self.last_image = img
                    self.last_cost = cost
                    self.last_scale = scale
                    self._paint(img)
                    self.lbl_cost.configure(
                        text=("快速预览 " if scale < 0.995 else "精修 ") +
                             f"{cost * 1000:.0f} ms")
        except Exception:
            log.exception("预览轮询异常")
        self.after(40, self._poll)

    def _update_info(self):
        info = getattr(self.session, "info", None)
        if not info:
            return
        self.job_page_count = info["page_count"]
        mode_label = {"gray": "灰度", "binary": "黑白二值",
                      "color": "彩色"}.get(self.sp.out_mode, self.sp.out_mode)
        self.lbl_info.configure(text=(
            f"打印：{PRINT_MODES.get(self.pp.mode, '')} · {self.pp.paper_label}\n"
            f"纸张：{info['sheet_mm'][0]:.0f} × {info['sheet_mm'][1]:.0f} mm"
            f"（原页 {info['src_mm'][0]:.0f} × {info['src_mm'][1]:.0f}）\n"
            f"预览 {info['preview_dpi']:.0f} DPI  ·  正式输出 {info['render_dpi']} DPI\n"
            f"输出：{self.op.out_dpi} DPI / {mode_label}\n"
            f"页面间：{self.sp.variation_label}"))

    @staticmethod
    def _to_pil(arr: np.ndarray) -> Image.Image:
        """扫描件数组 → PIL 图像（灰度 (H,W) 或彩色 (H,W,3) 自动识别）"""
        return Image.fromarray(arr, mode="RGB" if arr.ndim == 3 else "L")

    def _paint(self, arr: np.ndarray):
        self._pil = self._to_pil(arr)
        self._is_color = arr.ndim == 3
        self._refresh_photo()

    def _refresh_photo(self):
        if not hasattr(self, "_pil"):
            return
        # 用固定尺寸的容器来算可用面积（不能用 Label 自身，会形成尺寸反馈循环）
        holder = getattr(self, "_holder", None) or self
        w = max(160, holder.winfo_width() - 12)
        h = max(160, holder.winfo_height() - 12)
        disp = self._pil.copy()
        disp.thumbnail((w, h), Image.LANCZOS)
        from PIL import ImageTk
        self._photo = ImageTk.PhotoImage(disp)
        self.lbl_img.configure(image=self._photo, text="")
        quality = "快速预览" if getattr(self, "last_scale", 1.0) < 0.995 else "精修预览"
        kind = "彩色" if getattr(self, "_is_color", False) else "灰度"
        self._status(f"{quality} · {kind}  ·  图像 {self._pil.width}×{self._pil.height} px  ·  "
                     f"显示 {disp.width}×{disp.height} px")

    def _on_resize(self, _evt=None):
        if self._resize_id:
            try:
                self.after_cancel(self._resize_id)
            except Exception:
                pass
        self._resize_id = self.after(160, self._refresh_photo)

    # ------------------------------------------------------------ 操作
    def _goto(self, page_index: int):
        if self.session is None:
            return
        total = getattr(self, "job_page_count", 1)
        if page_index < 0 or page_index >= total:
            return
        self.page_index = page_index
        self.session = None
        self._status(f"正在加载第 {page_index + 1} 页…")
        self.jobs.put(("init", page_index, 0, self._make_job()))
        self._update_page_label()

    def _update_page_label(self):
        total = getattr(self, "job_page_count", 1)
        self.lbl_page.configure(text=f"第 {self.page_index + 1} / {total} 页")

    def _show_summary(self):
        p, s = self.pp, self.sp
        ang = "随机" if self.skew_random.get() else f"{s.skew_deg:+.2f}°"
        self._status(f"浓度 {p.density:.2f}  ·  扩散 {p.spread:.2f}  ·  颗粒 {p.grain:.2f}"
                     f"  ·  模糊 {s.blur:.2f}  ·  噪点 {s.noise:.2f}  ·  倾斜 {ang}")

    def _status(self, text: str):
        # 构建期间（右侧还没建好）可能被提前调用，这里做保护
        lbl = getattr(self, "lbl_status", None)
        if lbl is not None:
            lbl.configure(text=text)

    def _apply_to_main(self):
        """把预览里调好的三页参数写回主界面，直接用于正式处理"""
        job = self._make_job()
        pp, sp, lp = job.print_params, job.scan_params, job.layout_params
        a = self.app

        # ---- 打印 ----
        a.v_mode.set(pp.mode)
        a.v_papertype.set(PRINT_PAPER_TYPES.get(pp.paper_type,
                                               PRINT_PAPER_TYPES["plain"]))
        a.v_density.set(pp.density)
        a.v_pcontrast.set(pp.contrast)
        a.v_spread.set(pp.spread)
        a.v_grain.set(pp.grain)
        a.v_fog.set(pp.fog)
        a.v_halftone.set(pp.halftone)
        a.v_misreg.set(pp.misregister)
        a.v_tac.set(pp.tac_limit)

        # ---- 扫描 ----
        a.v_pvary.set(PAGE_VARIATIONS.get(sp.page_variation,
                                          PAGE_VARIATIONS["uniform"]))
        a.v_paperbase.set(sp.paper_base)
        a.v_cloud.set(sp.paper_cloud)
        a.v_noise.set(sp.noise)
        a.v_dust.set(sp.dust)
        a.v_streak.set(sp.streak)
        a.v_blur.set(sp.blur)
        a.v_scontrast.set(sp.contrast)
        a.v_bright.set(sp.brightness)
        a.v_vignette.set(sp.vignette)
        a.v_jpegq.set(sp.jpeg_quality)
        a.v_threshold.set(sp.binary_threshold)
        a.v_scanmode.set(sp.out_mode)
        a.v_warmth.set(sp.paper_warmth)
        a.v_cast.set(sp.color_cast)
        a.v_sat.set(sp.sat_boost)
        if sp.skew_deg is None:
            a.v_skew_random.set(True)
            a.v_skew_amt.set(sp.skew_random)
        else:
            a.v_skew_random.set(False)
            a.v_skew.set(sp.skew_deg)
            a.v_skew_amt.set(sp.skew_random)

        # ---- 版式 ----
        a.v_paper.set(paper.PAPER_LABELS.get(lp.paper, lp.paper))
        a.v_cw.set(f"{lp.custom_w_mm:g}")
        a.v_ch.set(f"{lp.custom_h_mm:g}")
        a.v_orient.set(lp.orientation)
        a.v_margin.set(lp.margin_mm)
        a.v_scale.set(SCALE_MODES.get(lp.scale_mode, SCALE_MODES["fit"]))

        for s in a._sliders:
            s.refresh()
        a._on_scanmode_change()
        a._on_skew_toggle()
        a._on_mode_change()
        a._on_paper_change()
        a._sync_format()

        a.v_status.set("已把预览参数应用到主界面")
        log.info("预览参数已回填主界面: 打印=%s/%s 浓度=%.2f 模糊=%.2f 噪点=%.2f "
                 "页面间=%s 纸张=%s",
                 pp.mode, pp.paper_type, pp.density, sp.blur, sp.noise,
                 sp.page_variation, lp.paper)
        messagebox.showinfo(
            "已应用",
            "已将预览中调整的参数写回主界面：\n\n"
            f"· 打印：{PRINT_MODES.get(pp.mode, '')} / {pp.paper_label}\n"
            f"· 浓度 {pp.density:.2f}   扩散 {pp.spread:.2f}   颗粒 {pp.grain:.2f}   "
            f"底灰 {pp.fog:.2f}\n"
            f"· 纸底 {sp.paper_base:.0f}   云斑 {sp.paper_cloud:.2f}   "
            f"噪点 {sp.noise:.2f}   灰尘 {sp.dust:.2f}   条纹 {sp.streak:.2f}\n"
            f"· 模糊 {sp.blur:.2f}   倾斜 "
            f"{'随机 ±' + format(sp.skew_random, '.2f') + '°' if sp.skew_deg is None else format(sp.skew_deg, '+.2f') + '°'}\n"
            f"· 页面间：{sp.variation_label}\n"
            f"· 纸张 {paper.PAPER_LABELS.get(lp.paper, lp.paper)}   "
            f"边距 {lp.margin_mm:.1f} mm   缩放 {SCALE_MODES.get(lp.scale_mode, '')}",
            parent=self)

    def _reset(self):
        self.pp = PrintParams(**vars(self.job.print_params))
        self.sp = ScanParams(**vars(self.job.scan_params))
        self.lp = LayoutParams(**vars(self.job.layout_params))
        self.op = OutputParams(**vars(self.job.output_params))
        self.skew_random.set(self.sp.skew_deg is None)
        if self.sp.skew_deg is None:
            self.sp.skew_deg = 0.0

        self.pmode.set(self.pp.mode)
        self.papertype.set(PRINT_PAPER_TYPES.get(self.pp.paper_type,
                                                PRINT_PAPER_TYPES["plain"]))
        self.scanmode.set(self.sp.out_mode)
        self.pvary.set(PAGE_VARIATIONS.get(self.sp.page_variation,
                                           PAGE_VARIATIONS["uniform"]))
        self.papervar.set(paper.PAPER_LABELS.get(self.lp.paper, self.lp.paper))
        self.orientvar.set(self.lp.orientation)
        self.scalevar.set(SCALE_MODES.get(self.lp.scale_mode, SCALE_MODES["fit"]))
        self.rendervar.set(str(self.op.render_dpi))
        self.cwvar.set(f"{self.lp.custom_w_mm:g}")
        self.chvar.set(f"{self.lp.custom_h_mm:g}")
        self.sl_margin.var.set(self.lp.margin_mm)

        for group, field, s in self._p_sliders:
            target = self.pp if group == "print" else self.sp
            s.var.set(getattr(target, field))
            s.refresh()
        self.sl_margin.refresh()

        self._on_pmode()
        self._on_scanmode()
        self._on_skew_toggle()
        self._on_paper_change()
        self._status("已恢复为打开预览时的参数")

    def _save(self):
        if self.last_image is None:
            messagebox.showinfo("提示", "预览还没生成。", parent=self)
            return
        p = filedialog.asksaveasfilename(
            parent=self, title="保存预览图", defaultextension=".png",
            initialfile=f"{self.src.stem}_preview_p{self.page_index + 1}.png",
            filetypes=[("PNG 图片", "*.png"), ("JPEG 图片", "*.jpg")])
        if not p:
            return
        try:
            Image.fromarray(self.last_image,
                            mode="RGB" if getattr(self.last_image, "ndim", 2) == 3
                            else "L").save(p)
            self._status(f"已保存：{p}")
            log.info("预览图已保存: %s", p)
        except Exception as e:
            messagebox.showerror("保存失败", str(e), parent=self)

    def _on_close(self):
        self._closing = True
        self._cancel_timers()
        try:
            self.jobs.put(None)
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass



def main():
    log_file = logsetup.init_logging()
    app = None
    try:
        app = App()
        app.mainloop()
    except Exception:
        logsetup.write_crash_report(*sys.exc_info(), source="gui-main")
        raise
    finally:
        log.info("主循环结束，日志文件: %s", log_file)
