# -*- coding: utf-8 -*-
"""
打印效果 + 扫描效果 模拟算法

成像物理链路（这是做出「真实感」的关键，而不是简单加个滤镜）：

    纸面反射率  R_paper ≈ 0.96（白纸）
          ↓  打印机上墨：R_sheet = 1 - coverage × (1 - R_ink)
    打印件反射率 R_sheet ∈ [R_ink_floor, 1]
          ↓  扫描仪灰度采样：G = Luminance(R_sheet) × 纸张灰度
    扫描件灰度值 G ∈ [0, 255]

由此自然得到几个真实特征：
    * 黑白激光：黑是真黑（R_ink≈0.045），灰阶靠网点，字口锐利
    * 彩色激光：黑是 CMY 叠印的「合成黑」，灰度化后只到 ~17% 反射率（约 43 灰阶），
                比黑白打印明显发灰；四色套印不准 → 边缘彩边 → 灰化后边缘发虚
    * 彩色喷墨：墨量更足但暗部发闷（反射率下限 0.23），偏青
    * 纸不是纯白：正片叠底后底灰自然下降（245 左右），且带低频云斑

── 页面间一致性（v1.0.5 的核心改动）──────────────────────────────────────
真实的一叠扫描件来自同一批纸、同一次过机：纸底纹理、传感器噪点、偏色方向、
盖板阴影、灰尘位置几乎完全一样，只有进纸歪斜会有几十分之一度的差别。
旧版让一个随机数发生器贯穿所有页面，导致歪斜方向、脏点位置、整体偏色逐页乱跳，
翻页时像在看十几份不同的扫描件 —— 这是「输出太难看」的根源。

现在把随机性拆成两层：
    * 文档级指纹（doc_seed）：纸底、噪点场、暗角方向、白平衡方向、灰尘位置
    * 页面级差异（page_index）：仅在 page_variation 允许时才引入
三种模式见 config.PAGE_VARIATIONS：uniform（整份统一）/ jitter（仅倾斜微抖）/
random（逐页随机，需要「杂乱感」时才用）。
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

from .config import PrintParams, ScanParams

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
REC601 = np.array([0.299, 0.587, 0.114], dtype=np.float32)

# 8×8 Bayer 有序抖动矩阵，用于模拟打印机的半色调网点
_BAYER8 = np.array([
    [0, 32, 8, 40, 2, 34, 10, 42],
    [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38],
    [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41],
    [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37],
    [63, 31, 55, 23, 61, 29, 53, 21],
], dtype=np.float32) / 63.0

_MASK32 = (1 << 32) - 1


# --------------------------------------------------------------------------- #
# 随机性分层：文档级指纹 / 页面级差异
# --------------------------------------------------------------------------- #
def _mix(seed: int, salt: int, page: int = 0) -> int:
    """把 (文档种子, 用途盐, 页序号) 混成一个 32 位种子。

    用大质数乘法而不是 Python 的 hash()：hash() 在同一进程内对不同 int
    是确定的，但跨版本/跨进程不保证，会让「固定种子可复现」这个承诺失效。
    """
    v = (int(seed) & _MASK32) * 2654435761
    v ^= (int(salt) & _MASK32) * 40503
    v ^= (int(page) & _MASK32) * 2246822519
    v &= _MASK32
    # 再打散一轮，避免相邻 salt 只差低位
    v ^= v >> 15
    v = (v * 2246822519) & _MASK32
    v ^= v >> 13
    return int(v) or 1


def page_seed(doc_seed: int, page_index: int, variation: str) -> int:
    """
    某页的成像随机种子。

    uniform / jitter → 与页序号无关（整份共用一套纹理指纹）
    random           → 每页不同
    """
    if variation == "random":
        return _mix(doc_seed, 17, page_index + 1)
    return _mix(doc_seed, 17)


def resolve_skew(sp: ScanParams, doc_seed: int, page_index: int) -> float:
    """
    本页的进纸倾斜角（度）。

    * sp.skew_deg 非 None：固定角度，所有页一致
    * uniform：整份一个角度（同一次进纸、同一台机器，角度基本一致）
    * jitter / random：每页在 ±sp.skew_random 内独立抽取
      —— 只有这里是真实文档里「每页看起来不同」的正当来源
    """
    if sp.skew_deg is not None:
        return float(sp.skew_deg)
    amp = float(sp.skew_random or 0.0)
    if amp <= 0.0:
        return 0.0
    if sp.page_variation == "uniform":
        rng = np.random.default_rng(_mix(doc_seed, 91))
    else:
        rng = np.random.default_rng(_mix(doc_seed, 91, page_index + 1))
    return float(rng.uniform(-amp, amp))


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def pil_to_float(img: Image.Image) -> np.ndarray:
    """PIL 图像 → float32 数组，取值范围 [0,1]"""
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return np.asarray(img, dtype=np.float32) / 255.0


def float_to_pil(arr: np.ndarray) -> Image.Image:
    """float32 [0,1] 数组 → PIL 图像"""
    a = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
    return Image.fromarray(a) if a.ndim == 3 else Image.fromarray(a, mode="L")


def _blur(arr01: np.ndarray, sigma: float) -> np.ndarray:
    """
    对 float32 [0,1] 数组做高斯模糊。
    内部走 PIL 的 C 实现（8bit 中转），精度损失 1/255，对最终 8bit 输出无影响。
    """
    if sigma is None or sigma <= 0.02:
        return arr01
    u8 = np.clip(arr01 * 255.0, 0, 255).astype(np.uint8)
    if u8.ndim == 3:
        img = Image.fromarray(u8, mode="RGB")
        img = img.filter(ImageFilter.GaussianBlur(radius=float(sigma)))
        return np.asarray(img, dtype=np.float32) / 255.0
    img = Image.fromarray(u8, mode="L")
    img = img.filter(ImageFilter.GaussianBlur(radius=float(sigma)))
    return np.asarray(img, dtype=np.float32) / 255.0


def _bayer_tile(h: int, w: int, seed_shift: int = 0) -> np.ndarray:
    """平铺 Bayer 矩阵到 (h,w)"""
    reps_y = h // 8 + 2
    reps_x = w // 8 + 2
    tiled = np.tile(_BAYER8, (reps_y, reps_x))
    if seed_shift:
        tiled = np.roll(tiled, seed_shift, axis=1)
    return tiled[:h, :w]


def _lowfreq_noise(h: int, w: int, rng: np.random.Generator,
                   scale: int = 24) -> np.ndarray:
    """
    生成低频随机场（零均值，范围约 ±1）。
    用于纸张云斑、扫描仪光照不均 —— 真实纸张/扫描件的底色起伏都是低频的。

    scale 的单位是**像素**，所以按比例缩放分辨率时必须同比缩放 scale，
    这样云斑的物理尺寸才不会随 DPI 变化（预览保真的关键之一）。
    """
    scale = max(2, int(scale))
    sh = max(2, h // scale)
    sw = max(2, w // scale)
    small = rng.normal(0.0, 1.0, (sh, sw)).astype(np.float32)
    img = Image.fromarray(np.clip((small * 40 + 128), 0, 255).astype(np.uint8), mode="L")
    img = img.resize((w, h), Image.BICUBIC)
    out = (np.asarray(img, dtype=np.float32) - 128.0) / 40.0
    return out


def _smooth1d(n: int, rng: np.random.Generator, scale: int = 8) -> np.ndarray:
    """一维平滑随机序列（零均值），用于纵向条纹的列增益起伏"""
    scale = max(1, int(scale))
    m = max(2, n // scale)
    small = rng.normal(0.0, 1.0, (1, m)).astype(np.float32)
    img = Image.fromarray(np.clip(small * 40 + 128, 0, 255).astype(np.uint8), mode="L")
    img = img.resize((n, 1), Image.BICUBIC)
    return (np.asarray(img, dtype=np.float32)[0] - 128.0) / 40.0


def _highfreq_noise(h: int, w: int, rng: np.random.Generator) -> np.ndarray:
    return rng.normal(0.0, 1.0, (h, w)).astype(np.float32)


def _highfreq_noise3(h: int, w: int, rng: np.random.Generator) -> np.ndarray:
    """三通道各自独立的传感器噪点。

    彩色扫描噪点**不能**三通道共用同一个噪声图 —— 那样只会改变亮度、不产生色噪，
    看起来像加了灰蒙层。真实传感器的 RGB 读出电路是独立的，暗部会出现彩色噪点。
    """
    return np.stack([_highfreq_noise(h, w, rng) for _ in range(3)], axis=-1)


def _tone_curve(x: np.ndarray, contrast: float, lift: float = 0.0,
                pivot: float = 0.5) -> np.ndarray:
    """
    阶调曲线。

    contrast: 以 pivot 为轴做对比度拉伸
    lift:     暗部抬升（彩色打印暗部密度不足、发闷）

    注意 lift 必须**锚定端点**：x=0 与 x=1 处不产生变化。
    否则会出现致命错误 —— 白纸（墨量 0）被抬出墨量，整张纸底发灰。
    这里用 4x(1-x)² 的形状，峰值在 x≈1/3，恰好只抬暗调、不动高光与纯黑。
    """
    y = np.clip((x - pivot) * contrast + pivot, 0.0, 1.0)
    if lift > 0.0:
        shape = 4.0 * y * (1.0 - y) ** 2      # x=0→0, x=1→0, 峰值约 x=1/3
        y = np.clip(y + lift * shape, 0.0, 1.0)
    return y


# --------------------------------------------------------------------------- #
# 打印效果
# --------------------------------------------------------------------------- #
def _print_mono(rgb: np.ndarray, pp: PrintParams,
                rng: np.random.Generator, dpi_scale: float = 1.0) -> np.ndarray:
    """黑白激光打印机：单色碳粉转印"""
    floor, spread_base, _misreg, _sat, _cast = pp.trait()
    spread_mul, grain_mul = pp.paper_traits()
    h, w = rgb.shape[:2]

    # 1) 灰度化（打印机驱动做色彩→灰度的转换）
    lum = rgb @ REC601

    # 2) 半色调网点：打印机用网点表达灰阶，先在连续调上叠加网点扰动
    cov = 1.0 - lum                       # 覆盖率：越黑墨越多
    if pp.halftone > 0.001:
        band = 4.0 * cov * (1.0 - cov)     # 中间调权重（纯黑/纯白不抖动）
        cov = cov + (_bayer_tile(h, w, rng.integers(0, 8)) - 0.5) * pp.halftone * band * 0.85

    # 3) 浓度 + 对比度：碳粉成像的阶调特性
    cov = _tone_curve(np.clip(cov, 0, 1),
                      contrast=0.90 + 0.32 * pp.contrast,
                      pivot=0.42)
    cov = np.clip(cov * pp.density, 0.0, 1.0)

    # 4) 墨粉扩散（纸纤维毛细渗透 / 定影压力不均）。涂布纸几乎不渗，草纸渗得厉害
    cov = _blur(cov, sigma=spread_base * (0.35 + pp.spread * 1.9) * spread_mul * dpi_scale)

    # 5) 碳粉颗粒：单个碳粉颗粒是否转印成功具有随机性
    if pp.grain > 0.001:
        cov = cov + _highfreq_noise(h, w, rng) * 0.085 * pp.grain * grain_mul \
            * np.clip(cov * 2.2, 0, 1)
        cov = np.clip(cov, 0.0, 1.0)
        # 高光区偶发掉粉（脏点）；概率按面积计，低分辨率预览下要按比例放大，
        # 否则同一张纸在预览里的脏点会明显少于最终输出。
        dirt = rng.random((h, w)).astype(np.float32) \
            < (0.00035 * pp.grain * grain_mul / dpi_scale ** 2)
        cov = np.where(dirt, np.maximum(cov, 0.55), cov)

    # 6) 底灰（fog）：定影不净时白底会浮一层极淡的碳粉雾。
    #    幅度必须极小（≤3 个灰阶），否则白纸变灰纸。
    if pp.fog > 0.001:
        veil = _lowfreq_noise(h, w, rng, scale=max(2, int(round(40 * dpi_scale))))
        cov = np.clip(cov + pp.fog * 0.030 * (0.65 + 0.35 * veil), 0.0, 1.0)

    # 7) 反射率合成（碳粉反射率 floor ≈ 0.045）
    refl = 1.0 - cov * (1.0 - floor)
    return np.stack([refl, refl, refl], axis=-1)


def _print_color(rgb: np.ndarray, pp: PrintParams,
                 rng: np.random.Generator, dpi_scale: float = 1.0) -> np.ndarray:
    """彩色激光 / 彩色喷墨：CMYK 减色法叠印"""
    floor, spread_base, misreg, sat_gain, cast = pp.trait()
    spread_mul, grain_mul = pp.paper_traits()
    h, w = rgb.shape[:2]

    # 1) 打印机色彩管理：彩度增益（打印机色域压缩后再拉伸，颜色会「艳而闷」）
    lum = (rgb @ REC601)[..., None]
    rgb2 = np.clip(lum + (rgb - lum) * sat_gain, 0.0, 1.0)

    # 2) 墨量模型（CMY）
    ink = 1.0 - rgb2

    # 3) 总墨量限制 TAC：四色叠印时暗部墨量超限 → 糊成一坨
    limit = pp.tac_limit / 100.0
    total = ink.sum(axis=-1, keepdims=True)
    over = total > limit
    ink = np.where(over, ink * (limit / np.maximum(total, 1e-6)), ink)

    # 4) 阶调曲线 + 墨量增益；彩色打印暗部密度不足 → 抬暗部（lift）
    ink = _tone_curve(ink,
                      contrast=0.92 + 0.26 * pp.contrast,
                      lift=0.10 + 0.10 * (1.0 - min(pp.density, 1.3) / 1.3),
                      pivot=0.40)
    ink = np.clip(ink * pp.density, 0.0, 1.0)

    # 5) 四色套印不准：每个色版扩散程度不同 + 亚像素位移 → 边缘彩边
    chan_sigma = spread_base * (0.35 + pp.spread * 1.9) * spread_mul * dpi_scale
    shift_px = int(round(misreg * pp.misregister * 2.0 * dpi_scale))
    for i in range(3):
        s = chan_sigma * (0.78 + 0.22 * i + 0.18 * pp.misregister * (i - 1))
        ch = _blur(ink[..., i], sigma=max(0.0, s))
        if shift_px:
            d = shift_px if i == 0 else (-shift_px if i == 1 else 0)
            if d:
                ch = np.roll(ch, d, axis=1)
                if d > 0:
                    ch[:, :d] = ch[:, d:d + 1]
                else:
                    ch[:, d:] = ch[:, d - 1:d]
        ink[..., i] = ch

    # 6) 墨滴/碳粉颗粒
    if pp.grain > 0.001:
        ink = np.clip(ink + _highfreq_noise(h, w, rng)[..., None]
                      * 0.06 * pp.grain * grain_mul
                      * np.clip(ink.sum(-1, keepdims=True), 0, 1.6),
                      0.0, 1.0)

    # 7) 反射率 = 1 - 墨量×(1-反射率下限)，并保证不低于墨的反射率下限（合成黑发灰的根源）
    refl = 1.0 - ink * (1.0 - floor)
    refl = np.maximum(refl, floor)

    # 8) 底灰：彩色机四色版都会留一点点底，暗部之外的白底也会脏
    if pp.fog > 0.001:
        veil = _lowfreq_noise(h, w, rng, scale=max(2, int(round(40 * dpi_scale))))
        refl = refl * (1.0 - pp.fog * 0.028 * (0.65 + 0.35 * veil))[..., None]

    # 9) 色偏（各色版增益偏差）：喷墨偏青、激光偏冷
    refl = np.clip(refl + np.array(cast, dtype=np.float32).reshape(1, 1, 3), 0.0, 1.0)
    return refl


def apply_print_effect(rgb: np.ndarray, pp: PrintParams,
                       rng: np.random.Generator, dpi_scale: float = 1.0) -> np.ndarray:
    """
    应用打印效果。

    :param rgb: float32 (H,W,3) 页面内容（sRGB，[0,1]，1=白纸）
    :param dpi_scale: 渲染分辨率 / 标称分辨率。所有以**像素**为单位的量
                      （墨点扩散、套印位移、脏点密度）都按此比例换算，
                      保证不同 DPI 下渲染出的物理效果一致。
    :return:    float32 (H,W,3) 打印到纸面后的**反射率**图，[0,1]，1=白纸反射
    """
    if pp.mode == "mono_laser":
        return _print_mono(rgb, pp, rng, dpi_scale)
    return _print_color(rgb, pp, rng, dpi_scale)


def print_effect_grayscale(rgb: np.ndarray, pp: PrintParams,
                           rng: np.random.Generator,
                           dpi_scale: float = 1.0) -> np.ndarray:
    """
    打印效果 + 扫描仪黑白模式的灰度采样。
    黑白扫描件最终看的是亮度，所以彩色的差别体现在「同一颜色灰化后深浅不同」。
    """
    refl = apply_print_effect(rgb, pp, rng, dpi_scale)
    if pp.mode == "mono_laser":
        return refl[..., 0]
    return refl @ REC601


# --------------------------------------------------------------------------- #
# 扫描效果
# --------------------------------------------------------------------------- #
def _paper_texture(h: int, w: int, sp: ScanParams,
                   rng: np.random.Generator, dpi_scale: float = 1.0,
                   channels: int = 1) -> np.ndarray:
    """
    纸张底色场（反射率平面，1.0 = 纸基标准白）。
    = 纸基灰度 × (1 + 低频云斑 + 高频纤维纹)

    channels=3 时额外叠加**光谱色调**：纸张本身不是中性白，
    加上扫描仪灯管色温偏暖，彩色扫描出来的纸底通常 R>G>B（偏暖黄）。
    这是彩色扫描件真实感的关键之一 —— 纯中性灰的纸底一眼就是数字生成的。
    """
    base = np.clip(sp.paper_base, 150.0, 255.0) / 255.0
    tex = np.ones((h, w), dtype=np.float32) * base

    if sp.paper_cloud > 0.001:
        # 三个尺度叠加：扫描灯管不均(粗) + 纸张定量不匀(中) + 纤维纹理(细)
        # scale 按 dpi_scale 同比缩放，云斑的物理尺寸才不会随分辨率变化
        s1 = max(2, int(round(26 * dpi_scale)))
        s2 = max(2, int(round(11 * dpi_scale)))
        s3 = max(2, int(round(5 * dpi_scale)))
        cloud = (0.75 * _lowfreq_noise(h, w, rng, scale=s1)
                 + 0.50 * _lowfreq_noise(h, w, rng, scale=s2)
                 + 0.30 * _lowfreq_noise(h, w, rng, scale=s3))
        # 幅度必须很小：真实扫描件纸底起伏通常只有 ±2~6 个灰阶，
        # 幅度过大就会变成「发霉的脏抹布」而不是白纸。
        tex = tex * (1.0 + cloud * 0.020 * sp.paper_cloud)

    # 高频纤维纹理（极弱，只增加质感）
    tex = tex * (1.0 + _highfreq_noise(h, w, rng) * 0.005)

    if channels >= 3:
        warmth = float(np.clip(sp.paper_warmth, 0.0, 1.0))
        # 暖调：红/绿略升、蓝明显降。幅度取 ±3~6%，再大就成「泛黄的旧纸」了
        gains = np.array([1.0 + 0.030 * warmth,
                          1.0 + 0.004 * warmth,
                          1.0 - 0.055 * warmth], dtype=np.float32)
        # 云斑色调也跟着轻微起伏（涂布不均会造成细微色差）
        tex = tex[..., None] * gains.reshape(1, 1, 3)
        if sp.paper_cloud > 0.001:
            tint = _lowfreq_noise(h, w, rng, scale=max(2, int(round(18 * dpi_scale))))
            tex = tex * (1.0 + tint[..., None] * 0.008 * sp.paper_cloud
                         * np.array([1.0, 0.35, -0.85], dtype=np.float32).reshape(1, 1, 3))

    return np.clip(tex, 0.05, 1.0)


def _apply_skew(img: Image.Image, angle_deg: float,
                fill_gray) -> Image.Image:
    """
    进纸倾斜：绕中心旋转并保持画布尺寸（超出的边角被裁掉，露出的一角补纸色）。
    这正是扫描件的真实表现 —— 内容不会因为歪了而缩小。
    """
    if abs(angle_deg) < 0.01:
        return img
    return img.rotate(float(angle_deg), resample=Image.BICUBIC,
                      expand=False, fillcolor=fill_gray)


def _apply_vignette(arr: np.ndarray, strength: float,
                    rng: np.random.Generator, channels: int = 1) -> np.ndarray:
    """
    边缘照度衰减 + 单侧装订阴影。扫描仪光源在边缘衰减，盖板压不实的一侧更暗。

    channels=3 时三通道衰减量略有差异：灯管两端的色温会漂移，
    真实彩色扫描件四角的颜色往往和中心对不上。
    """
    if strength <= 0.001:
        return arr
    h, w = arr.shape[:2]
    yy = np.linspace(-1.0, 1.0, h, dtype=np.float32)[:, None]
    xx = np.linspace(-1.0, 1.0, w, dtype=np.float32)[None, :]
    r2 = np.clip(xx * xx * 0.85 + yy * yy * 0.85, 0.0, 1.4)
    vig = 1.0 - strength * 0.13 * (r2 ** 1.25)

    # 单侧阴影（左右随机，模拟装订侧/盖板侧）
    axis = int(rng.integers(0, 2))
    ramp = np.linspace(0.0, 1.0, w if axis == 0 else h, dtype=np.float32) ** 1.6
    if rng.random() < 0.5:
        ramp = ramp[::-1]
    ramp = ramp.reshape(1, -1) if axis == 0 else ramp.reshape(-1, 1)
    vig = vig * (1.0 - strength * 0.075 * ramp)

    if channels >= 3:
        # 每通道 ±12% 的相对差异，方向随机 → 角落出现轻微色彩漂移
        rel = 1.0 + (rng.random(3).astype(np.float32) - 0.5) * 0.24 * strength
        vig = vig[..., None] * rel.reshape(1, 1, 3)
    return arr * vig


def _apply_streaks(arr: np.ndarray, amount: float,
                   rng: np.random.Generator, dpi_scale: float = 1.0,
                   channels: int = 1) -> np.ndarray:
    """
    纵向条纹：CIS/CCD 传感器逐列增益标定不均、走纸划痕、灯管脏污。

    这是老式扫描仪最典型的毛病，而且**方向永远是走纸方向**（竖直），
    与内容无关 —— 所以必须放在倾斜之后按扫描坐标施加，而不是跟着纸转。
    """
    if amount <= 0.001:
        return arr
    h, w = arr.shape[:2]

    # 1) 全幅缓慢的列增益起伏
    g = _smooth1d(w, rng, scale=max(2, int(round(12 * dpi_scale))))
    gain = 1.0 + g * 0.030 * amount

    # 2) 少数几根明显的亮线/暗线（灯管划伤、镜头脏点）
    n_lines = int(rng.integers(1, 4))
    for _ in range(n_lines):
        x = int(rng.integers(0, max(1, w)))
        width = max(1, int(round(rng.uniform(0.6, 1.8) * max(0.4, dpi_scale))))
        amp = float(rng.uniform(0.025, 0.085)) * amount * (1 if rng.random() < 0.5 else -1)
        x0, x1 = max(0, x - width // 2), min(w, x + width // 2 + 1)
        gain[x0:x1] += amp

    gain = np.clip(gain, 0.75, 1.25).astype(np.float32)
    if channels >= 3:
        # 各通道的列增益不完全相同 → 条纹带一点颜色
        tint = 1.0 + (rng.random(3).astype(np.float32) - 0.5) * 0.5 * amount
        gain = gain[..., None] * tint.reshape(1, 1, 3)
    else:
        gain = gain.reshape(1, -1)
    return arr * gain


def _dust_layer(h: int, w: int, sp: ScanParams, rng: np.random.Generator,
                dpi_scale: float = 1.0):
    """
    灰尘 / 污渍层。

    与传感器噪点分开：噪点是逐像素的高频抖动，而灰尘是**成块的**污点 ——
    纸面粉尘、复印机鼓面残留、盖板玻璃上的毛屑。

    直接在成品分辨率上逐个画斑点（圆形、边缘柔和衰减），而不是在低分辨率网格
    上撒点再放大 —— 后者放大出来是硬边方块，一眼就是「马赛克噪点」。
    """
    density = float(np.clip(sp.dust, 0.0, 1.0))
    if density <= 0.001:
        return None

    scale = max(0.4, dpi_scale)

    # 斑点数按物理面积给：dust=1 时约 0.6 个/cm²。A4@200dpi 约 620cm²，
    # 也就是 ~370 个点 —— 一页上零星可见的脏污。
    # 早期版本按 3 个/cm² 给，整页近两千个点，看起来像下雪，不像扫描件。
    # 注意 1 in² = 6.4516 cm²，是乘不是除 —— 除反了会让点数少两个数量级，
    # 小图（自检用的测试页）直接归零，滑块看起来完全没反应。
    in2 = (h / (200.0 * scale)) * (w / (200.0 * scale))
    area_cm2 = in2 * 6.4516
    n_dark = int(round(area_cm2 * density * 0.6))
    n_light = int(round(n_dark * 0.45))

    dark = np.zeros((h, w), dtype=np.float32)
    light = np.zeros((h, w), dtype=np.float32)

    def _sprinkle(canvas: np.ndarray, count: int) -> None:
        if count <= 0:
            return
        ys = rng.integers(0, h, count)
        xs = rng.integers(0, w, count)
        # 半径：多数是 0.3~0.8mm 的细尘，少数是 1~2mm 的纸屑/毛屑
        radii = rng.uniform(1.0, 2.6, count) * scale
        big = rng.random(count) < 0.08
        radii[big] = rng.uniform(3.0, 6.5, int(big.sum())) * scale
        # 强度也随机：有的只是淡影，有的是实心黑点
        amps = rng.uniform(0.30, 1.0, count)
        for y, x, rad, amp in zip(ys, xs, radii, amps):
            r = int(np.ceil(rad * 1.8))
            y0, y1 = max(0, y - r), min(h, y + r + 1)
            x0, x1 = max(0, x - r), min(w, x + r + 1)
            if y0 >= y1 or x0 >= x1:
                continue
            yy = np.arange(y0, y1, dtype=np.float32) - y
            xx = np.arange(x0, x1, dtype=np.float32) - x
            d2 = (yy[:, None] ** 2 + xx[None, :] ** 2) / (rad * rad)
            # 高斯衰减：中心实、边缘虚，不会留下方块轮廓
            spot = np.exp(-d2 * 1.6).astype(np.float32) * amp
            region = canvas[y0:y1, x0:x1]
            np.maximum(region, spot, out=region)

    _sprinkle(dark, n_dark)
    _sprinkle(light, n_light)
    return dark, light


def _color_grade(arr3: np.ndarray, sp: ScanParams,
                 rng: np.random.Generator) -> np.ndarray:
    """
    彩色扫描特有的两项色度处理：白平衡偏差 + 饱和度增益。

    没有这一步，彩色扫描件会「太准」—— 三通道增益完全一致、饱和度原样保留，
    反而一眼看出是程序生成的。真实扫描仪的白平衡总有偏差，
    自动校色也总会把饱和度推离原值。
    """
    amp = float(np.clip(sp.color_cast, 0.0, 1.0))
    if amp > 0.001:
        # 从 青 / 品红 / 黄 三个方向里随机挑一个，幅度由 color_cast 控制。
        # 用随机方向而不是固定偏色，避免所有输出都朝同一边偏、显得规律。
        dirs = np.array([[-1.0, +1.0, +1.0],     # 偏青（红通道弱）
                         [+1.0, -1.0, +1.0],     # 偏品红（绿通道弱）
                         [+1.0, +1.0, -1.0]], dtype=np.float32)
        pick = dirs[int(rng.integers(0, 3))]
        gains = 1.0 + pick * amp * 0.045
        arr3 = arr3 * gains.reshape(1, 1, 3)

    sat = float(sp.sat_boost)
    if abs(sat - 1.0) > 0.001:
        lum = (arr3 @ REC601)[..., None]
        arr3 = lum + (arr3 - lum) * sat

    return np.clip(arr3, 0.0, 1.0)


def apply_scan_effect(sheet01: np.ndarray, sp: ScanParams,
                      rng: np.random.Generator,
                      log_hook=None, dpi_scale: float = 1.0,
                      skew_deg: float | None = None) -> np.ndarray:
    """
    应用扫描效果。

    :param sheet01:   打印到纸面后的**反射率**图，float32 [0,1]。
                      · (H,W)   灰度反射率
                      · (H,W,3) RGB 反射率（彩色打印引擎的原始输出）
    :param sp:        扫描参数
    :param dpi_scale: 渲染分辨率 / 标称分辨率。光学模糊 sigma、椒盐点密度都按此换算，
                      保证预览与最终输出看到的是同一个物理效果。
    :param skew_deg:  本页倾斜角。None 时按 sp.skew_deg / sp.skew_random 自行抽取；
                      由调用方传入是为了让「整份统一」模式下所有页面用同一个角度，
                      而「仅倾斜微抖」模式下只有这一项逐页不同（其余纹理完全一致）。
    :return:          uint8。
                      · out_mode=color 且输入为三通道 → (H,W,3) RGB 彩色扫描件
                      · 其余情况 → (H,W) 8bit 灰度扫描件

    说明：彩色信息**只有**在 out_mode=color 时才会保留三通道。
    灰度与二值模式依然先按 Rec.601 亮度采样 —— 这是扫描仪黑白模式的实际行为，
    彩色内容灰化后深浅不同（红字偏浅、黄字几乎消失）正是由这一步产生的。
    """
    def _log(msg: str) -> None:
        if log_hook:
            log_hook(msg)

    color = bool(sheet01.ndim == 3 and sp.is_color)
    if sheet01.ndim == 3 and not color:
        # 灰度 / 二值模式：扫描仪按亮度采样，三通道塌缩为单通道
        sheet01 = sheet01 @ REC601

    ch = 3 if color else 1
    h, w = sheet01.shape[:2]

    # 1) 纸底正片叠底：纸反射率 × 墨迹反射率
    _log("  扫描: 纸底合成" + ("（彩色）" if color else ""))
    sheet = np.clip(sheet01, 0.0, 1.0) * _paper_texture(
        h, w, sp, rng, dpi_scale, channels=ch)

    # 2) 光学模糊（景深 / 镜头 MTF / 走纸运动模糊）
    _log("  扫描: 光学模糊")
    blur_px = sp.blur * dpi_scale
    sheet = _blur(sheet, sigma=blur_px)
    if blur_px > 1.2:                      # 强模糊时补一点运动方向模糊
        u8 = np.clip(sheet * 255, 0, 255).astype(np.uint8)
        im = Image.fromarray(u8, mode="RGB" if color else "L").filter(
            ImageFilter.BoxBlur(radius=blur_px * 0.5))
        sheet = np.asarray(im, dtype=np.float32) / 255.0

    # 3) 进纸倾斜（几何变形要在加噪之前，噪声属于传感器、不该被重采样）
    angle = skew_deg
    if angle is None:
        angle = sp.skew_deg
    if angle is None:
        angle = float(rng.uniform(-sp.skew_random, sp.skew_random)) \
            if sp.skew_random > 0 else 0.0
    if abs(angle) > 0.01:
        _log(f"  扫描: 进纸倾斜 {angle:+.2f}°")
        mode = "RGB" if color else "L"
        tmp = Image.fromarray(np.clip(sheet * 255, 0, 255).astype(np.uint8), mode=mode)
        fill = int(np.clip(sp.paper_base, 0, 255))
        # 彩色模式下补边要带上纸底色调，否则歪出来的角是中性灰、很突兀
        tmp = _apply_skew(tmp, angle, (fill, fill, fill) if color else fill)
        sheet = np.asarray(tmp, dtype=np.float32) / 255.0

    # 4) 纵向条纹（传感器列增益不均，在扫描坐标里，必须晚于倾斜）
    if sp.streak > 0.001:
        _log("  扫描: 纵向条纹")
        sheet = _apply_streaks(sheet, sp.streak, rng, dpi_scale, channels=ch)

    # 5) 传感器噪点（高斯，暗部信噪比差 → 与 (1 - 亮度) 相关，白纸几乎干净）
    _log("  扫描: 传感器噪点")
    if sp.noise > 0.001:
        lum = sheet if not color else sheet @ REC601
        gain = (0.35 + 0.65 * (1.0 - lum))
        nz = _highfreq_noise3(h, w, rng) if color else _highfreq_noise(h, w, rng)
        amp = 0.030 * sp.noise
        sheet = sheet + nz * (amp if not color else amp * 1.35) * (
            gain if not color else gain[..., None])
        sheet = np.clip(sheet, 0.0, 1.0)

    # 6) 灰尘 / 污渍（成块斑点，与逐像素噪点分开）
    if sp.dust > 0.001:
        _log("  扫描: 灰尘污渍")
        layer = _dust_layer(h, w, sp, rng, dpi_scale)
        if layer is not None:
            dark, light = layer
            strength = 0.55 + 0.35 * sp.dust
            if color:
                sheet = sheet * (1.0 - dark[..., None] * strength)
                sheet = sheet + (1.0 - sheet) * (light[..., None] * strength * 0.8)
            else:
                sheet = sheet * (1.0 - dark * strength)
                sheet = sheet + (1.0 - sheet) * (light * strength * 0.8)
            sheet = np.clip(sheet, 0.0, 1.0)

    # 7) 边缘暗角
    if sp.vignette > 0.001:
        sheet = _apply_vignette(sheet, sp.vignette, rng, channels=ch)

    # 8) 扫描仪自动曝光的阶调调整（对比度/亮度）
    _log("  扫描: 阶调调整")
    sheet = np.clip(sheet * sp.brightness, 0.0, 1.0)
    sheet = np.clip((sheet - 0.5) * sp.contrast + 0.5, 0.0, 1.0)

    # 9) 彩色专用：白平衡偏差 + 饱和度
    if color:
        _log("  扫描: 白平衡 / 饱和度")
        sheet = _color_grade(sheet, sp, rng)

    out = np.clip(sheet * 255.0, 0, 255)

    # 10) 二值化（黑白二值扫描模式；彩色模式不适用）
    if sp.is_binary:
        _log(f"  扫描: 二值化 (阈值 {sp.binary_threshold})")
        # 阈值加一点空间扰动，避免边缘过于"数学化"
        thr = float(np.clip(sp.binary_threshold, 1, 254))
        jitter = _highfreq_noise(h, w, rng) * 4.0
        out = np.where(out + jitter < thr, 0.0, 255.0)

    return out.astype(np.uint8)


# --------------------------------------------------------------------------- #
# 便捷入口：一步完成「打印 → 扫描」
# --------------------------------------------------------------------------- #
def simulate(rgb: np.ndarray, pp: PrintParams, sp: ScanParams,
             rng: np.random.Generator | None = None,
             log_hook=None, dpi_scale: float = 1.0,
             skew_deg: float | None = None) -> np.ndarray:
    """
    :param rgb: float32 (H,W,3) 页面内容
    :return:    uint8。彩色扫描 → (H,W,3)；灰度/二值扫描 → (H,W)
    """
    rng = rng or np.random.default_rng()
    if sp.is_color:
        # 彩色扫描：打印反射率保持三通道，不做灰度化
        sheet = apply_print_effect(rgb, pp, rng, dpi_scale)
    else:
        # 灰度/二值：先按亮度采样（扫描仪黑白模式的实际行为）
        sheet = print_effect_grayscale(rgb, pp, rng, dpi_scale)
    return apply_scan_effect(sheet, sp, rng, log_hook=log_hook,
                             dpi_scale=dpi_scale, skew_deg=skew_deg)


__all__ = [
    "pil_to_float", "float_to_pil", "apply_print_effect",
    "print_effect_grayscale", "apply_scan_effect", "simulate",
    "page_seed", "resolve_skew",
]
