import json
import os
import re
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

def find_sd_bundle():
    """
    自动寻找当前目录下成对的：
      sd数字.json + sd数字.resx.json
    返回：
      (prefix, json_file, resx_file) 例如 ('sd003', 'sd003.json', 'sd003.resx.json')
    默认选择编号最大的那一组（如果有多组）
    """
    bundles = []

    for name in os.listdir('.'):
        if not os.path.isfile(name):
            continue

        m = re.fullmatch(r'(sd\d+)\.json', name, flags=re.IGNORECASE)
        if not m:
            continue

        prefix = m.group(1).lower()
        # 排除 xxx.resx.json（因为上面的正则不会匹配到 .resx.json，这里只是双保险）
        if name.lower().endswith('.resx.json'):
            continue

        resx_name = f"{prefix}.resx.json"
        if not os.path.exists(resx_name):
            continue

        num_m = re.search(r'(\d+)$', prefix)
        num = int(num_m.group(1)) if num_m else -1
        bundles.append((num, prefix, name, resx_name))

    if not bundles:
        raise FileNotFoundError("当前目录未找到成对的 sd数字.json 和 sd数字.resx.json（例如 sd003.json / sd003.resx.json）")

    bundles.sort(key=lambda x: x[0])  # 按数字升序
    _, prefix, json_file, resx_file = bundles[-1]  # 取最大编号
    return prefix, json_file, resx_file


def find_motion_object_key(sd_data, prefix: str):
    """
    优先按 prefix.upper() 匹配对象键（如 sd003 -> SD003），
    找不到则回退为第一个包含 motion 的对象。
    """
    obj_map = sd_data.get("object", {})
    preferred = prefix.upper()
    if preferred in obj_map and isinstance(obj_map[preferred], dict):
        return preferred

    for k, v in obj_map.items():
        if isinstance(v, dict) and isinstance(v.get("motion"), dict):
            return k

    raise ValueError(f"未找到包含 motion 的对象键。当前 object keys: {list(obj_map.keys())}")

