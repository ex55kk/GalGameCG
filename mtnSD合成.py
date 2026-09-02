# -*- coding: utf-8 -*-
"""
SD Motion GUI Editor (Tkinter + Pillow)

Features (MVP, usable):
- Left: preview canvas (composited image)
- Bottom-left: select "preview item" (motion), choose frame/time (slider + entry)
- Right: layer list populated from JSON at current time (auto-filled with position/opacity/bm/weight)
- Can add external PNG as a new layer
- Can edit selected layer per-motion: position (tx,ty), scale (sx,sy), angle, pivot (ox,oy),
  opacity, bm, weight(order), visible
- Export current preview to PNG (with your edits)
- Export all "SDxxxx*" motions at last frame (optional batch)

Notes:
- This tool is for manual correction and preview. It does NOT implement stencil/masks/ccc/zcc curves.
- Supports bm=19 additive blend; other bm treated as normal.
- Coordinate system: coord(0,0) is screen center.

Run:
  python sd_motion_gui_editor.py
"""

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk, ImageChops


# ---------------- math / transform ----------------

def deg2rad(deg: float) -> float:
    return deg * math.pi / 180.0

def rotate_point(x: float, y: float, ang_rad: float) -> Tuple[float, float]:
    c = math.cos(ang_rad)
    s = math.sin(ang_rad)
    return (x * c - y * s, x * s + y * c)

State = Tuple[float, float, float, float, float]  # tx, ty, sx, sy, ang_deg

def compose_state(parent: State, local: Dict[str, Any]) -> State:
    p_tx, p_ty, p_sx, p_sy, p_ang = parent
    l_coord = local.get("coord", [0, 0, 0])
    l_tx = float(l_coord[0]) if len(l_coord) >= 1 else 0.0
    l_ty = float(l_coord[1]) if len(l_coord) >= 2 else 0.0
    l_sx = float(local.get("zx", 1.0))
    l_sy = float(local.get("zy", 1.0))
    l_ang = float(local.get("angle", 0.0))

    dx = l_tx * p_sx
    dy = l_ty * p_sy
    rx, ry = rotate_point(dx, dy, deg2rad(p_ang))

    w_tx = p_tx + rx
    w_ty = p_ty + ry
    w_sx = p_sx * l_sx
    w_sy = p_sy * l_sy
    w_ang = p_ang + l_ang
    return (w_tx, w_ty, w_sx, w_sy, w_ang)

def apply_opacity(img: Image.Image, opa_val: Any) -> Image.Image:
    if opa_val is None:
        return img
    try:
        opa = float(opa_val)
    except Exception:
        return img
    alpha_mul = opa if opa <= 1.0 else (opa / 255.0)
    alpha_mul = max(0.0, min(1.0, alpha_mul))
    if abs(alpha_mul - 1.0) < 1e-6:
        return img
    r, g, b, a = img.split()
    a = a.point(lambda p: int(p * alpha_mul))
    return Image.merge("RGBA", (r, g, b, a))


def _alpha_compose_inplace(dst: Image.Image, src: Image.Image, px: int, py: int):
    """
    Correct RGBA alpha blending (no "double-alpha" bug that happens with paste(mask=src)).
    This prevents semi-transparent edges from punching holes in the layers below.
    """
    if dst.mode != "RGBA":
        dst = dst.convert("RGBA")
    if src.mode != "RGBA":
        src = src.convert("RGBA")

    dw, dh = dst.size
    sw, sh = src.size

    # clip to dst bounds
    x0 = max(0, px)
    y0 = max(0, py)
    x1 = min(dw, px + sw)
    y1 = min(dh, py + sh)
    if x0 >= x1 or y0 >= y1:
        return

    sx0 = x0 - px
    sy0 = y0 - py
    sx1 = sx0 + (x1 - x0)
    sy1 = sy0 + (y1 - y0)

    dst_crop = dst.crop((x0, y0, x1, y1))
    src_crop = src.crop((sx0, sy0, sx1, sy1))
    out = Image.alpha_composite(dst_crop, src_crop)
    dst.paste(out, (x0, y0))


def render_sprite(
    canvas: Image.Image,
    img: Image.Image,
    state: State,
    screen_center: Tuple[float, float],
    ox: float = 0.0,
    oy: float = 0.0,
    opa: Any = None,
    bm: Optional[int] = None,
):
    tx, ty, sx, sy, ang = state
    cx0, cy0 = screen_center
    world_x = cx0 + tx
    world_y = cy0 + ty

    img = img.convert("RGBA")
    img = apply_opacity(img, opa)

    w0, h0 = img.size
    ax = w0 / 2.0 + float(ox)
    ay = h0 / 2.0 + float(oy)

    # scale
    if abs(sx - 1.0) > 1e-6 or abs(sy - 1.0) > 1e-6:
        new_w = max(1, int(round(w0 * sx)))
        new_h = max(1, int(round(h0 * sy)))
        img = img.resize((new_w, new_h), Image.BICUBIC)
        ax *= sx
        ay *= sy

    # rotate around anchor
    if abs(ang) > 1e-6:
        W, H = img.size
        ang_rad = deg2rad(ang)
        corners = [(0, 0), (W, 0), (W, H), (0, H)]
        rot_pts = []
        for x, y in corners:
            rx, ry = rotate_point(x - ax, y - ay, ang_rad)
            rot_pts.append((rx + ax, ry + ay))
        minx = min(p[0] for p in rot_pts)
        miny = min(p[1] for p in rot_pts)
        ax_out = ax - minx
        ay_out = ay - miny
        img = img.rotate(ang, resample=Image.BICUBIC, expand=True, center=(ax, ay))
    else:
        ax_out = ax
        ay_out = ay

    px = int(round(world_x - ax_out))
    py = int(round(world_y - ay_out))

    if bm == 19:
        # bm=19 in this SD format is typically used for 'shadow multiply'.
        # NOTE: filenames like '*add40' often mean ~40% opacity (opa=102), not additive blending.
        # We apply multiply weighted by sprite alpha.
        temp = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        _alpha_compose_inplace(temp, img, px, py)
        try:
            import numpy as np
            base = np.array(canvas, dtype=np.float32)
            spr = np.array(temp, dtype=np.float32)
            a = spr[..., 3:4] / 255.0
            spr_rgb = spr[..., :3] / 255.0
            base_rgb = base[..., :3]
            mul_rgb = base_rgb * spr_rgb
            out_rgb = base_rgb * (1.0 - a) + mul_rgb * a
            out_a = np.maximum(base[..., 3:4], spr[..., 3:4])
            out = np.concatenate([np.clip(out_rgb, 0, 255), out_a], axis=2).astype(np.uint8)
            canvas.paste(Image.fromarray(out, mode="RGBA"), (0, 0))
        except Exception:
            base_r, base_g, base_b, base_a = canvas.split()
            spr_r, spr_g, spr_b, spr_a = temp.split()
            base_rgb = Image.merge("RGB", (base_r, base_g, base_b))
            spr_rgb = Image.merge("RGB", (spr_r, spr_g, spr_b))
            mul_rgb = ImageChops.multiply(base_rgb, spr_rgb)
            out_rgb = Image.composite(mul_rgb, base_rgb, spr_a)
            out_r, out_g, out_b = out_rgb.split()
            out_a = ImageChops.lighter(base_a, spr_a)
            canvas.paste(Image.merge("RGBA", (out_r, out_g, out_b, out_a)), (0, 0))
        return


    _alpha_compose_inplace(canvas, img, px, py)


