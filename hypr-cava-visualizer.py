#!/usr/bin/python3
"""
hypr-cava-visualizer -- Real-time audio visualizer for Hyprland.

Renders cava output as smooth bezier curves with animated color gradients
on a Wayland layer surface (behind windows, above wallpaper).

Requires: GTK4, gtk4-layer-shell, cava
"""

import argparse
import atexit
import json
import os
import select
import signal
import socket
import subprocess
import threading
import time

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gdk, GLib, Gtk4LayerShell as LayerShell

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

LAYER_MAP = {
    "background": LayerShell.Layer.BACKGROUND,
    "bottom": LayerShell.Layer.BOTTOM,
    "top": LayerShell.Layer.TOP,
    "overlay": LayerShell.Layer.OVERLAY,
}

_parser = argparse.ArgumentParser(
    description="Real-time audio visualizer for Hyprland (cava + GTK4 layer-shell)",
)
_parser.add_argument("--bars", type=int, default=50, help="Number of bars (default: 50)")
_parser.add_argument("--framerate", type=int, default=0, help="Framerate, 0 = auto-detect (default: 0)")
_parser.add_argument("--height-pct", type=float, default=20, help="Height as %% of monitor (default: 20)")
_parser.add_argument("--opacity", type=float, default=0.55, help="Bar opacity 0-1 (default: 0.55)")
_parser.add_argument("--mirror", action="store_true", default=True, help="Mirror bars vertically (default: true)")
_parser.add_argument("--no-mirror", action="store_true", help="Disable vertical mirroring")
_parser.add_argument("--monstercat", action="store_true", default=True, help="Monstercat smoothing (default: true)")
_parser.add_argument("--no-monstercat", action="store_true", help="Disable monstercat smoothing")
_parser.add_argument("--noise-reduction", type=float, default=0.50, help="Noise reduction 0-1 (default: 0.50)")
_parser.add_argument("--sensitivity", type=int, default=150, help="Cava sensitivity (default: 150)")
_parser.add_argument("--gradient-speed", type=float, default=0.15, help="Gradient animation speed in cycles/sec (default: 0.15)")
_parser.add_argument("--layer", type=str, default="background", choices=LAYER_MAP.keys(),
                      help="Wayland layer (default: background)")
_parser.add_argument("--color-primary", type=str, default="#F2BF6E",
                      help='Primary gradient color as "#RRGGBB" (default: #F2BF6E)')
_parser.add_argument("--color-secondary", type=str, default="#E6BDC0",
                      help='Secondary gradient color as "#RRGGBB" (default: #E6BDC0)')
_parser.add_argument("--color-tertiary", type=str, default="#B5CF9E",
                      help='Tertiary gradient color as "#RRGGBB" (default: #B5CF9E)')
_parser.add_argument("--colors-file", type=str, default=None,
                      help="Path to Hyprland-format colors file (overrides --color-* flags)")
_parser.add_argument("--cava-config-dir", type=str, default=None,
                      help="Directory for generated cava config (default: $XDG_RUNTIME_DIR or /tmp)")
# Fade
_parser.add_argument("--fade", action="store_true", default=False,
                      help="Fade in/out on silence (default: off)")
_parser.add_argument("--no-fade", action="store_true", help="Disable fade (explicit)")
_parser.add_argument("--fade-in-speed", type=float, default=3.0,
                      help="Fade-in speed in opacity units/sec (default: 3.0)")
_parser.add_argument("--fade-out-speed", type=float, default=1.5,
                      help="Fade-out speed in opacity units/sec (default: 1.5)")
_parser.add_argument("--silence-threshold", type=float, default=0.02,
                      help="Peak level below which audio is considered silent (default: 0.02)")
# Saturation boost
_parser.add_argument("--boost-saturation", type=float, default=0.35,
                      help="Saturation boost at center, 0 to disable (default: 0.35)")
# Power-aware framerate
_parser.add_argument("--power-aware", action="store_true", default=False,
                      help="Scale framerate by power profile (default: off)")
_parser.add_argument("--no-power-aware", action="store_true", help="Disable power-aware scaling (explicit)")

args = _parser.parse_args()

if args.no_mirror:
    args.mirror = False
