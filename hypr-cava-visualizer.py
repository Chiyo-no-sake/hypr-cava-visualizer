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

args = _parser.parse_args()

if args.no_mirror:
    args.mirror = False
if args.no_monstercat:
    args.monstercat = False

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


def reload_colors():
    global fg_primary, fg_secondary, fg_tertiary
    fg_primary, fg_secondary, fg_tertiary = load_colors()


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
    profile = detect_power_profile()
    caps = {"power-saver": 0.45, "balanced": 0.65}
    ratio = caps.get(profile, 0.90)
    return max(30, min(int(refresh_rate * ratio), refresh_rate))


if args.framerate <= 0:
    args.framerate = apply_power_scaling(detect_refresh_rate())

# ---------------------------------------------------------------------------
# Drawing state
# ---------------------------------------------------------------------------

bar_values = [0.0] * args.bars
win = None
drawing_area = None
_gradient_phase = 0.0


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
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()

    n = len(bar_values)
    if n == 0:
        return

    max_h = height * (args.height_pct / 100)
    center_y = height / 2

    cr.set_operator(cairo.OPERATOR_OVER)

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

    # Animated gradient position
    p = _gradient_phase
    span = width * 1.5
    gx0 = -span + p * span
    gx1 = gx0 + span

    # Fill gradient
    gradient = cairo.LinearGradient(gx0, 0, gx1, 0)
    gradient.add_color_stop_rgba(0.00, *fg_primary, args.opacity)
    gradient.add_color_stop_rgba(0.33, *fg_secondary, args.opacity)
    gradient.add_color_stop_rgba(0.66, *fg_tertiary, args.opacity)
    gradient.add_color_stop_rgba(1.00, *fg_primary, args.opacity)
    gradient.set_extend(cairo.Extend.REPEAT)

    # Line gradient (brighter)
    lo = min(1.0, args.opacity * 2)
    line_gradient = cairo.LinearGradient(gx0, 0, gx1, 0)
    line_gradient.add_color_stop_rgba(0.00, *fg_primary, lo)
    line_gradient.add_color_stop_rgba(0.33, *fg_secondary, lo)
    line_gradient.add_color_stop_rgba(0.66, *fg_tertiary, lo)
    line_gradient.add_color_stop_rgba(1.00, *fg_primary, lo)
    line_gradient.set_extend(cairo.Extend.REPEAT)

    # Top curve fill
    cr.new_path()
    smooth_curve(cr, points_top)
    cr.line_to(width, center_y)
    cr.line_to(0, center_y)
    cr.close_path()
    cr.set_source(gradient)
    cr.fill()

    # Top curve stroke
    cr.new_path()
    smooth_curve(cr, points_top)
    cr.set_source(line_gradient)
    cr.set_line_width(2)
    cr.stroke()

    # Mirror: bottom curve
    if args.mirror:
        cr.new_path()
        smooth_curve(cr, points_bot)
        cr.line_to(width, center_y)
        cr.line_to(0, center_y)
        cr.close_path()
        cr.set_source(gradient)
        cr.fill()

        cr.new_path()
        smooth_curve(cr, points_bot)
        cr.set_source(line_gradient)
        cr.set_line_width(2)
        cr.stroke()

    # Saturation-boosted center glow
    boost_h = max_h * 0.25
    bp = boost_saturation(fg_primary, 0.35)
    bs = boost_saturation(fg_secondary, 0.35)
    bt = boost_saturation(fg_tertiary, 0.35)
    boost_opacity = min(1.0, args.opacity * 1.4)

    boost_grad = cairo.LinearGradient(gx0, 0, gx1, 0)
    boost_grad.add_color_stop_rgba(0.00, *bp, boost_opacity)
    boost_grad.add_color_stop_rgba(0.33, *bs, boost_opacity)
    boost_grad.add_color_stop_rgba(0.66, *bt, boost_opacity)
    boost_grad.add_color_stop_rgba(1.00, *bp, boost_opacity)
    boost_grad.set_extend(cairo.Extend.REPEAT)

    fade = cairo.LinearGradient(0, center_y - boost_h, 0, center_y + boost_h)
    fade.add_color_stop_rgba(0.0, 0, 0, 0, 0)
    fade.add_color_stop_rgba(0.4, 0, 0, 0, 1)
    fade.add_color_stop_rgba(0.5, 0, 0, 0, 1)
    fade.add_color_stop_rgba(0.6, 0, 0, 0, 1)
    fade.add_color_stop_rgba(1.0, 0, 0, 0, 0)

    cr.save()
    cr.new_path()
    smooth_curve(cr, points_top)
    cr.line_to(width, center_y)
    cr.line_to(0, center_y)
    cr.close_path()
    if args.mirror:
        smooth_curve(cr, points_bot)
        cr.line_to(width, center_y)
        cr.line_to(0, center_y)
        cr.close_path()
    cr.clip()
    cr.set_source(boost_grad)
    cr.mask(fade)
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
    global bar_values, _cava_proc
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
            GLib.idle_add(queue_draw)
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


def queue_draw():
    if drawing_area:
        drawing_area.queue_draw()
    return False


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

    def tick_gradient():
        global _gradient_phase
        _gradient_phase = (_gradient_phase + args.gradient_speed / 60) % 1.0
        if drawing_area:
            drawing_area.queue_draw()
        return True

    GLib.timeout_add(16, tick_gradient)

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