# ---------------- JSON helpers ----------------

def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))

def last_content_at_time(frame_list: List[Dict[str, Any]], t: float) -> Optional[Dict[str, Any]]:
    best = None
    for fr in frame_list or []:
        try:
            ft = float(fr.get("time", 0))
        except Exception:
            ft = 0.0
        if ft <= t and "content" in fr:
            best = fr["content"]
    return best

def priority_order_at_time(priority_obj: Any, t: float) -> Optional[List[int]]:
    if isinstance(priority_obj, dict):
        c = priority_obj.get("content")
        if isinstance(c, list):
            return [int(x) for x in c]
        return None
    if isinstance(priority_obj, list):
        best = None
        for fr in priority_obj:
            try:
                ft = float(fr.get("time", 0))
            except Exception:
                ft = 0.0
            c = fr.get("content")
            if ft <= t and isinstance(c, list):
                best = c
        if best is not None:
            return [int(x) for x in best]
    return None

def choose_image_token(content: Dict[str, Any]) -> Optional[str]:
    if not isinstance(content, dict):
        return None
    src = content.get("src")
    if isinstance(src, str) and src and src not in ("clip",) and not src.startswith("particle/"):
        return src
    for k in ("pixel", "image", "tex"):
        v = content.get(k)
        if isinstance(v, str) and v:
            return v
    return src if isinstance(src, str) else None

def norm_nfkc(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s))
    s = s.replace("－", "-").replace("—", "-").replace("‐", "-").replace("‑", "-")
    return s


# ---------------- Resource resolving ----------------

class ResourceResolver:
    def __init__(self):
        self.base_dir: Optional[Path] = None
        self.model_label: str = ""
        self.resx: Dict[str, str] = {}
        self.image_folder: Optional[Path] = None

    def load(self, json_path: Path, model_label: str):
        self.base_dir = json_path.parent
        self.json_stem = json_path.stem
        self.model_label = model_label
        resx_path = json_path.with_suffix(".resx.json")
        self.resx = {}
        if resx_path.exists():
            try:
                data = read_json(resx_path)
                self.resx = data.get("Resources", {}) or {}
            except Exception:
                self.resx = {}

    def set_image_folder(self, folder: Path):
        self.image_folder = folder

    def token_to_path(self, token: str) -> Optional[Path]:
        """
        token can be:
          - 'src/<bucket>/<name>'  e.g. 'src/normal/乃愛|base'
          - '#resource#34' -> resx -> 'sd101/normal-xxx.png'
        Notes:
          - Some exports use '|' in src names but files use '_' (we auto-convert).
          - Prefer resolving via resx mapping when possible.
        """
        if not isinstance(token, str) or not token:
            return None

        # --- resx token ---
        if token.startswith("#resource#"):
            key = token.split("#")[-1]
            rel = self.resx.get(key)
            if not rel or not self.base_dir:
                return None
            p = (self.base_dir / rel).resolve()
            if p.exists():
                return p
            # case variants (common for sd001/SD001)
            p2 = (self.base_dir / rel.replace("sd001/", "SD001/")).resolve()
            if p2.exists():
                return p2
            return None

        # --- src token ---
        if token.startswith("src/"):
            parts = token.split("/", 2)
            if len(parts) < 3:
                return None
            # IMPORTANT: DO NOT rely on NFKC-only normalization for filenames.
            # Many SD exports use fullwidth digits (e.g. '１') in actual PNG filenames.
            # NFKC turns them into ASCII ('1'), which would break path resolution.
            bucket_raw = str(parts[1])
            name_raw = str(parts[2])
            bucket_nfkc = norm_nfkc(bucket_raw)
            name_nfkc = norm_nfkc(name_raw)

            # common: token uses '|' while filename uses '_'
            name_u_raw = name_raw.replace("|", "_")
            name_u_nfkc = name_nfkc.replace("|", "_")

            def _uniq(seq):
                seen = set()
                out = []
                for x in seq:
                    if x in seen:
                        continue
                    seen.add(x)
                    out.append(x)
                return out

            bucket_vars = _uniq([bucket_raw, bucket_nfkc])
            name_vars = _uniq([name_u_raw, name_raw, name_u_nfkc, name_nfkc])

            # 1) try resolve via resx mapping (works even if image_folder not set)
            if self.base_dir and self.resx:
                suffixes = []
                for b in bucket_vars:
                    for n in name_vars:
                        suffixes.extend([
                            f"/{b}-{n}.png",
                            f"/{b}-{n}.PNG",
                        ])
                for rel in self.resx.values():
                    if not isinstance(rel, str):
                        continue
                    # Compare both raw and NFKC-normalized forms to be resilient.
                    rel_raw = rel.replace("\\", "/")
                    rel_n = norm_nfkc(rel_raw)
                    for suf in suffixes:
                        suf_raw = suf
                        suf_n = norm_nfkc(suf_raw)
                        if rel_raw.endswith(suf_raw) or rel_n.endswith(suf_n):
                            p = (self.base_dir / rel).resolve()
                            if p.exists():
                                return p

            # 2) direct lookups in chosen image folder
            if self.image_folder and self.image_folder.exists():
                cands = []
                for b in bucket_vars:
                    for n in name_vars:
                        cands.extend([
                            self.image_folder / f"{b}-{n}.png",
                            self.image_folder / f"{b}-{n}.PNG",
                        ])
                for n in name_vars:
                    cands.extend([
                        self.image_folder / f"{n}.png",
                        self.image_folder / f"{n}.PNG",
                    ])
                # keep backward-compatible fallback
                if self.model_label:
                    for n in name_vars:
                        cands.extend([
                            self.image_folder / f"{self.model_label}-{n}.png",
                            self.image_folder / f"{self.model_label}-{n}.PNG",
                        ])
                for cand in cands:
                    if cand.exists():
                        return cand

                # fuzzy contains-match (last resort)
                try:
                    # try both raw and nfkc stems
                    for n in _uniq([name_u_raw, name_u_nfkc]):
                        for fp in self.image_folder.glob(f"*{n}.png"):
                            return fp
                        for fp in self.image_folder.glob(f"*{n}.PNG"):
                            return fp
                except Exception:
                    pass

            # 3) fallback: sibling folder named like json stem (sd101/SD101), if present
            if self.base_dir:
                json_stem = getattr(self, "json_stem", "") or ""
                for folder in [json_stem, json_stem.lower(), json_stem.upper()]:
                    if not folder:
                        continue
                    base = (self.base_dir / folder)
                    if not base.exists():
                        continue
                    for b in bucket_vars:
                        for n in name_vars:
                            cand = base / f"{b}-{n}.png"
                            if cand.exists():
                                return cand
                            cand2 = base / f"{b}-{n}.PNG"
                            if cand2.exists():
                                return cand2

                # older fallback: base_dir/<model_label>/<model_label>-<name>.png
                for folder in [self.model_label, self.model_label.lower()]:
                    if not folder:
                        continue
                    for n in name_vars:
                        cand3 = self.base_dir / folder / f"{self.model_label}-{n}.png"
                        if cand3.exists():
                            return cand3
                        cand4 = self.base_dir / folder / f"{self.model_label}-{n}.PNG"
                        if cand4.exists():
                            return cand4

            return None

        return None