if args.no_monstercat:
    args.monstercat = False
if args.no_fade:
    args.fade = False

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------


def parse_hex_color(s):
    """Parse '#RRGGBB' or 'RRGGBB' into (r, g, b) floats 0-1."""
    s = s.strip().lstrip("#")
    if len(s) != 6 or not all(c in "0123456789abcdefABCDEF" for c in s):
        raise ValueError(f"Invalid hex color: '{s}' (expected 6-digit hex like '#FF8800')")
    return (int(s[0:2], 16) / 255, int(s[2:4], 16) / 255, int(s[4:6], 16) / 255)


def parse_hypr_colors(path):
    """Parse Hyprland-format color definitions: $name = rgb(RRGGBB)."""
    colors = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("$") and "= rgb(" in line:
                    name, _, val = line.partition("=")
                    name = name.strip().lstrip("$")
                    hex_str = val.strip().removeprefix("rgb(").removesuffix(")")
                    colors[name] = parse_hex_color(hex_str)
    except Exception:
        pass
    return colors


def interpolate_color(c1, c2, t):
    return (
        c1[0] + (c2[0] - c1[0]) * t,
        c1[1] + (c2[1] - c1[1]) * t,
        c1[2] + (c2[2] - c1[2]) * t,
    )


def rgb_to_hsl(r, g, b):
    cmax, cmin = max(r, g, b), min(r, g, b)
    delta = cmax - cmin
    l = (cmax + cmin) / 2
    if delta == 0:
        return 0, 0, l
    denom = 1 - abs(2 * l - 1)
    s = delta / denom if denom > 1e-7 else 1.0
    if cmax == r:
        h = ((g - b) / delta) % 6
    elif cmax == g:
        h = (b - r) / delta + 2
    else:
        h = (r - g) / delta + 4
    return h / 6, min(max(s, 0.0), 1.0), l


def hsl_to_rgb(h, s, l):
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h * 6) % 2 - 1))
    m = l - c / 2
    h6 = int(h * 6) % 6
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][h6]
    return (r + m, g + m, b + m)


def boost_saturation(rgb, amount=0.35):
    h, s, l = rgb_to_hsl(*rgb)
    s = min(1.0, s + amount)
    return hsl_to_rgb(h, s, l)


# ---------------------------------------------------------------------------
# Color state
# ---------------------------------------------------------------------------


def load_colors():
    """Load colors from file (if given) or CLI hex values."""
    if args.colors_file:
        path = os.path.expanduser(args.colors_file)
        c = parse_hypr_colors(path)
        primary = c.get("primary", parse_hex_color(args.color_primary))
        secondary = c.get("secondary", parse_hex_color(args.color_secondary))
        tertiary = c.get("tertiary", parse_hex_color(args.color_tertiary))
        return primary, secondary, tertiary
    return (
        parse_hex_color(args.color_primary),
        parse_hex_color(args.color_secondary),
        parse_hex_color(args.color_tertiary),
    )


fg_primary, fg_secondary, fg_tertiary = load_colors()
# Pre-computed boosted colors (updated on color reload)
_boosted_primary = _boosted_secondary = _boosted_tertiary = (0, 0, 0)


def _recompute_boosted():
    global _boosted_primary, _boosted_secondary, _boosted_tertiary
    amt = args.boost_saturation
    if amt > 0:
        _boosted_primary = boost_saturation(fg_primary, amt)
        _boosted_secondary = boost_saturation(fg_secondary, amt)
        _boosted_tertiary = boost_saturation(fg_tertiary, amt)


def reload_colors():
    global fg_primary, fg_secondary, fg_tertiary
    fg_primary, fg_secondary, fg_tertiary = load_colors()
    _recompute_boosted()


_recompute_boosted()


# ---------------------------------------------------------------------------
# System detection
# ---------------------------------------------------------------------------


def detect_refresh_rate():
    """Read the active monitor's refresh rate from Hyprland IPC."""
    try:
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        sock_path = f"{runtime}/hypr/{sig}/.socket.sock"
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(1)
            s.connect(sock_path)
            s.send(b"j/monitors")
            chunks = []
            while True:
                data = s.recv(8192)
                if not data:
                    break
                chunks.append(data)
        finally:
            s.close()
        monitors = json.loads(b"".join(chunks))
        for mon in (monitors or []):
            if mon.get("focused"):
                return int(mon.get("refreshRate", 60))
        if monitors:
            return int(monitors[0].get("refreshRate", 60))
    except Exception:
        pass
    return 60


