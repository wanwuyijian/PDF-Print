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

from PIL import Image

from . import engine, logsetup, paper
from .config import (BUILTIN_PRESETS, OUTPUT_FORMATS, ORIENTATIONS,
                     PRESET_ORDER, PRINT_MODES, SCALE_MODES, LayoutParams,
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
                 fmt: str = "{:.2f}", width_label: int = 16, tip: str = ""):
        self.var = var
        self.fmt = fmt
        self.tip = tip
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
        self.value_lbl.configure(text=self._text())

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
        self.v_density = tk.DoubleVar(value=1.00)
        self.v_pcontrast = tk.DoubleVar(value=1.00)
        self.v_spread = tk.DoubleVar(value=0.35)
        self.v_grain = tk.DoubleVar(value=0.30)
        self.v_halftone = tk.DoubleVar(value=0.25)
        self.v_misreg = tk.DoubleVar(value=0.50)
        self.v_tac = tk.DoubleVar(value=280)

        # --- 扫描 ---
        self.v_paperbase = tk.DoubleVar(value=246)
        self.v_cloud = tk.DoubleVar(value=0.35)
        self.v_noise = tk.DoubleVar(value=0.30)
        self.v_blur = tk.DoubleVar(value=0.45)
        self.v_scontrast = tk.DoubleVar(value=1.07)
        self.v_bright = tk.DoubleVar(value=1.00)
        self.v_skew = tk.DoubleVar(value=0.0)
        self.v_skew_random = tk.BooleanVar(value=True)
        self.v_skew_amt = tk.DoubleVar(value=0.70)
        self.v_vignette = tk.DoubleVar(value=0.25)
        self.v_jpegq = tk.DoubleVar(value=78)
        self.v_scanmode = tk.StringVar(value="gray")
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
        ttk.Button(bar, text="日志目录", command=self.open_log_dir).pack(side="right", padx=2)

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

        ttk.Separator(p, orient="horizontal").grid(row=r, column=0, columnspan=3,
                                                   sticky="ew", pady=PAD)
        r += 1

        specs = [
            ("浓度（碳粉/墨量）", self.v_density, 0.60, 1.45, "{:.2f}",
             "越大印得越黑越实。小于 1.0 为淡印，大于 1.0 为浓印。"),
            ("阶调对比", self.v_pcontrast, 0.70, 1.40, "{:.2f}",
             "打印机成像的对比度，越大明暗拉得越开。"),
            ("墨点扩散", self.v_spread, 0.00, 1.00, "{:.2f}",
             "油墨在纸纤维上的毛细渗透程度。越大越糊，边缘越软。"),
            ("颗粒感", self.v_grain, 0.00, 1.00, "{:.2f}",
             "碳粉/墨滴转印的随机性，产生颗粒噪声与偶发脏点。"),
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
        self._add_slider(p, r, "四色套印不准", self.v_misreg, 0.0, 1.0, "{:.2f}",
                         "CMYK 四个色版对位偏差，产生彩边。灰度化后边缘会发虚、发脏。")
        r += 1
        self._add_slider(p, r, "总墨量上限 (%)", self.v_tac, 150, 360, "{:.0f}",
                         "四色叠印的总覆盖率上限。越低暗部越糊、越闷。常见值 240~300。")
        r += 1

    def _build_scan_tab(self, p):
        p.columnconfigure(1, weight=1)
        r = 0
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
             "暗部信噪比差，噪声集中在深色区域；另含少量灰尘椒盐点。"),
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
                                            "每页在 ±该幅度内随机倾斜，模拟进纸不齐。")
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
        ttk.Radiobutton(box, text="黑白二值扫描", value="binary",
                        variable=self.v_scanmode).pack(side="left", padx=(PAD, 0))
        _Tooltip(box, "灰度：保留灰阶，最接近真实扫描件。\n"
                      "二值：只有纯黑与纯白，字口硬化，文件体积最小。")
        r += 1
        self._add_slider(p, r, "二值化阈值", self.v_threshold, 60, 235, "{:.0f}",
                         "仅二值模式生效。阈值越高，越多灰阶被判为黑。")
        r += 1

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
        r += 1

        ttk.Checkbutton(p, text="处理完成后自动打开输出目录",
                        variable=self.v_openfolder).grid(
            row=r, column=1, sticky="w", pady=(PAD, 0))

        self._sync_format()
        self._on_paper_change()

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
    def _on_skew_toggle(self):
        random_on = self.v_skew_random.get()
        self.sl_skew.scale.configure(state="disabled" if random_on else "normal")
        self.sl_skew_amt.scale.configure(state="normal" if random_on else "disabled")

    def _on_mode_change(self):
        pass  # 彩色参数对黑白模式无效，但保留数值，切换回来不丢

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
        for key, var in (("density", self.v_density), ("contrast", self.v_pcontrast),
                         ("spread", self.v_spread), ("grain", self.v_grain),
                         ("halftone", self.v_halftone), ("misregister", self.v_misreg),
                         ("tac_limit", self.v_tac)):
            if key in pr:
                var.set(pr[key])

        mapping = {
            "paper_base": self.v_paperbase, "paper_cloud": self.v_cloud,
            "noise": self.v_noise, "blur": self.v_blur, "contrast": self.v_scontrast,
            "brightness": self.v_bright, "vignette": self.v_vignette,
            "jpeg_quality": self.v_jpegq, "binary_threshold": self.v_threshold,
        }
        for key, var in mapping.items():
            if key in sc:
                var.set(sc[key])
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
        self.v_status.set(f"已应用预设：{name}")
        log.info("应用预设: %s", name)

    # ---------------- 文件操作 ----------------
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择 PDF 文件",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")])
        if not paths:
            return
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
            density=round(self.v_density.get(), 3),
            contrast=round(self.v_pcontrast.get(), 3),
            spread=round(self.v_spread.get(), 3),
            grain=round(self.v_grain.get(), 3),
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
            paper_base=round(self.v_paperbase.get(), 1),
            paper_cloud=round(self.v_cloud.get(), 3),
            noise=round(self.v_noise.get(), 3),
            blur=round(self.v_blur.get(), 3),
            contrast=round(self.v_scontrast.get(), 3),
            brightness=round(self.v_bright.get(), 3),
            skew_deg=skew,
            skew_random=skew_rand,
            vignette=round(self.v_vignette.get(), 3),
            jpeg_quality=int(round(self.v_jpegq.get())),
            out_mode=scan_mode,
            binary_threshold=int(round(self.v_threshold.get())),
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
        if engine.parse_page_range(job.pages, 10 ** 9) == []:
            messagebox.showwarning("页码范围无效",
                                   f"「{job.pages}」解析不出任何页码。\n"
                                   "请使用 all 或 1-3,5,8-10 这样的格式。")
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
        messagebox.showinfo(
            "关于",
            f"{APP_NAME}\n版本 {__version__}（{BUILD_DATE}）\n\n"
            f"把 PDF 模拟成「打印后扫描」的黑白扫描件。\n\n"
            f"· 黑白激光 / 彩色激光 / 彩色喷墨 三种打印引擎\n"
            f"· 纸底、云斑、噪点、模糊、倾斜、暗角 全套扫描劣化\n"
            f"· 可指定 A3/A4/A5/A6/B4/B5/16开/Letter 等输出纸张\n"
            f"· 全量运行日志与崩溃日志，便于排查问题\n\n"
            f"日志目录：\n{logsetup.paths.log_dir()}")

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
        self.v_density.set(pp.density)
        self.v_pcontrast.set(pp.contrast)
        self.v_spread.set(pp.spread)
        self.v_grain.set(pp.grain)
        self.v_halftone.set(pp.halftone)
        self.v_misreg.set(pp.misregister)
        self.v_tac.set(pp.tac_limit)

        self.v_paperbase.set(sp.paper_base)
        self.v_cloud.set(sp.paper_cloud)
        self.v_noise.set(sp.noise)
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
        self.v_seed.set(str(op.seed))
        self.v_openfolder.set(op.open_folder_after)
        self.v_pages.set(job.pages)

        for s in self._sliders:
            s.refresh()
        self._on_paper_change()
        self._on_skew_toggle()
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

    左侧是扫描侧参数的实时滑块 —— 拖动的瞬间预览图就跟着变，
    不用反复「改参数 → 关窗 → 重开 → 再看」。
    调满意了点「应用到主界面」，三个核心参数（模糊度 / 噪点 / 倾斜度）
    连同其他扫描参数一并写回主窗口，直接用于正式处理。

    性能做法见 engine.PreviewSession：渲染与打印只算一次，每次只重跑扫描阶段，
    并且整个会话用固定随机种子，保证拖动滑块时噪点图案不会乱跳。
    拖动过程中先出一张低分辨率快图（QUICK_SCALE），停手 0.5 秒后自动精修。
    """

    QUICK_SCALE = 0.60        # 拖动时的快速预览倍率

    def __init__(self, master: "App", src: Path, job: RenderJob, page_index: int = 0):
        super().__init__(master)
        self.app = master
        self.src = src
        self.job = job
        self.page_index = page_index

        self.title(f"效果预览 · 实时调参 —— {src.name}")
        self.geometry("1240x900")
        self.minsize(1000, 720)
        self.transient(master)

        # ---- 本地可变参数（复制一份，不直接改主界面）----
        self.sp = ScanParams(**vars(job.scan_params))
        self.skew_random = tk.BooleanVar(value=(self.sp.skew_deg is None))
        if self.sp.skew_deg is None:
            self.sp.skew_deg = 0.0

        # ---- 线程与请求队列 ----
        self.jobs: queue.Queue = queue.Queue()
        self.results: queue.Queue = queue.Queue()
        self.session: engine.PreviewSession | None = None
        self.token = 0
        self.shown_token = -1
        self.last_image = None            # 原始 PIL 灰度图（用于保存）
        self.last_cost = 0.0
        self.last_scale = 1.0
        self._debounce_id = None
        self._quality_id = None
        self._resize_id = None
        self._closing = False

        self._build_ui()
        self._start_worker()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 初始化：后台构建预览会话
        self._status("正在准备预览…")
        self.jobs.put(("init", self.page_index, 0))
        self.after(50, self._poll)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        outer = ttk.Frame(self, padding=PAD)
        outer.pack(fill="both", expand=True)

        # 左栏可滚动：参数较多，固定高度会把底部按钮挤出可视区
        self.scroll = ScrollFrame(outer, width=322)
        self.scroll.pack(side="left", fill="y", padx=(0, PAD))
        left = self.scroll.inner
        right = ttk.Frame(outer)
        right.pack(side="left", fill="both", expand=True)

        self._build_left(left)
        self._build_right(right)

    def _build_left(self, parent):
        # ---- 信息 ----
        info = ttk.Labelframe(parent, text=" 当前设置 ", padding=PAD)
        info.pack(fill="x")
        self.lbl_info = ttk.Label(info, style="Hint.TLabel", justify="left",
                                  text="加载中…", wraplength=290)
        self.lbl_info.pack(anchor="w")

        # ---- 核心调试：模糊度 / 噪点 / 倾斜度 ----
        core = ttk.Labelframe(parent, text=" 核心调试（拖动即时生效） ", padding=PAD)
        core.pack(fill="x", pady=(PAD, 0))
        core.columnconfigure(1, weight=1)
        self._p_sliders: dict[str, SliderRow] = {}

        r = 0
        self._p_slider(core, r, "blur", "模糊度", 0.0, 2.0, "{:.2f}"); r += 1
        ttk.Label(core, style="Hint.TLabel", wraplength=290, justify="left",
                  text="扫描镜头景深与走纸运动模糊。越大越糊，>1.2 会带方向性拖影。"
                  ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, PAD)); r += 1

        self._p_slider(core, r, "noise", "噪点", 0.0, 1.0, "{:.2f}"); r += 1
        ttk.Label(core, style="Hint.TLabel", wraplength=290, justify="left",
                  text="传感器噪声，集中在深色区域；另含灰尘椒盐点。"
                  ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, PAD)); r += 1

        ttk.Checkbutton(core, text="倾斜度随机生成（每页不同）",
                        variable=self.skew_random,
                        command=self._on_skew_toggle).grid(
            row=r, column=0, columnspan=3, sticky="w"); r += 1
        self.sl_skew = self._p_slider(core, r, "skew_deg", "倾斜度 (°)", -2.0, 2.0,
                                      "{:+.2f}"); r += 1
        ttk.Label(core, style="Hint.TLabel", wraplength=290, justify="left",
                  text="进纸歪斜。取消勾选后拖动滑块，可精确调到想要的角度。"
                  ).grid(row=r, column=0, columnspan=3, sticky="w"); r += 1

        ttk.Label(core, style="Hint.TLabel", wraplength=290, justify="left",
                  text="模糊度、噪点强度、倾斜角度与最终输出一致（已做 DPI 补偿）；\n"
                       "颗粒与网点的纹理粗细受预览分辨率限制，会与最终输出略有差异。"
                  ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(PAD, 0)); r += 1

        # ---- 其他扫描参数 ----
        more = ttk.Labelframe(parent, text=" 其他扫描参数 ", padding=PAD)
        more.pack(fill="both", expand=True, pady=(PAD, 0))
        more.columnconfigure(1, weight=1)
        r = 0
        self._p_slider(more, r, "paper_base", "纸底灰度", 215, 255, "{:.0f}"); r += 1
        self._p_slider(more, r, "paper_cloud", "纸底不均匀", 0.0, 1.0, "{:.2f}"); r += 1
        self._p_slider(more, r, "contrast", "扫描对比度", 0.80, 1.45, "{:.2f}"); r += 1
        self._p_slider(more, r, "brightness", "亮度", 0.85, 1.15, "{:.2f}"); r += 1
        self._p_slider(more, r, "vignette", "边缘暗角", 0.0, 1.0, "{:.2f}"); r += 1
        self._p_slider(more, r, "jpeg_quality", "JPEG 质量", 25, 100, "{:.0f}"); r += 1
        self._p_slider(more, r, "binary_threshold", "二值化阈值", 60, 235, "{:.0f}"); r += 1

        self._on_skew_toggle()

        # ---- 按钮 ----
        btns = ttk.Frame(parent)
        btns.pack(fill="x", pady=(PAD, 0))
        ttk.Button(btns, text="应用到主界面", command=self._apply_to_main).pack(fill="x")
        ttk.Button(btns, text="恢复初始参数", command=self._reset).pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="保存预览图…", command=self._save).pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="关闭", command=self._on_close).pack(fill="x", pady=(10, 0))

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
                                    anchor="w", wraplength=800)
        self.lbl_status.pack(fill="x", pady=(4, 0))

    # ------------------------------------------------------- 滑块工厂（本地）
    def _p_slider(self, parent, row, field_name, label, lo, hi, fmt) -> SliderRow:
        var = tk.DoubleVar(value=getattr(self.sp, field_name))
        s = SliderRow(parent, row, label, var, lo, hi, fmt, width_label=14)
        s.scale.configure(command=lambda _v, f=field_name: self._on_change(f))
        self._p_sliders[field_name] = s
        return s

    def _read_sliders(self):
        for name, s in self._p_sliders.items():
            v = float(s.var.get())
            if name in ("paper_base", "jpeg_quality", "binary_threshold", "skew_deg"):
                v = round(v, 2)
            else:
                v = round(v, 4)
            setattr(self.sp, name, v)
        self.sp.skew_deg = None if self.skew_random.get() else round(self.sp.skew_deg or 0.0, 3)

    def _on_skew_toggle(self):
        self.sl_skew.scale.configure(state="disabled" if self.skew_random.get() else "normal")
        self._on_change("skew_deg")

    def _on_change(self, _field=None):
        if self._closing:
            return
        self._read_sliders()
        self._show_summary()
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
        if self._closing or self.session is None:
            return
        self.token += 1
        scale = self.QUICK_SCALE if quick else 1.0
        self.jobs.put(("render", self.token, ScanParams(**vars(self.sp)), scale))

    # ------------------------------------------------------------ 后台线程
    def _start_worker(self):
        self.worker = threading.Thread(target=self._worker_loop, name="preview",
                                       daemon=True)
        self.worker.start()

    def _worker_loop(self):
        while True:
            try:
                job = self.jobs.get()
            except Exception:
                return
            if job is None:
                return
            kind = job[0]
            if kind == "init":
                _, page_index, token = job
                try:
                    s = engine.PreviewSession(self.src, self.job,
                                              page_index=page_index, max_side=1100)
                    self.results.put(("init", token, s, None))
                except Exception as e:
                    log.exception("预览会话构建失败")
                    self.results.put(("init", token, None, e))
            else:
                _, token, sp, *rest = job
                scale = rest[0] if rest else 1.0
                # 合并积压请求：只渲染最新的那一份
                while True:
                    try:
                        nx = self.jobs.get_nowait()
                    except queue.Empty:
                        break
                    if nx is None:
                        return
                    if nx[0] == "render":
                        token, sp = nx[1], nx[2]
                        scale = nx[3] if len(nx) > 3 else 1.0
                    else:
                        self.jobs.put(nx)
                        break
                s = self.session
                if s is None:
                    continue
                try:
                    t0 = time.perf_counter()
                    img = s.rescan(sp, scale=scale)
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
                if kind == "init":
                    if err is not None:
                        self._status(f"预览准备失败：{err}")
                        messagebox.showerror("预览失败", f"{type(err).__name__}: {err}",
                                             parent=self)
                        continue
                    self.session = payload
                    info = payload.info
                    self.job_page_count = info["page_count"]
                    self.lbl_info.configure(text=(
                        f"打印引擎：{PRINT_MODES.get(self.job.print_params.mode, '')}\n"
                        f"纸张：{info['sheet_mm'][0]:.0f} × {info['sheet_mm'][1]:.0f} mm\n"
                        f"预览 {info['preview_dpi']:.0f} DPI  ·  正式输出 {info['render_dpi']} DPI\n"
                        f"输出：{self.job.output_params.out_dpi} DPI / "
                        f"{'灰度' if self.job.scan_params.out_mode == 'gray' else '黑白二值'}"))
                    self._update_page_label()
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

    def _paint(self, gray):
        from PIL import ImageTk               # 延迟导入，加快启动
        self._pil = Image.fromarray(gray, mode="L")
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
        self._status(f"{quality}  ·  图像 {self._pil.width}×{self._pil.height} px  ·  "
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
        self.jobs.put(("init", page_index, 0))
        self._update_page_label()

    def _update_page_label(self):
        total = getattr(self, "job_page_count", 1)
        self.lbl_page.configure(text=f"第 {self.page_index + 1} / {total} 页")

    def _show_summary(self):
        s = self.sp
        ang = "随机" if self.skew_random.get() else f"{s.skew_deg:+.2f}°"
        self._status(f"模糊度 {s.blur:.2f}   ·   噪点 {s.noise:.2f}   ·   倾斜 {ang}   "
                     f"·   纸底 {s.paper_base:.0f}")

    def _status(self, text: str):
        # 构建期间（右侧还没建好）可能被提前调用，这里做保护
        lbl = getattr(self, "lbl_status", None)
        if lbl is not None:
            lbl.configure(text=text)

    def _apply_to_main(self):
        """把预览里调好的参数写回主界面，直接用于正式处理"""
        self._read_sliders()
        ang = "随机（每页不同）" if self.skew_random.get() else f"{self.sp.skew_deg:+.2f}°"
        a = self.app
        a.v_blur.set(self.sp.blur)
        a.v_noise.set(self.sp.noise)
        a.v_paperbase.set(self.sp.paper_base)
        a.v_cloud.set(self.sp.paper_cloud)
        a.v_scontrast.set(self.sp.contrast)
        a.v_bright.set(self.sp.brightness)
        a.v_vignette.set(self.sp.vignette)
        a.v_jpegq.set(self.sp.jpeg_quality)
        a.v_threshold.set(self.sp.binary_threshold)

        if self.skew_random.get():
            a.v_skew_random.set(True)
        else:
            a.v_skew_random.set(False)
            a.v_skew.set(self.sp.skew_deg or 0.0)
        a._on_skew_toggle()

        for s in a._sliders:
            s.refresh()
        a.v_status.set("已把预览参数应用到主界面")
        log.info("预览参数已回填主界面: 模糊=%.2f 噪点=%.2f 倾斜=%s 纸底=%.0f",
                 self.sp.blur, self.sp.noise,
                 "随机" if self.skew_random.get() else f"{self.sp.skew_deg:+.2f}",
                 self.sp.paper_base)
        messagebox.showinfo(
            "已应用",
            "已将预览中调整的参数写回主界面：\n\n"
            f"· 模糊度：{self.sp.blur:.2f}\n"
            f"· 噪点：{self.sp.noise:.2f}\n"
            f"· 倾斜度：{ang}\n"
            f"· 纸底灰度：{self.sp.paper_base:.0f}   纸底不均匀：{self.sp.paper_cloud:.2f}\n"
            f"· 扫描对比度：{self.sp.contrast:.2f}   亮度：{self.sp.brightness:.2f}\n"
            f"· 边缘暗角：{self.sp.vignette:.2f}   JPEG 质量：{self.sp.jpeg_quality:.0f}",
            parent=self)

    def _reset(self):
        self.sp = ScanParams(**vars(self.job.scan_params))
        self.skew_random.set(self.sp.skew_deg is None)
        if self.sp.skew_deg is None:
            self.sp.skew_deg = 0.0
        for name, s in self._p_sliders.items():
            s.var.set(getattr(self.sp, name))
            s.refresh()
        self._on_skew_toggle()
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
            Image.fromarray(self.last_image, mode="L").save(p)
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