# ---------------- Motion flattening ----------------

@dataclass
class LayerState:
    layer_id: str            # unique id for this layer instance
    key: str                  # normalized name key for UI (typically png stem without model-)
    file_path: Optional[Path] # resolved path
    state: State
    ox: float
    oy: float
    opa: Optional[float]
    bm: Optional[int]
    weight: int
    visible: bool = True
    token: str = ""           # for debugging


class MotionFlattener:
    def __init__(self, motions: Dict[str, Any], resolver: ResourceResolver):
        self.motions = motions
        self.resolver = resolver

    def compute_child_time(self, child_motion: Dict[str, Any], parent_t: float, time_offset: float) -> float:
        child_t = parent_t + time_offset
        child_last = float(child_motion.get("lastTime", 0.0))
        child_loop = child_motion.get("loopTime", -1.0)
        try:
            child_loop = float(child_loop)
        except Exception:
            child_loop = -1.0

        if child_t > child_last:
            if 0 <= child_loop < child_last:
                loop_len = child_last - child_loop
                if loop_len > 0:
                    child_t = child_loop + ((child_t - child_loop) % loop_len)
                else:
                    child_t = child_last
            else:
                child_t = child_last
        return max(0.0, min(child_t, child_last))

    def flatten(self, motion_name: str, t: float) -> List[LayerState]:
        """
        Return draw list at time t (approx):
        - respects priority order
        - expands nested motion refs
        - assigns weight in draw order (0..)
        - IMPORTANT: allows duplicated images by assigning unique layer_id per instance
        """
        out: List[LayerState] = []
        base_state: State = (0.0, 0.0, 1.0, 1.0, 0.0)
        occ: Dict[str, int] = {}  # key -> count
        self._walk_motion(motion_name, t, base_state, out, stack=set(), occ=occ)
        # ensure stable weights
        for i, ls in enumerate(out):
            ls.weight = i
        return out

    def gather_drawable_tokens(self, motion_name: str) -> List[str]:
        """Collect ALL distinct drawable sprite tokens used by a motion (any frame, any nested motion).

        This is used for the UI option '显示全部图层' so users can see layers that haven't started yet
        at the current time (i.e., first keyframe time > current t).
        """
        out: List[str] = []
        seen: set = set()

        def _walk_motion(name: str, stack: set):
            if name in stack:
                return
            stack.add(name)
            mo = self.motions.get(name) or {}
            for layer in (mo.get("layer") or []):
                _walk_layer(layer, stack)
            stack.remove(name)

        def _walk_layer(layer_obj: Dict[str, Any], stack: set):
            # scan every frame's content for drawable tokens
            for fr in (layer_obj.get("frameList") or []):
                c = fr.get("content")
                if not isinstance(c, dict):
                    continue
                tok = choose_image_token(c)
                if isinstance(tok, str) and tok.startswith("motion/"):
                    parts = tok.split("/")
                    child_name = parts[-1] if len(parts) >= 3 else None
                    if child_name:
                        _walk_motion(child_name, stack)
                elif isinstance(tok, str) and (tok.startswith("src/") or tok.startswith("#resource#")):
                    try:
                        layer_type = int(layer_obj.get("type", 0) or 0)
                    except Exception:
                        layer_type = 0
                    try:
                        stencil_type = int(layer_obj.get("stencilType", 0) or 0)
                    except Exception:
                        stencil_type = 0
                    is_mask_tex = tok.startswith("src/mask/")
                    if layer_type == 12 or stencil_type == 1 or is_mask_tex or tok == "layout":
                        pass
                    else:
                        if tok not in seen:
                            seen.add(tok)
                            out.append(tok)

            for ch in (layer_obj.get("children") or []):
                _walk_layer(ch, stack)

        _walk_motion(motion_name, set())
        return out



    def _walk_motion(self, motion_name: str, t: float, parent_state: State, out: List[LayerState], stack: set, occ: Dict[str, int]):
        if motion_name in stack:
            return
        motion = self.motions.get(motion_name)
        if not motion:
            return
        stack.add(motion_name)

        layers = motion.get("layer", []) or []
        order = priority_order_at_time(motion.get("priority"), t)
        if order:
            ordered = []
            for idx in order:
                if 0 <= idx < len(layers):
                    ordered.append(layers[idx])
            for l in layers:
                if l not in ordered:
                    ordered.append(l)
            layers = ordered

        for layer in layers:
            self._walk_layer(layer, t, parent_state, out, stack, occ)

        stack.remove(motion_name)

    def _walk_layer(self, layer: Dict[str, Any], t: float, parent_state: State, out: List[LayerState], stack: set, occ: Dict[str, int]):
        content = last_content_at_time(layer.get("frameList", []), t)
        local_state = parent_state
        if content and isinstance(content, dict):
            local_state = compose_state(parent_state, content)

        # expand motion ref
        if content and isinstance(content, dict):
            tok = choose_image_token(content)
            if isinstance(tok, str) and tok.startswith("motion/"):
                parts = tok.split("/")
                child_name = parts[-1] if len(parts) >= 3 else None
                if child_name and child_name in self.motions:
                    time_offset = 0.0
                    mi = content.get("motion")
                    if isinstance(mi, dict):
                        try:
                            time_offset = float(mi.get("timeOffset", 0.0))
                        except Exception:
                            time_offset = 0.0
                    child_motion = self.motions[child_name]
                    child_t = self.compute_child_time(child_motion, t, time_offset)
                    self._walk_motion(child_name, child_t, local_state, out, stack, occ)

        # record image
        if content and isinstance(content, dict):
            tok = choose_image_token(content)
            if isinstance(tok, str) and (tok.startswith("src/") or tok.startswith("#resource#")):
                # NOTE: this editor does not implement stencil/mask.
                # Many SD jsons wrap everything in a stencil layer (type=12, stencilType=1) with a black mask texture.
                # If we draw that texture, the preview becomes pure black. So we skip drawing such layers.
                try:
                    layer_type = int(layer.get("type", 0) or 0)
                except Exception:
                    layer_type = 0
                try:
                    stencil_type = int(layer.get("stencilType", 0) or 0)
                except Exception:
                    stencil_type = 0

                is_mask_tex = isinstance(tok, str) and tok.startswith("src/mask/")
                if layer_type == 12 or stencil_type == 1 or is_mask_tex:
                    pass
                else:
                    fp = self.resolver.token_to_path(tok)
                    key = self._token_to_key(tok, fp)
                    # assign unique instance id per key
                    nk = norm_nfkc(key)
                    occ[nk] = occ.get(nk, 0) + 1
                    layer_id = f"{nk}::{occ[nk]}"
                    out.append(LayerState(
                        layer_id=layer_id,
                        key=nk,
                        file_path=fp,
                        state=local_state,
                        ox=float(content.get("ox", 0.0)),
                        oy=float(content.get("oy", 0.0)),
                        opa=content.get("opa"),
                        bm=content.get("bm"),
                        weight=len(out),
                        visible=True,  # visible flag in this format is not reliable (0/1 both appear for visible layers),
                        token=tok,
                    ))

        for ch in layer.get("children", []) or []:
            self._walk_layer(ch, t, local_state, out, stack, occ)

    def _token_to_key(self, tok: str, fp: Optional[Path]) -> str:
        # Prefer actual filename without model prefix:
        if fp and fp.exists():
            stem = norm_nfkc(fp.stem)
            pref = norm_nfkc(self.resolver.model_label) + "-"
            if stem.startswith(pref):
                return stem[len(pref):]
            if "-" in stem:
                return stem.split("-", 1)[1]
            return stem
        # fallback from src token
        if tok.startswith("src/"):
            parts = tok.split("/", 2)
            if len(parts) >= 3:
                return norm_nfkc(parts[2])
        if tok.startswith("#resource#"):
            return tok
        return tok