def detect_power_profile():
    """Read active power profile via D-Bus (powerprofiles daemon)."""
    try:
        result = subprocess.run(
            ["busctl", "get-property", "net.hadess.PowerProfiles",
             "/net/hadess/PowerProfiles", "net.hadess.PowerProfiles", "ActiveProfile"],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip().split('"')[1]
    except Exception:
        return "balanced"


def apply_power_scaling(refresh_rate):
    """Cap framerate based on power profile, scaled to monitor refresh rate."""
    if not args.power_aware:
        return max(30, refresh_rate // 2)
    profile = detect_power_profile()
    caps = {"power-saver": 30, "balanced": 45, "performance": 60}
    return caps.get(profile, 60)


if args.framerate <= 0:
    args.framerate = apply_power_scaling(detect_refresh_rate())

# ---------------------------------------------------------------------------
# Drawing state
# ---------------------------------------------------------------------------

bar_values = [0.0] * args.bars
win = None
drawing_area = None
_gradient_phase = 0.0
_last_draw_time = 0.0
_win_opacity = 0.0 if args.fade else 1.0
_cava_dirty = False
_data_frame = False  # True when the current draw was triggered by new cava data
# Cached curve points — reused during fade-only frames to skip bezier recomputation
_cached_points_top = None
_cached_points_bot = None
_points_valid = False


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def smooth_curve(cr, points):
    """Draw a smooth curve through points using cubic bezier splines."""
    if len(points) < 2:
        return
    cr.move_to(*points[0])
    if len(points) == 2:
        cr.line_to(*points[1])
        return
    for i in range(1, len(points) - 1):
        x0, y0 = points[i - 1]
        x1, y1 = points[i]
        x2, y2 = points[i + 1]
        cp1x = x0 + (x1 - x0) * 0.5
        cp1y = y0 + (y1 - y0) * 0.5
        cp2x = x1
        cp2y = y1
        cr.curve_to(cp1x, cp1y, cp2x, cp2y, (x1 + x2) * 0.5, (y1 + y2) * 0.5)
    cr.line_to(*points[-1])


def draw_func(_area, cr, width, height):
    global _gradient_phase, _last_draw_time, _win_opacity

    now = time.monotonic()
    dt = now - _last_draw_time if _last_draw_time > 0 else 0
    _last_draw_time = now
    _gradient_phase = (_gradient_phase + args.gradient_speed * dt) % 1.0

    # Fade window based on audio level
    if args.fade:
        peak = max(bar_values) if bar_values else 0.0
        if peak > args.silence_threshold:
            _win_opacity = min(1.0, _win_opacity + args.fade_in_speed * dt)
        else:
            _win_opacity = max(0.0, _win_opacity - args.fade_out_speed * dt)

        if _win_opacity < 0.005:
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
            return

    n = len(bar_values)
    if n == 0:
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        return

    max_h = height * (args.height_pct / 100)
    center_y = height / 2

    # Clip + clear only the band where bars can appear
    band_top = center_y - max_h * 0.5 - 4
    band_bot = center_y + max_h * 0.5 + 4
    cr.save()
    cr.rectangle(0, band_top, width, band_bot - band_top)
    cr.clip()

    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()

    cr.set_operator(cairo.OPERATOR_OVER)

    global _cached_points_top, _cached_points_bot, _points_valid

    # Reuse cached curve points during fade-only frames (no new cava data)
    if _points_valid and _cached_points_top is not None and not _data_frame:
        points_top = _cached_points_top
        points_bot = _cached_points_bot
    else:
        step = width / max(1, n - 1)
        points_top = []
        points_bot = []
        for i, val in enumerate(bar_values):
            x = i * step
            h = val * max_h * 0.5
            if args.mirror:
                points_top.append((x, center_y - h))
                points_bot.append((x, center_y + h))
            else:
                points_top.append((x, center_y + max_h * 0.5 - h * 2))
                points_bot.append((x, center_y + max_h * 0.5))
        _cached_points_top = points_top
        _cached_points_bot = points_bot
        _points_valid = True

    # 3-color sliding gradient
    span = width * 1.5
    gx0 = -span + _gradient_phase * span
    gx1 = gx0 + span

    oa = args.opacity * _win_opacity

    gradient = cairo.LinearGradient(gx0, 0, gx1, 0)
    gradient.add_color_stop_rgba(0.00, *fg_primary, oa)
    gradient.add_color_stop_rgba(0.33, *fg_secondary, oa)
    gradient.add_color_stop_rgba(0.66, *fg_tertiary, oa)
    gradient.add_color_stop_rgba(1.00, *fg_primary, oa)
    gradient.set_extend(cairo.Extend.REPEAT)

    lo = min(1.0, args.opacity * 2) * _win_opacity
    line_gradient = cairo.LinearGradient(gx0, 0, gx1, 0)
    line_gradient.add_color_stop_rgba(0.00, *fg_primary, lo)
    line_gradient.add_color_stop_rgba(0.33, *fg_secondary, lo)
    line_gradient.add_color_stop_rgba(0.66, *fg_tertiary, lo)
    line_gradient.add_color_stop_rgba(1.00, *fg_primary, lo)
    line_gradient.set_extend(cairo.Extend.REPEAT)

    # Saturation boost near center (uses pre-computed boosted colors)
    boost_amt = args.boost_saturation
    if boost_amt > 0:
        boost_h = max_h * 0.25
        bp = _boosted_primary
        bs = _boosted_secondary
        bt = _boosted_tertiary

        def make_gradient(y_from_center):
            dist = abs(y_from_center) / max(1, boost_h)
            if dist >= 1.0:
                return gradient
            t = 1.0 - dist
            o = (args.opacity + (args.opacity * 0.5) * t) * _win_opacity
            g = cairo.LinearGradient(gx0, 0, gx1, 0)
            g.add_color_stop_rgba(0.00, *interpolate_color(fg_primary, bp, t), o)
            g.add_color_stop_rgba(0.33, *interpolate_color(fg_secondary, bs, t), o)
            g.add_color_stop_rgba(0.66, *interpolate_color(fg_tertiary, bt, t), o)
            g.add_color_stop_rgba(1.00, *interpolate_color(fg_primary, bp, t), o)
            g.set_extend(cairo.Extend.REPEAT)
            return g

        fill_top = make_gradient(-max_h * 0.15)
        fill_bot = make_gradient(max_h * 0.15)
    else:
        fill_top = gradient
        fill_bot = gradient

    # Top half
    cr.new_path()
    smooth_curve(cr, points_top)
    stroke_top = cr.copy_path()
    cr.line_to(width, center_y)
    cr.line_to(0, center_y)
    cr.close_path()
    cr.set_source(fill_top)
    cr.fill()

    cr.new_path()
    cr.append_path(stroke_top)
    cr.set_source(line_gradient)
    cr.set_line_width(2)
    cr.stroke()

    # Mirror: bottom half
    if args.mirror:
        cr.new_path()
        smooth_curve(cr, points_bot)
        stroke_bot = cr.copy_path()
        cr.line_to(width, center_y)
        cr.line_to(0, center_y)
        cr.close_path()
        cr.set_source(fill_bot)
        cr.fill()

        cr.new_path()
        cr.append_path(stroke_bot)
        cr.set_source(line_gradient)
        cr.set_line_width(2)
        cr.stroke()

    cr.restore()


# ---------------------------------------------------------------------------
# Cava integration
# ---------------------------------------------------------------------------


def build_cava_config():
    """Write a temporary cava config and return its path."""
    if args.cava_config_dir:
        config_dir = os.path.expanduser(args.cava_config_dir)
    else:
        runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
        config_dir = os.path.join(runtime, "hypr-cava-visualizer")
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "cava.conf")
    config = f"""[general]
bars = {args.bars}
framerate = {args.framerate}
autosens = 1
sensitivity = {args.sensitivity}
monstercat = {1 if args.monstercat else 0}
noise_reduction = {args.noise_reduction}

[output]
method = raw
raw_target = /dev/stdout
data_format = ascii
ascii_max_range = 1000
channels = mono
mono_option = average
"""
    with open(config_path, "w") as f:
        f.write(config)
    return config_path


_cava_proc = None


def cava_reader():
    """Read cava output in a background thread and update bar_values."""
    global bar_values, _cava_proc, _cava_dirty
    config_path = build_cava_config()
    proc = subprocess.Popen(
        ["cava", "-p", config_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    _cava_proc = proc
    try:
        fd = proc.stdout.fileno()
        os.set_blocking(fd, False)
        buf = b""
        while True:
            select.select([fd], [], [])
            # Drain all available data, keep only the last complete line
            while True:
                try:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        return
                    buf += chunk
                except BlockingIOError:
                    break
            if b"\n" not in buf:
                continue
            rest, _, trailing = buf.rpartition(b"\n")
            buf = trailing
            if b"\n" in rest:
                last_line = rest.rsplit(b"\n", 1)[1]
            else:
                last_line = rest
            vals = last_line.decode().strip().rstrip(";").split(";")
            try:
                bar_values = [min(1.0, int(v) / 1000) for v in vals if v]
            except ValueError:
                continue
            _cava_dirty = True
    except Exception:
        pass
    finally:
        proc.kill()
        proc.wait()


def _cleanup_cava():
    if _cava_proc and _cava_proc.poll() is None:
        _cava_proc.kill()
        _cava_proc.wait()


atexit.register(_cleanup_cava)


# ---------------------------------------------------------------------------
# Color file watcher
# ---------------------------------------------------------------------------


def color_reload_watcher():
    """Watch colors file for changes and reload on modification."""
    if not args.colors_file:
        return
    path = os.path.expanduser(args.colors_file)
    last_mtime = 0
    while True:
        try:
            mtime = os.path.getmtime(path)
            if mtime != last_mtime:
                last_mtime = mtime
                GLib.idle_add(reload_colors)
        except Exception:
            pass
        time.sleep(2)


# ---------------------------------------------------------------------------
# GTK application
# ---------------------------------------------------------------------------


def on_activate(app):
    global win, drawing_area

    win = Gtk.ApplicationWindow(application=app)

    LayerShell.init_for_window(win)
    LayerShell.set_layer(win, LAYER_MAP[args.layer])
    LayerShell.set_anchor(win, LayerShell.Edge.TOP, True)
    LayerShell.set_anchor(win, LayerShell.Edge.BOTTOM, True)
    LayerShell.set_anchor(win, LayerShell.Edge.LEFT, True)
    LayerShell.set_anchor(win, LayerShell.Edge.RIGHT, True)
    LayerShell.set_exclusive_zone(win, -1)
    LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
    LayerShell.set_namespace(win, "hypr-cava-visualizer")

    drawing_area = Gtk.DrawingArea()
    drawing_area.set_vexpand(True)
    drawing_area.set_hexpand(True)
    drawing_area.set_draw_func(draw_func)
    win.set_child(drawing_area)

    css = Gtk.CssProvider()
    css.load_from_string("window, window.background { background: transparent; }")
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )

    win.present()

    # Vblank-synced frame tick — fires once per compositor frame via the
    # frame clock, eliminating timer drift that causes perceived lag.
    _frame_skip = max(1, round(detect_refresh_rate() / args.framerate))
    _frame_counter = [0]

    def frame_tick(widget, clock):
        global _cava_dirty, _data_frame
        _frame_counter[0] += 1
        if _frame_counter[0] % _frame_skip != 0:
            return True
        fading = args.fade and 0.005 < _win_opacity < 0.995
        if (_cava_dirty or fading) and drawing_area:
            _data_frame = _cava_dirty
            _cava_dirty = False
            drawing_area.queue_draw()
        return True

    drawing_area.add_tick_callback(frame_tick)

    threading.Thread(target=cava_reader, daemon=True).start()
    threading.Thread(target=color_reload_watcher, daemon=True).start()


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    app = Gtk.Application(application_id="dev.hypr.cava-visualizer")
    app.connect("activate", on_activate)
    app.run(None)


if __name__ == "__main__":
    main()
