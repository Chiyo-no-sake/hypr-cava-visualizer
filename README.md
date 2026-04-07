# hypr-cava-visualizer

A real-time audio visualizer for [Hyprland](https://hyprland.org/) that renders smooth bezier curves with animated color gradients on your desktop background.

It uses [cava](https://github.com/karlstav/cava) for audio capture, GTK4 for rendering, and [gtk4-layer-shell](https://github.com/wmww/gtk4-layer-shell) to draw on a Wayland layer surface — behind your windows but above your wallpaper.

<!-- Add a screenshot: place it in the repo as screenshot.png and uncomment below -->
<!-- ![screenshot](screenshot.png) -->

> The visualizer draws smooth, flowing curves across the full width of your monitor. Colors animate as a shifting gradient using your three theme colors, with a saturation-boosted glow near the center line. In mirror mode (default), the curves are reflected vertically.

## Features

- Smooth bezier curve rendering (not blocky bars)
- Three-color animated gradient with configurable speed
- Vertical mirror mode
- Saturation-boosted center glow effect (configurable intensity)
- Fade in/out on silence with configurable speed
- Automatic framerate detection from monitor refresh rate
- Power profile awareness (optional — scales framerate on battery)
- Live color reloading from Hyprland-format color files (e.g. [matugen](https://github.com/InioX/matugen))
- Configurable Wayland layer (background, bottom, top, overlay)
- Monstercat smoothing via cava
- Band clipping optimization (only redraws the active area)

## Dependencies

| Dependency | Purpose |
|---|---|
| Python 3.10+ | Runtime |
| GTK4 | UI toolkit |
| [gtk4-layer-shell](https://github.com/wmww/gtk4-layer-shell) | Wayland layer surface |
| [cava](https://github.com/karlstav/cava) | Audio capture |
| PyGObject | Python GTK4 bindings |
| PyCairo | 2D drawing |

### Fedora

```sh
sudo dnf install cava gtk4-layer-shell python3-gobject python3-cairo
```

### Arch Linux

```sh
sudo pacman -S cava gtk4-layer-shell python-gobject python-cairo
```

### Ubuntu / Debian

```sh
sudo apt install cava libgtk-4-1 libgtk4-layer-shell0 python3-gi python3-gi-cairo gir1.2-gtk-4.0
```

## Installation

```sh
git clone https://github.com/Chiyo-no-sake/hypr-cava-visualizer.git
cd hypr-cava-visualizer
sudo make install
```

This installs to `/usr/local/bin/`. The included wrapper script automatically finds `libgtk4-layer-shell` on your system — no manual `LD_PRELOAD` needed.

Custom prefix:

```sh
sudo make install PREFIX=/usr
```

Uninstall:

```sh
sudo make uninstall
```

## Usage

```sh
hypr-cava-visualizer [OPTIONS]
```

### CLI Options

| Option | Default | Description |
|---|---|---|
| `--bars N` | `50` | Number of visualization bars |
| `--framerate N` | `0` (auto) | Target framerate. `0` = auto-detect from monitor refresh rate |
| `--height-pct N` | `20` | Visualization height as percentage of monitor height |
| `--opacity N` | `0.55` | Fill opacity (0-1) |
| `--mirror` / `--no-mirror` | `--mirror` | Vertical mirroring |
| `--monstercat` / `--no-monstercat` | `--monstercat` | Monstercat smoothing in cava |
| `--noise-reduction N` | `0.50` | Cava noise reduction (0-1) |
| `--sensitivity N` | `150` | Cava sensitivity |
| `--gradient-speed N` | `0.15` | Gradient animation speed (cycles/sec) |
| `--layer LAYER` | `background` | Wayland layer: `background`, `bottom`, `top`, `overlay` |
| `--color-primary HEX` | `#F2BF6E` | Primary gradient color |
| `--color-secondary HEX` | `#E6BDC0` | Secondary gradient color |
| `--color-tertiary HEX` | `#B5CF9E` | Tertiary gradient color |
| `--colors-file PATH` | *(none)* | Hyprland-format colors file (overrides `--color-*` flags) |
| `--cava-config-dir PATH` | `$XDG_RUNTIME_DIR` | Directory for generated cava config |
| `--fade` / `--no-fade` | `--no-fade` | Fade in/out when audio goes silent |
| `--fade-in-speed N` | `3.0` | Fade-in speed in opacity units/sec |
| `--fade-out-speed N` | `1.5` | Fade-out speed in opacity units/sec |
| `--silence-threshold N` | `0.02` | Peak level below which audio is silent (0-1) |
| `--boost-saturation N` | `0.35` | Saturation boost at center glow, `0` to disable |
| `--power-aware` / `--no-power-aware` | `--no-power-aware` | Scale framerate by system power profile |

### Examples

Default warm palette:

```sh
hypr-cava-visualizer
```

Custom neon colors with higher opacity:

```sh
hypr-cava-visualizer \
    --color-primary "#FF6B6B" \
    --color-secondary "#4ECDC4" \
    --color-tertiary "#45B7D1" \
    --opacity 0.7
```

Using a colors file with fade and power-aware scaling:

```sh
hypr-cava-visualizer \
    --colors-file ~/.config/hypr/colors.conf \
    --height-pct 35 --opacity 0.33 \
    --fade --fade-in-speed 3.0 --fade-out-speed 1.5 \
    --boost-saturation 0.25 --power-aware
```

Minimal, no effects:

```sh
hypr-cava-visualizer --no-fade --boost-saturation 0
```

## Colors File Format

The `--colors-file` option reads Hyprland-format color definitions. The visualizer looks for `$primary`, `$secondary`, and `$tertiary` variables:

```conf
$primary = rgb(F2BF6E)
$secondary = rgb(E6BDC0)
$tertiary = rgb(B5CF9E)
```

When `--colors-file` is provided, the file is watched for changes and colors reload automatically (polled every 2 seconds). This works well with theme generators like [matugen](https://github.com/InioX/matugen) that write colors on wallpaper change.

See [`examples/colors.conf`](examples/colors.conf) for a template.

## Hyprland Integration

Add layer rules and autostart to `~/.config/hypr/hyprland.conf`:

```conf
# Layer rules
layerrule = noanim, hypr-cava-visualizer
layerrule = blur, hypr-cava-visualizer
layerrule = blurpasses, 0, hypr-cava-visualizer

# Autostart
exec-once = hypr-cava-visualizer
```

See [`examples/hyprland.conf`](examples/hyprland.conf) for more integration examples.

## Power Profile Integration

When `--power-aware` is enabled and framerate is set to auto-detect (`--framerate 0`, the default), the visualizer reads the active power profile via `net.hadess.PowerProfiles` on D-Bus and scales the framerate proportionally to your monitor's refresh rate:

| Profile | Framerate scaling |
|---|---|
| `power-saver` | ~20% of refresh rate |
| `balanced` | ~33% of refresh rate |
| `performance` | ~50% of refresh rate |

Without `--power-aware`, auto-detect defaults to 50% of the monitor refresh rate (minimum 30 fps).

If the D-Bus service is unavailable, the `balanced` profile is assumed. If Hyprland IPC is also unavailable (e.g. running outside Hyprland), the monitor refresh rate defaults to 60 Hz.

## Running Without the Wrapper

If you prefer to run the Python script directly (e.g. during development), you need to set `LD_PRELOAD` manually:

```sh
LD_PRELOAD=/usr/lib64/libgtk4-layer-shell.so.0 ./hypr-cava-visualizer.py
```

The library path varies by distro. Check with `find /usr -name 'libgtk4-layer-shell*'`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