# ---------------- Data model (edits) ----------------

@dataclass
class LayerEdit:
    """
    One drawable layer instance inside a motion at a given time.
    layer_id is unique per instance (key::occurrence).
    override_* flags decide whether a field should stay fixed when time/frame changes.
    """
    layer_id: str
    key: str
    file_path: Optional[Path]
    state: State
    ox: float
    oy: float
    opa: Optional[float]
    bm: Optional[int]
    weight: int
    visible: bool

    override_state: bool = False
    override_oxoy: bool = False
    override_opa: bool = False
    override_bm: bool = False
    override_weight: bool = False
    override_visible: bool = False

    @staticmethod
    def from_state(ls: LayerState) -> "LayerEdit":
        return LayerEdit(
            layer_id=ls.layer_id,
            key=ls.key,
            file_path=ls.file_path,
            state=ls.state,
            ox=ls.ox,
            oy=ls.oy,
            opa=ls.opa,
            bm=ls.bm,
            weight=ls.weight,
            visible=ls.visible,
        )


@dataclass
class GlobalEdit:
    """
    Global overrides applied to ALL motions for a given image key.
    (If a layer instance has local override flags, those win.)
    """
    key: str
    state: Optional[State] = None
    ox: Optional[float] = None
    oy: Optional[float] = None
    opa: Optional[float] = None
    bm: Optional[int] = None
    visible: Optional[bool] = None
    weight: Optional[int] = None