def extract_animations():
    sd_prefix, sd_json_file, sd_resx_file = find_sd_bundle()

    with open(sd_json_file, 'r', encoding='utf-8') as f:
        sd_data = json.load(f)
    with open(sd_resx_file, 'r', encoding='utf-8') as f:
        resx_data = json.load(f)

    obj_key = find_motion_object_key(sd_data, sd_prefix)

    animations_output = {}

    # 收集所有带 motion 的对象（SD009、AA、AB、RA、EA...）
    all_motion_objects = {}
    for _obj_name, _obj_val in sd_data.get("object", {}).items():
        if isinstance(_obj_val, dict) and isinstance(_obj_val.get("motion"), dict):
            all_motion_objects[_obj_name] = _obj_val["motion"]

    # 展平（用于最终导出 extracted_animations.json）
    motions = {}
    for _obj_name, _motion_map in all_motion_objects.items():
        for _motion_name, _motion_data in _motion_map.items():
            if _motion_name in motions:
                print(f"[warn] motion 名称重复（后者覆盖前者）: {_motion_name} (来自 object={_obj_name})")
            motions[_motion_name] = _motion_data

    print(f"[auto-detect] 使用: {sd_json_file} + {sd_resx_file} | object key = {obj_key}")

    # 记录哪些 motion 是被别的动作通过 "motion/xxx" 引用的（常见是 xxxani）
    referenced_motion_names = set()

    def _scan_motion_refs_in_layer(layer_data):
        for frame in layer_data.get('frameList', []):
            content = frame.get('content') or {}
            src = content.get('src', '')
            if isinstance(src, str) and src.startswith("motion/"):
                ref_name = src.rsplit('/', 1)[-1]
                referenced_motion_names.add(ref_name)

        for child in layer_data.get('children', []):
            _scan_motion_refs_in_layer(child)

    for _anim_data in motions.values():
        for _layer in _anim_data.get("layer", []):
            _scan_motion_refs_in_layer(_layer)

    def _build_motion_ref_transform(parent_transform, content):
        """
        把一个 frame 中的 motion/ 引用当成“子动画实例”，计算其挂载时的父变换。
        注意：这里和普通图片一样，坐标仍然要减 ox/oy。
        """
        coord = content.get('coord', [0, 0, 0])

        local_x = (coord[0] if len(coord) > 0 else 0) - content.get('ox', 0.0)
        local_y = (coord[1] if len(coord) > 1 else 0) - content.get('oy', 0.0)

        local_scale_x = content.get('zx', 1.0)
        local_scale_y = content.get('zy', 1.0)
        local_rot = content.get('angle', 0.0)
        local_opa = content.get('opa', 255)

        return {
            'x': parent_transform['x'] + local_x,
            'y': parent_transform['y'] + local_y,
            'scale_x': parent_transform['scale_x'] * local_scale_x,
            'scale_y': parent_transform['scale_y'] * local_scale_y,
            'rotation': parent_transform['rotation'] + local_rot,
            'opacity': int(parent_transform['opacity'] * (local_opa / 255.0))
        }

    def parse_layer(layer_data, current_anim, parent_transform, time_shift=0.0, motion_stack=None):
        if motion_stack is None:
            motion_stack = []

        layer_name = layer_data.get('label', 'unnamed_layer')
        frames = layer_data.get('frameList', [])

        # 继承父级变换
        current_layer_transform = {
            'x': parent_transform['x'],
            'y': parent_transform['y'],
            'scale_x': parent_transform['scale_x'],
            'scale_y': parent_transform['scale_y'],
            'rotation': parent_transform['rotation'],
            'opacity': parent_transform['opacity']
        }

        # 【修正点1】：父节点（Layout骨架）绝对不叠加 ox/oy，只使用纯坐标，防止产生整体小偏移
        if len(frames) > 0:
            c0 = (frames[0].get('content') or {})
            c0_src = c0.get('src', '')
            if c0 and c0_src == "layout":
                coord = c0.get('coord', [0, 0, 0])
                current_layer_transform['x'] += (coord[0] if len(coord) > 0 else 0)
                current_layer_transform['y'] += (coord[1] if len(coord) > 1 else 0)
                current_layer_transform['scale_x'] *= c0.get('zx', 1.0)
                current_layer_transform['scale_y'] *= c0.get('zy', 1.0)
                current_layer_transform['rotation'] += c0.get('angle', 0.0)
                current_layer_transform['opacity'] = int(current_layer_transform['opacity'] * (c0.get('opa', 255) / 255.0))

        # 传递给子图层（注意 time_shift / motion_stack 也要传下去）
        for child in layer_data.get('children', []):
            parse_layer(child, current_anim, current_layer_transform, time_shift=time_shift, motion_stack=motion_stack)

        if not frames:
            return

        layer_frames = []
        has_inlined_motion_ref = False  # 这个图层是否仅用于挂载 motion 子动作

        for frame in frames:
            time = time_shift + frame.get('time', 0)
            content = frame.get('content')

            if not content:
                layer_frames.append({"time": time, "visible": False})
                continue

            src = content.get('src', '')

            # layout 直接跳过（保留时间点）
            if not src or src == "layout":
                layer_frames.append({"time": time, "visible": False})
                continue

            # ★ 关键修复：展开 motion/ 子动作引用，而不是直接跳过
            if isinstance(src, str) and src.startswith("motion/"):
                has_inlined_motion_ref = True

                # src 形如 motion/AA/AA05
                ref_parts = src.split('/', 2)
                ref_obj = ref_parts[1] if len(ref_parts) > 1 else None
                ref_name = ref_parts[2] if len(ref_parts) > 2 else None
                ref_key = f"{ref_obj}/{ref_name}" if ref_obj and ref_name else src

                motion_cfg = content.get('motion', {}) or {}
                ref_time_offset = motion_cfg.get('timeOffset', 0) or 0

                # 优先按完整路径查（AA + AA05），找不到再回退到展平 motions（兼容旧数据）
                ref_motion = None
                if ref_obj and ref_name:
                    ref_motion = all_motion_objects.get(ref_obj, {}).get(ref_name)
                if ref_motion is None and ref_name:
                    ref_motion = motions.get(ref_name)

                if ref_motion is not None:
                    if ref_key in motion_stack:
                        print(f"[warn] 检测到循环 motion 引用，已跳过: {' -> '.join(motion_stack + [ref_key])}")
                    else:
                        ref_parent_transform = _build_motion_ref_transform(parent_transform, content)

                        for ref_layer in ref_motion.get("layer", []):
                            parse_layer(
                                ref_layer,
                                current_anim,
                                ref_parent_transform,
                                time_shift=time + ref_time_offset,
                                motion_stack=motion_stack + [ref_key]
                            )
                else:
                    print(f"[warn] 找不到被引用的 motion: {src}")

                    # 当前这个 type=3 图层本身通常只是“挂载点”，保留一个不可见帧即可
                    layer_frames.append({"time": time, "visible": False})
                    continue

            try:
                parts = src.split('/', 2)
                cat = parts[1]
                name = parts[2]

                img_config = sd_data.get('source', {}).get(cat, {}).get('icon', {}).get(name, {})
                res_id = img_config.get('pixel', '').replace('#resource#', '')

                img_path = resx_data.get('Resources', {}).get(res_id, '')
                img_name = os.path.basename(img_path)

                origin_x = img_config.get('originX', 0)
                origin_y = img_config.get('originY', 0)

                coord = content.get('coord', [0, 0, 0])

                # 【修正点2】：当前绘制图像的锚点偏移(Origin Offset)
                local_x = (coord[0] if len(coord) > 0 else 0) - content.get('ox', 0.0)
                local_y = (coord[1] if len(coord) > 1 else 0) - content.get('oy', 0.0)

                local_opacity = content.get('opa', 255)
                abs_opacity = int(parent_transform['opacity'] * (local_opacity / 255.0))

                frame_info = {
                    "time": time,
                    "visible": True if abs_opacity > 0 else False,
                    "image": img_name,
                    "origin_x": origin_x,
                    "origin_y": origin_y,
                    "x": parent_transform['x'] + local_x,
                    "y": parent_transform['y'] + local_y,
                    "scale_x": parent_transform['scale_x'] * content.get('zx', 1.0),
                    "scale_y": parent_transform['scale_y'] * content.get('zy', 1.0),
                    "rotation_angle": parent_transform['rotation'] + content.get('angle', 0.0),
                    "opacity": abs_opacity,
                    "blend_mask": content.get('mask', 3)
                }
                layer_frames.append(frame_info)

            except Exception as e:
                print(f"[warn] 解析图层帧失败: layer={layer_name}, src={src}, err={e}")
                layer_frames.append({"time": time, "visible": False})
                continue

        # 如果这个图层只是一个 motion 挂载容器（自己没有任何图片帧），就不要把“空壳层”塞进输出
        if has_inlined_motion_ref and not any('image' in f for f in layer_frames):
            return

        if layer_frames and layer_frames[0]['time'] > 0:
            layer_frames.insert(0, {"time": 0, "visible": False})

        if layer_frames:
            current_anim["layers"].append({
                "layer_name": layer_name,
                "frames": layer_frames
            })

    for anim_name, anim_data in motions.items():
        # 可选：把被引用的 xxxani 子动作从下拉列表里隐藏（避免单独列出来）
        # 如果你想保留它们做调试，把这两行注释掉即可
        if anim_name in referenced_motion_names and re.search(r'ani$', anim_name, flags=re.IGNORECASE):
            continue

        animations_output[anim_name] = {
            "duration": anim_data.get("lastTime", 0),
            "layers": []
        }

        initial_transform = {
            'x': 0.0, 'y': 0.0, 'scale_x': 1.0, 'scale_y': 1.0, 'rotation': 0.0, 'opacity': 255
        }

        for layer in anim_data.get("layer", []):
            parse_layer(
                layer,
                animations_output[anim_name],
                initial_transform,
                time_shift=0.0,
                motion_stack=[anim_name]
            )

    with open('extracted_animations.json', 'w', encoding='utf-8') as f:
        json.dump(animations_output, f, ensure_ascii=False, indent=2)
    print("✅ 完美的提取数据！骨架无偏移，特效锚点已反向修正！")