# ---------------- GUI ----------------


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SD Motion 图形编辑器 (预览/帧/位置/透明度/bm/权重)")
        self.geometry("1400x820")
        self.minsize(1200, 700)

        # loaded data
        self.json_path: Optional[Path] = None
        self.model_label: str = ""
        self.motions: Dict[str, Any] = {}
        self.resolver = ResourceResolver()
        self.flattener: Optional[MotionFlattener] = None

        self.screen_w = 1500
        self.screen_h = 900
        self.origin_x = 0.0
        self.origin_y = 0.0

        # current selection
        self.var_motion = tk.StringVar(value="")
        self.var_time = tk.DoubleVar(value=0.0)
        self.var_show_all_layers = tk.BooleanVar(value=False)
        self.all_tokens_by_motion: Dict[str, List[str]] = {}  # motion -> ordered unique drawable tokens

        # export resolution (empty = original)
        self.var_export_w = tk.StringVar(value="")
        self.var_export_h = tk.StringVar(value="")

        # per-motion edits: edits[motion_name][layer_id] -> LayerEdit
        self.edits: Dict[str, Dict[str, LayerEdit]] = {}  # keyed by layer_id

        # global overrides by image key (NFKC-normalized)
        self.global_edits: Dict[str, GlobalEdit] = {}
        self.global_key_order: List[str] = []  # image-key draw order (smaller drawn earlier)
        self.global_weights: Dict[str, int] = {}  # derived mapping for convenience

        # preview cache
        self._tk_preview: Optional[ImageTk.PhotoImage] = None
        self._img_cache: Dict[str, Image.Image] = {}

        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self):
        outer = ttk.Frame(self)
        outer.pack(fill=tk.BOTH, expand=True)

        pw = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        pw.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(pw)
        right = ttk.Frame(pw, width=420)
        right.pack_propagate(False)
        pw.add(left, weight=3)
        pw.add(right, weight=1)

        # left: preview canvas
        self.canvas = tk.Canvas(left, bg="#111111", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.canvas.bind("<Configure>", lambda e: self._refresh_preview())

        # bottom-left controls
        ctrl = ttk.Frame(left)
        ctrl.pack(fill=tk.X, padx=8, pady=(0, 8))

        ttk.Button(ctrl, text="加载JSON", command=self._load_json).pack(side=tk.LEFT)
        ttk.Button(ctrl, text="设置图片文件夹", command=self._pick_image_folder).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Separator(ctrl, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Label(ctrl, text="预览项(Motion):").pack(side=tk.LEFT)
        self.cmb_motion = ttk.Combobox(ctrl, textvariable=self.var_motion, state="readonly", width=24)
        self.cmb_motion.pack(side=tk.LEFT, padx=(6, 0))
        self.cmb_motion.bind("<<ComboboxSelected>>", lambda e: self._on_motion_change())

        ttk.Label(ctrl, text="帧/时间:").pack(side=tk.LEFT, padx=(12, 0))
        self.ent_time = ttk.Entry(ctrl, width=8)
        self.ent_time.pack(side=tk.LEFT, padx=(6, 0))
        self.ent_time.insert(0, "0")
        ttk.Button(ctrl, text="跳转", command=self._apply_time_entry).pack(side=tk.LEFT, padx=(6, 0))

        self.scale_time = ttk.Scale(ctrl, from_=0, to=100, orient=tk.HORIZONTAL, variable=self.var_time,
                                    command=lambda v: self._on_time_change())
        self.scale_time.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 10))


        # export resolution controls (optional)
        exp = ttk.Frame(ctrl)
        exp.pack(side=tk.RIGHT, padx=(0, 10))
        ttk.Label(exp, text="导出分辨率:").pack(side=tk.LEFT)
        ttk.Entry(exp, textvariable=self.var_export_w, width=6).pack(side=tk.LEFT, padx=(6, 2))
        ttk.Label(exp, text="x").pack(side=tk.LEFT)
        ttk.Entry(exp, textvariable=self.var_export_h, width=6).pack(side=tk.LEFT, padx=(2, 0))
        ttk.Label(exp, text="(留空=原画布；不缩放)").pack(side=tk.LEFT, padx=(6, 0))
        ttk.Separator(exp, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        ttk.Checkbutton(exp, text="显示全部图层", variable=self.var_show_all_layers,
                        command=self._on_show_all_layers_toggle).pack(side=tk.LEFT)

        ttk.Button(ctrl, text="导出当前预览", command=self._export_current).pack(side=tk.RIGHT)
        ttk.Button(ctrl, text="批量导出SD*", command=self._export_batch).pack(side=tk.RIGHT, padx=(0, 6))

        # right panel: layer list and editor
        top = ttk.Frame(right)
        top.pack(fill=tk.X, padx=8, pady=8)
        ttk.Label(top, text="图层列表（来自JSON，可编辑）").pack(anchor="w")

        # treeview
        cols = ("weight", "opa", "bm", "vis")
        self.tree = ttk.Treeview(right, columns=cols, show="tree headings", height=18)
        self.tree.heading("#0", text="图层(文件)")
        self.tree.heading("weight", text="权重")
        self.tree.heading("opa", text="透明度")
        self.tree.heading("bm", text="bm")
        self.tree.heading("vis", text="显示")
        self.tree.column("#0", width=210, anchor="w")
        self.tree.column("weight", width=52, anchor="center")
        self.tree.column("opa", width=72, anchor="center")
        self.tree.column("bm", width=52, anchor="center")
        self.tree.column("vis", width=52, anchor="center")
        self.tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select_layer())

        btn_row = ttk.Frame(right)
        btn_row.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Button(btn_row, text="添加PNG", command=self._add_png_as_layer).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="删除图层", command=self._remove_selected_layer).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btn_row, text="上移", command=lambda: self._shift_layer_global(-1)).pack(side=tk.RIGHT)
        ttk.Button(btn_row, text="下移", command=lambda: self._shift_layer_global(1)).pack(side=tk.RIGHT, padx=(0, 6))

        # editor form
        frm = ttk.LabelFrame(right, text="当前图层参数（每个预览项单独保存）")
        frm.pack(fill=tk.X, padx=8, pady=(0, 8))

        self._vars = {}
        def add_field(r, c, label, key, width=10):
            ttk.Label(frm, text=label).grid(row=r, column=c, sticky="w", padx=6, pady=4)
            var = tk.StringVar(value="")
            ent = ttk.Entry(frm, textvariable=var, width=width)
            ent.grid(row=r, column=c+1, sticky="w", padx=6, pady=4)
            self._vars[key] = var
            return ent

        add_field(0, 0, "tx", "tx")
        add_field(0, 2, "ty", "ty")
        add_field(1, 0, "sx", "sx")
        add_field(1, 2, "sy", "sy")
        add_field(2, 0, "angle", "ang")
        add_field(2, 2, "weight", "weight")
        add_field(3, 0, "ox", "ox")
        add_field(3, 2, "oy", "oy")
        add_field(4, 0, "opa(0-255)", "opa")
        add_field(4, 2, "bm", "bm")

        self.var_vis = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="显示", variable=self.var_vis).grid(row=5, column=0, sticky="w", padx=6, pady=4)

        apply_row = ttk.Frame(frm)
        apply_row.grid(row=6, column=0, columnspan=4, sticky="ew", padx=6, pady=(4, 8))
        ttk.Button(apply_row, text="应用修改", command=self._apply_layer_edit).pack(side=tk.LEFT)
        ttk.Button(apply_row, text="应用到全局", command=self._apply_layer_edit_global).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(apply_row, text="重置为JSON默认", command=self._reset_layer_to_json).pack(side=tk.LEFT, padx=(6, 0))

        for i in range(4):
            frm.grid_columnconfigure(i, weight=1)

        self.status = ttk.Label(right, text="提示：加载JSON -> 选择Motion -> 调帧 -> 选图层编辑。")
        self.status.pack(fill=tk.X, padx=8, pady=(0, 8))

    # ---------- Load ----------
    def _load_json(self):
        p = filedialog.askopenfilename(
            title="选择 sdxxx.json",
            filetypes=[("JSON", "*.json"), ("All", "*.*")]
        )
        if not p:
            return
        try:
            data = read_json(Path(p))
        except Exception as e:
            messagebox.showerror("失败", f"JSON读取失败：\n{e}")
            return

        try:
            self.json_path = Path(p)
            self.model_label = data.get("label", "")
            screen = data.get("screenSize") or {}
            self.screen_w = int(screen.get("width", 1500))
            self.screen_h = int(screen.get("height", 900))
            self.origin_x = float(screen.get("originX", 0))
            self.origin_y = float(screen.get("originY", 0))

            self.motions = data["object"][self.model_label]["motion"]
            self.resolver.load(self.json_path, self.model_label)
            # auto-detect image folder (common: a folder with the same name as json stem, e.g. sd101/)
            if not self.resolver.image_folder:
                base = self.json_path.parent

                # Auto-pick an image folder.
                # Common layouts:
                #   1) <json_dir>/<json_stem>/*.png   (e.g. sd101/normal-xxx.png)
                #   2) <json_dir>/*.png              (e.g. normal-xxx.png)
                candidates = [
                    base / self.json_path.stem,
                    base / self.json_path.stem.lower(),
                    base / self.json_path.stem.upper(),
                    base,
                ]

                def _has_expected_pngs(d: Path) -> bool:
                    try:
                        # Prefer folders that clearly contain SD sprite files
                        if any(d.glob("normal-*.png")) or any(d.glob("mask-*.png")):
                            return True
                        # fallback: any png at all
                        return any(d.glob("*.png")) or any(d.glob("*.PNG"))
                    except Exception:
                        return False

                for d in candidates:
                    if d.exists() and d.is_dir() and _has_expected_pngs(d):
                        self.resolver.set_image_folder(d)
                        self._img_cache.clear()
                        self.status.config(text=f"已自动设置图片文件夹：{d.name}")
                        break


            self.flattener = MotionFlattener(self.motions, self.resolver)
            # Pre-scan tokens for '显示全部图层'
            self.all_tokens_by_motion = {m: self.flattener.gather_drawable_tokens(m) for m in self.motions.keys()}

            motions = sorted(self.motions.keys())
            self.cmb_motion["values"] = motions

            # default: first SD* motion if exists
            pref = [m for m in motions if m.startswith(self.model_label)]
            default = pref[0] if pref else (motions[0] if motions else "")
            self.var_motion.set(default)
            self._on_motion_change()

            messagebox.showinfo("成功", f"模型：{self.model_label}\n画布：{self.screen_w}x{self.screen_h}\nMotion：{len(motions)}")
        except Exception as e:
            messagebox.showerror("失败", f"解析结构失败：\n{e}")

    def _pick_image_folder(self):
        if not self.json_path:
            messagebox.showwarning("提示", "请先加载JSON。")
            return
        p = filedialog.askdirectory(title="选择包含PNG的文件夹（如 SD001 文件夹）")
        if not p:
            return
        self.resolver.set_image_folder(Path(p))
        self._img_cache.clear()
        self.status.config(text=f"已设置图片文件夹：{Path(p).name}")
        self._rebuild_layers_from_json()
        self._refresh_preview()

    # ---------- Motion/time ----------
    def _on_motion_change(self):
        if not self.var_motion.get():
            return
        # reset time slider range to lastTime
        motion = self.motions.get(self.var_motion.get())
        if motion:
            last_t = float(motion.get("lastTime", 0.0))
            self.scale_time.configure(to=max(1, last_t))

            # Default to last frame (lastTime) as requested.
            default_t = last_t

            self.var_time.set(default_t)
            self.ent_time.delete(0, tk.END)
            self.ent_time.insert(0, str(int(round(default_t))))
        self._rebuild_layers_from_json()
        self._refresh_preview()

    def _apply_time_entry(self):
        try:
            t = float(self.ent_time.get().strip())
        except Exception:
            return
        self.var_time.set(t)
        self._on_time_change()

    def _on_time_change(self):
        # update entry display and rebuild list (because JSON values change with time)
        t = float(self.var_time.get())
        self.ent_time.delete(0, tk.END)
        self.ent_time.insert(0, str(int(round(t))))
        self._rebuild_layers_from_json()
        self._refresh_preview()

    
    def _on_show_all_layers_toggle(self):
        # Only affects the right-side layer list, not the preview draw result.
        self._rebuild_layers_from_json()

    def _screen_center(self, w: Optional[int] = None, h: Optional[int] = None) -> Tuple[float, float]:
        """Return screen center in pixels. If w/h provided, use them as canvas size."""
        if w is None:
            w = self.screen_w
        if h is None:
            h = self.screen_h
        return (w / 2.0 + self.origin_x, h / 2.0 + self.origin_y)

    # ---------- Layer list / edits ----------
    def _get_motion_edits(self) -> Dict[str, LayerEdit]:
        m = self.var_motion.get()
        if m not in self.edits:
            self.edits[m] = {}
        return self.edits[m]


    def _ensure_global_key_order(self, keys_in_order: List[str]):
        """
        Ensure self.global_key_order contains given keys (append missing in given order),
        then refresh self.global_weights mapping.
        """
        for k in keys_in_order:
            k = norm_nfkc(k)
            if k and (k not in self.global_key_order):
                self.global_key_order.append(k)
        self.global_weights = {k: i for i, k in enumerate(self.global_key_order)}

    def _move_key_in_global_order(self, key: str, new_index: int):
        """Move a key to a specific position in global draw order."""
        key = norm_nfkc(key)
        if not key:
            return
        if key not in self.global_key_order:
            self.global_key_order.append(key)
        old = self.global_key_order.index(key)
        new_index = max(0, min(len(self.global_key_order) - 1, int(new_index)))
        if old == new_index:
            return
        self.global_key_order.pop(old)
        self.global_key_order.insert(new_index, key)
        self.global_weights = {k: i for i, k in enumerate(self.global_key_order)}

    def _rebuild_layers_from_json(self):
        """
        Rebuild right-side layer list and refresh per-instance edits from JSON defaults.

        IMPORTANT:
        - If a field is NOT overridden (override_* = False), it will follow JSON when frame/time changes.
        - Global overrides (self.global_edits / self.global_weights) apply to all motions, unless local overrides exist.
        """
        if not self.flattener or not self.var_motion.get():
            return
        motion = self.var_motion.get()
        t = float(self.var_time.get())

        defaults = self.flattener.flatten(motion, t)  # List[LayerState] with unique layer_id
        edits = self._get_motion_edits()              # Dict[layer_id, LayerEdit]

        # helper: occurrence index for stable ordering of duplicated keys
        def occ_index(layer_id: str) -> int:
            try:
                return int(layer_id.split("::")[-1])
            except Exception:
                return 1

        # build / update edits for defaults
        merged: List[LayerEdit] = []

        # Determine a baseline weight per key from JSON default order (first occurrence)
        default_key_weight: Dict[str, int] = {}
        for ls in defaults:
            if ls.key not in default_key_weight:
                default_key_weight[ls.key] = int(ls.weight)

        for ls in defaults:
            lid = ls.layer_id
            if lid in edits:
                e = edits[lid]
                # Always keep these in sync
                e.key = ls.key
                if (not e.file_path) and ls.file_path:
                    e.file_path = ls.file_path

                # Follow JSON if not overridden
                if not e.override_state:
                    e.state = ls.state
                if not e.override_oxoy:
                    e.ox, e.oy = ls.ox, ls.oy
                if not e.override_opa:
                    e.opa = ls.opa
                if not e.override_bm:
                    e.bm = ls.bm
                if not e.override_visible:
                    e.visible = ls.visible
                # weight is handled later (global/local/default)
            else:
                e = LayerEdit.from_state(ls)
                edits[lid] = e

            # Apply global overrides by key (unless local override exists)
            g = self.global_edits.get(ls.key)
            if g:
                if (g.state is not None) and (not e.override_state):
                    e.state = g.state
                if (g.ox is not None or g.oy is not None) and (not e.override_oxoy):
                    e.ox = g.ox if g.ox is not None else e.ox
                    e.oy = g.oy if g.oy is not None else e.oy
                if (g.opa is not None) and (not e.override_opa):
                    e.opa = g.opa
                if (g.bm is not None) and (not e.override_bm):
                    e.bm = g.bm
                if (g.visible is not None) and (not e.override_visible):
                    e.visible = g.visible
                if (g.weight is not None) and (not e.override_weight):
                    e.weight = int(g.weight)

            merged.append(e)

        # Keep user-added layers (layer_id starts with USER::)
        for lid, e in list(edits.items()):
            if lid.startswith("USER::"):
                # apply global overrides too
                g = self.global_edits.get(e.key)
                if g:
                    if (g.state is not None) and (not e.override_state):
                        e.state = g.state
                    if (g.ox is not None or g.oy is not None) and (not e.override_oxoy):
                        e.ox = g.ox if g.ox is not None else e.ox
                        e.oy = g.oy if g.oy is not None else e.oy
                    if (g.opa is not None) and (not e.override_opa):
                        e.opa = g.opa
                    if (g.bm is not None) and (not e.override_bm):
                        e.bm = g.bm
                    if (g.visible is not None) and (not e.override_visible):
                        e.visible = g.visible
                    if (g.weight is not None) and (not e.override_weight):
                        e.weight = int(g.weight)
                merged.append(e)

        # Compute global/default weights for non-overridden layers
        # Ensure global order contains keys we see (in JSON default order)
        keys_in_default_order = [k for k, _ in sorted(default_key_weight.items(), key=lambda kv: kv[1])]
        # also include user-added keys
        for e in merged:
            if e.key not in keys_in_default_order:
                keys_in_default_order.append(e.key)
        self._ensure_global_key_order(keys_in_default_order)

        # Apply weights from GLOBAL order by key (always). This makes Up/Down consistent.
        for e in merged:
            e.weight = int(self.global_weights.get(e.key, default_key_weight.get(e.key, e.weight)))
        merged.sort(key=lambda x: (int(x.weight), occ_index(x.layer_id), x.layer_id))

        # rewrite treeview (iid must be unique -> use layer_id)
        self.tree.delete(*self.tree.get_children())
        for e in merged:
            name = e.file_path.name if (e.file_path and e.file_path.exists()) else e.key
            self.tree.insert("", "end", iid=e.layer_id, text=name,
                             values=(e.weight, self._fmt_opa(e.opa), self._fmt_bm(e.bm), "Y" if e.visible else "N"))

        
        extra_n = 0
        if self.var_show_all_layers.get():
            try:
                tokens = self.all_tokens_by_motion.get(motion, []) or []
                existing_keys = {e.key for e in merged}
                for tok in tokens:
                    fp = self.resolver.token_to_path(tok) if self.resolver else None
                    key = norm_nfkc(self.flattener._token_to_key(tok, fp) if (self.flattener) else (fp.name if fp else str(tok)))
                    if key in existing_keys:
                        continue
                    iid = f"all:{key}"
                    name = fp.name if (fp and fp.exists()) else key
                    # show as "not active at current time" placeholders
                    self.tree.insert("", "end", iid=iid, text=name, values=("-", "-", "-", "-"))
                    extra_n += 1
            except Exception:
                extra_n = 0

        sz = self._get_export_size()
        exp_txt = f"  导出={sz[0]}x{sz[1]}" if sz else ""
        self.status.config(text=f"Motion={motion}  t={int(round(t))}  图层数={len(merged)}{f'+{extra_n}' if extra_n else ''}{exp_txt}")

    def _fmt_opa(self, opa: Optional[float]) -> str:
        if opa is None:
            return "-"
        try:
            v = float(opa)
        except Exception:
            return str(opa)
        if v <= 1.0:
            return f"{int(v*255)}"
        return f"{int(v)}"

    def _fmt_bm(self, bm: Optional[int]) -> str:
        return "-" if bm is None else str(bm)

    def _selected_layer_id(self) -> Optional[str]:
        sel = self.tree.selection()
        if not sel:
            return None
        return sel[0]

    def _on_select_layer(self):
        layer_id = self._selected_layer_id()
        if not layer_id:
            return
        e = self._get_motion_edits().get(layer_id)
        if not e:
            return
        tx, ty, sx, sy, ang = e.state
        self._vars["tx"].set(f"{tx:.2f}")
        self._vars["ty"].set(f"{ty:.2f}")
        self._vars["sx"].set(f"{sx:.4f}")
        self._vars["sy"].set(f"{sy:.4f}")
        self._vars["ang"].set(f"{ang:.2f}")
        self._vars["ox"].set(f"{e.ox:.2f}")
        self._vars["oy"].set(f"{e.oy:.2f}")
        self._vars["opa"].set("" if e.opa is None else str(e.opa))
        self._vars["bm"].set("" if e.bm is None else str(e.bm))
        self._vars["weight"].set(str(e.weight))
        self.var_vis.set(bool(e.visible))

    def _apply_layer_edit(self):
        layer_id = self._selected_layer_id()
        if not layer_id:
            return
        edits = self._get_motion_edits()
        e = edits.get(layer_id)
        if not e:
            return

        def fget(name, default):
            s = self._vars[name].get().strip()
            if s == "":
                return default
            try:
                return float(s)
            except Exception:
                return default

        tx = fget("tx", e.state[0])
        ty = fget("ty", e.state[1])
        sx = fget("sx", e.state[2])
        sy = fget("sy", e.state[3])
        ang = fget("ang", e.state[4])
        ox = fget("ox", e.ox)
        oy = fget("oy", e.oy)

        # opa/bm can be None
        opa_s = self._vars["opa"].get().strip()
        opa = e.opa
        if opa_s == "":
            opa = None
        else:
            try:
                opa = float(opa_s)
            except Exception:
                pass

        bm_s = self._vars["bm"].get().strip()
        bm = e.bm
        if bm_s == "":
            bm = None
        else:
            try:
                bm = int(float(bm_s))
            except Exception:
                pass

        w_raw = self._vars["weight"].get().strip()
        w = None
        if w_raw != "":
            try:
                w = int(float(w_raw))
            except Exception:
                w = None

        e.state = (tx, ty, sx, sy, ang)
        e.ox = ox
        e.oy = oy
        e.opa = opa
        e.bm = bm
        if w is not None:
            # interpret weight as GLOBAL order index
            self._move_key_in_global_order(e.key, w)
        e.weight = int(self.global_weights.get(e.key, e.weight))
        e.visible = bool(self.var_vis.get())
        # mark overrides so animation/frame changes won't overwrite these fields
        e.override_state = True
        e.override_oxoy = True
        e.override_opa = True
        e.override_bm = True
        e.override_weight = False
        e.override_visible = True


        self._rebuild_layers_from_json()
        # reselect
        if self.tree.exists(layer_id):
            self.tree.selection_set(layer_id)
        self._refresh_preview()

    
    def _apply_layer_edit_global(self):
        """
        Apply current editor values to GLOBAL overrides (all motions).
        This will also clear local override flags for matching layers so global can take effect.
        """
        layer_id = self._selected_layer_id()
        if not layer_id:
            return
        cur_edits = self._get_motion_edits()
        e0 = cur_edits.get(layer_id)
        if not e0:
            return
        key = e0.key

        # parse fields from UI (same as local apply)
        def fget(name, default):
            s = self._vars[name].get().strip()
            if s == "":
                return default
            try:
                return float(s)
            except Exception:
                return default

        tx = fget("tx", e0.state[0])
        ty = fget("ty", e0.state[1])
        sx = fget("sx", e0.state[2])
        sy = fget("sy", e0.state[3])
        ang = fget("ang", e0.state[4])
        ox = fget("ox", e0.ox)
        oy = fget("oy", e0.oy)

        opa_s = self._vars["opa"].get().strip()
        opa = None if opa_s == "" else e0.opa
        if opa_s != "":
            try:
                opa = float(opa_s)
            except Exception:
                opa = e0.opa

        bm_s = self._vars["bm"].get().strip()
        bm = None if bm_s == "" else e0.bm
        if bm_s != "":
            try:
                bm = int(float(bm_s))
            except Exception:
                bm = e0.bm

        w_raw = self._vars["weight"].get().strip()
        w = None
        if w_raw != "":
            try:
                w = int(float(w_raw))
            except Exception:
                w = None

        vis = bool(self.var_vis.get())

        if w is not None:
            self._move_key_in_global_order(key, w)

        self.global_edits[key] = GlobalEdit(
            key=key,
            state=(tx, ty, sx, sy, ang),
            ox=ox,
            oy=oy,
            opa=opa,
            bm=bm,
            visible=vis,
            weight=None,
        )
        if w is not None:
            self.global_weights[key] = int(w)

        # Clear local override flags for matching keys so global dominates
        for motion_name, d in self.edits.items():
            for lid, ee in d.items():
                if ee.key != key:
                    continue
                ee.override_state = False
                ee.override_oxoy = False
                ee.override_opa = False
                ee.override_bm = False
                ee.override_visible = False
                # weight: if user wants global weight, clear local weight override
                ee.override_weight = False

        self._rebuild_layers_from_json()
        if self.tree.exists(layer_id):
            self.tree.selection_set(layer_id)
        self._refresh_preview()

    def _reset_layer_to_json(self):
        # reset selected layer to JSON default at current time
        if not self.flattener:
            return
        layer_id = self._selected_layer_id()
        if not layer_id:
            return
        motion = self.var_motion.get()
        t = float(self.var_time.get())
        defaults = self.flattener.flatten(motion, t)
        target = None
        for ls in defaults:
            if ls.layer_id == layer_id:
                target = ls
                break
        if not target:
            return
        edits = self._get_motion_edits()
        edits[layer_id] = LayerEdit.from_state(target)
        self._rebuild_layers_from_json()
        if self.tree.exists(layer_id):
            self.tree.selection_set(layer_id)
        self._refresh_preview()

    def _add_png_as_layer(self):
        p = filedialog.askopenfilename(
            title="选择 PNG",
            filetypes=[("PNG", "*.png"), ("All", "*.*")]
        )
        if not p:
            return
        fp = Path(p)
        # key from filename (strip model prefix)
        stem = norm_nfkc(fp.stem)
        pref = norm_nfkc(self.model_label) + "-"
        if stem.startswith(pref):
            key = stem[len(pref):]
        elif "-" in stem:
            key = stem.split("-", 1)[1]
        else:
            key = stem

        edits = self._get_motion_edits()
        # place at center by default
        e = LayerEdit(
            layer_id=f"USER::{key}::{len(edits)+1}",
            key=key,
            file_path=fp,
            state=(0.0, 0.0, 1.0, 1.0, 0.0),
            ox=0.0,
            oy=0.0,
            opa=None,
            bm=None,
            weight=max([x.weight for x in edits.values()], default=-1) + 1,
            visible=True,
        )
        edits[e.layer_id] = e
        self._rebuild_layers_from_json()
        if self.tree.exists(layer_id):
            self.tree.selection_set(layer_id)
        self._refresh_preview()

    def _remove_selected_layer(self):
        layer_id = self._selected_layer_id()
        if not layer_id:
            return
        edits = self._get_motion_edits()
        if layer_id in edits:
            del edits[layer_id]
        self._rebuild_layers_from_json()
        self._refresh_preview()

    def _shift_layer_global(self, delta: int):
        """
        Move a layer key up/down in a GLOBAL order (applies to all motions).
        Operates on the image key (duplicates move together).
        """
        layer_id = self._selected_layer_id()
        if not layer_id:
            return
        cur = self._get_motion_edits().get(layer_id)
        if not cur:
            return
        sel_key = cur.key

        # If global order is empty, seed it from current UI unique key order
        if not self.global_key_order:
            cur_edits = self._get_motion_edits()
            unique_keys: List[str] = []
            for lid in self.tree.get_children():
                e = cur_edits.get(lid)
                if e and (e.key not in unique_keys):
                    unique_keys.append(e.key)
            self._ensure_global_key_order(unique_keys)
        else:
            # ensure current view keys are included too
            cur_edits = self._get_motion_edits()
            view_keys: List[str] = []
            for lid in self.tree.get_children():
                e = cur_edits.get(lid)
                if e and (e.key not in view_keys):
                    view_keys.append(e.key)
            self._ensure_global_key_order(view_keys)

        if sel_key not in self.global_key_order:
            self.global_key_order.append(sel_key)

        i = self.global_key_order.index(sel_key)
        j = i + int(delta)
        if j < 0 or j >= len(self.global_key_order):
            return

        self.global_key_order[i], self.global_key_order[j] = self.global_key_order[j], self.global_key_order[i]
        self.global_weights = {k: idx for idx, k in enumerate(self.global_key_order)}

        self._rebuild_layers_from_json()
        # reselect first instance of the moved key
        for lid in self.tree.get_children():
            e = self._get_motion_edits().get(lid)
            if e and e.key == sel_key:
                self.tree.selection_set(lid)
                break
        self._refresh_preview()

    # backward compatible name (unused by UI now)
    def _shift_layer(self, delta: int):
        self._shift_layer_global(delta)

    # ---------- Preview ----------

    def _load_image_cached(self, fp: Path) -> Optional[Image.Image]:
        key = str(fp.resolve())
        if key in self._img_cache:
            return self._img_cache[key]
        try:
            img = Image.open(fp).convert("RGBA")
        except Exception:
            return None
        self._img_cache[key] = img
        return img

    def _render_composite(self, canvas_size: Optional[Tuple[int, int]] = None) -> Image.Image:
        """
        Render composite without scaling sprites.
        If canvas_size is provided, render onto that canvas (cropping/padding) while keeping coordinates.
        """
        if canvas_size:
            cw, ch = canvas_size
        else:
            cw, ch = self.screen_w, self.screen_h
        img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        center = self._screen_center(cw, ch)

        edits = self._get_motion_edits()
        layers = list(edits.values())
        layers.sort(key=lambda x: (x.weight, x.layer_id))
        for e in layers:
            if not e.visible:
                continue
            if not e.file_path or not e.file_path.exists():
                continue
            spr = self._load_image_cached(e.file_path)
            if spr is None:
                continue
            render_sprite(img, spr, e.state, center, ox=e.ox, oy=e.oy, opa=e.opa, bm=e.bm)
        return img


    def _refresh_preview(self):
        if self.canvas.winfo_width() < 10 or self.canvas.winfo_height() < 10:
            return
        if not self.json_path:
            # blank preview
            self.canvas.delete("all")
            self.canvas.create_text(20, 20, anchor="nw", fill="white", font=("Segoe UI", 14),
                                    text="加载JSON开始使用")
            return

        comp = self._render_composite()
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        iw, ih = comp.size
        scale = min(cw / iw, ch / ih)
        new_w = max(1, int(iw * scale))
        new_h = max(1, int(ih * scale))
        disp = comp.resize((new_w, new_h), Image.BICUBIC)

        # show on black bg (easier to see)
        bg = Image.new("RGBA", (new_w, new_h), (0, 0, 0, 255))
        disp2 = Image.alpha_composite(bg, disp)

        self._tk_preview = ImageTk.PhotoImage(disp2)
        self.canvas.delete("all")
        x = (cw - new_w) // 2
        y = (ch - new_h) // 2
        self.canvas.create_image(x, y, image=self._tk_preview, anchor="nw")
        self.canvas.create_text(10, 10, anchor="nw", fill="white", font=("Segoe UI", 11),
                                text=f"{self.model_label}  {self.var_motion.get()}  t={int(round(self.var_time.get()))}")


    # ---------- Export helpers ----------
    def _export_size_is_invalid(self) -> bool:
        sw = (self.var_export_w.get() or "").strip()
        sh = (self.var_export_h.get() or "").strip()
        return (sw != "" and sh == "") or (sw == "" and sh != "")

    def _get_export_size(self) -> Optional[Tuple[int, int]]:
        """Return (w,h) from UI; None means keep original screen size."""
        sw = (self.var_export_w.get() or "").strip()
        sh = (self.var_export_h.get() or "").strip()
        if not sw or not sh:
            return None
        try:
            w = int(float(sw))
            h = int(float(sh))
        except Exception:
            return None
        if w <= 0 or h <= 0:
            return None
        return (w, h)


    # ---------- Export ----------
    def _export_current(self):
        if not self.json_path:
            return
        out = filedialog.asksaveasfilename(
            title="导出PNG",
            defaultextension=".png",
            initialfile=f"{self.model_label}_{self.var_motion.get()}_t{int(round(self.var_time.get()))}.png",
            filetypes=[("PNG", "*.png")]
        )
        if not out:
            return
        try:
            if self._export_size_is_invalid():
                messagebox.showwarning("导出分辨率", "请同时填写宽和高（两个框都要有数字），或两个都留空。")
                return
            sz = self._get_export_size()
            img = self._render_composite(canvas_size=sz)
            img.save(out, "PNG", compress_level=1)
            messagebox.showinfo("完成", f"已导出：\n{out}\n分辨率：{img.size[0]}x{img.size[1]}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
            return

    def _export_batch(self):
        if not self.json_path:
            return
        out_dir = filedialog.askdirectory(title="选择批量导出目录")
        if not out_dir:
            return
        out_dir = Path(out_dir)
        if self._export_size_is_invalid():
            messagebox.showwarning("导出分辨率", "请同时填写宽和高（两个框都要有数字），或两个都留空。")
            return
        motions = [m for m in sorted(self.motions.keys()) if m.startswith(self.model_label)]
        ok = 0
        for m in motions:
            self.var_motion.set(m)
            self._on_motion_change()
            # set to last frame
            last_t = float(self.motions[m].get("lastTime", 0.0))
            self.var_time.set(last_t)
            self._on_time_change()
            try:
                sz = self._get_export_size()
                img = self._render_composite(canvas_size=sz)
                img.save(out_dir / f"{self.model_label}_{m}_last.png", "PNG", compress_level=1)
                ok += 1
            except Exception:
                continue
        messagebox.showinfo("完成", f"批量导出完成：{ok}/{len(motions)}\n目录：{out_dir}")


def main():
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()