def detect_default_prefix() -> str:
    try:
        prefix, _, _ = find_sd_bundle()
        return prefix
    except Exception:
        return 'sd000'


def get_next_export_filename(prefix: str, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    pattern = re.compile(rf'^{re.escape(prefix)}-(\d+)\.png$', flags=re.IGNORECASE)
    max_index = 0
    for name in os.listdir(output_dir):
        m = pattern.match(name)
        if m:
            idx = int(m.group(1))
            if idx > max_index:
                max_index = idx
    return f"{prefix}-{max_index + 1}.png"


class ExportHandler(SimpleHTTPRequestHandler):
    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != '/api/export_frame':
            self._send_json(404, {'ok': False, 'error': '接口不存在'})
            return

        qs = parse_qs(parsed.query)
        prefix = (qs.get('prefix', [''])[0] or '').strip().lower()
        if not prefix:
            prefix = detect_default_prefix()

        if not re.fullmatch(r'[a-zA-Z0-9_-]+', prefix):
            self._send_json(400, {'ok': False, 'error': 'prefix 非法'})
            return

        content_type = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if content_type != 'image/png':
            self._send_json(400, {'ok': False, 'error': '只支持 image/png 导出'})
            return

        try:
            content_length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self._send_json(400, {'ok': False, 'error': '空数据'})
            return

        png_bytes = self.rfile.read(content_length)
        if not png_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
            self._send_json(400, {'ok': False, 'error': '数据不是合法 PNG 文件头'})
            return

        output_dir = os.path.join(os.getcwd(), 'output')
        filename = get_next_export_filename(prefix, output_dir)
        fullpath = os.path.join(output_dir, filename)

        with open(fullpath, 'wb') as f:
            f.write(png_bytes)  # 原样写入，不二次压缩/重编码

        self._send_json(200, {
            'ok': True,
            'filename': filename,
            'path': fullpath,
        })


def run_server(host: str = '127.0.0.1', port: int = 25599):
    server = HTTPServer((host, port), ExportHandler)
    print(f"🌐 本地预览服务器已启动: http://{host}:{port}/viewer.html")
    print("📁 导出 PNG 会保存到当前目录 /output 文件夹（自动编号）")
    print("⚠️ 请用这个脚本启动，不要再用 python -m http.server（那个没有导出接口）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止服务器")
    finally:
        server.server_close()


if __name__ == "__main__":
    # 用法：
    #   python 1.py           -> 仅提取 extracted_animations.json
    #   python 1.py serve     -> 先提取，再启动本地预览+导出服务器
    if len(sys.argv) > 1 and sys.argv[1].lower() == 'serve':
        extract_animations()
        run_server()
    else:
        extract_animations()
