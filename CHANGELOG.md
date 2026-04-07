# Changelog

## 0.2.0 — 2026-04-07

### Added
- **Fade in/out on silence**: visualizer fades to transparent when no audio is playing (`--fade`, `--fade-in-speed`, `--fade-out-speed`, `--silence-threshold`)
- **Configurable saturation boost**: control center glow intensity with `--boost-saturation` (set to `0` to disable)
- **Power-aware framerate**: opt-in scaling by system power profile (`--power-aware`)
- Band clipping optimization — only redraws the active vertical band instead of the full monitor area
- Unified frame tick — single timer that only redraws on new cava data or during fade transitions, reducing idle CPU usage

### Changed
- Saturation boost now uses integrated gradient blending instead of a separate clip+mask pass (smoother, less GPU work)
- Auto-detect framerate without `--power-aware` now defaults to 50% of monitor refresh rate (was 90%)
- Power profile ratios adjusted: power-saver 20%, balanced 33%, performance 50% of refresh rate

## 0.1.0 — 2026-04-04

Initial release.

- Smooth bezier curve visualization with animated 3-color gradient
- Vertical mirror mode
- Saturation-boosted center glow
- Auto framerate from monitor refresh rate + power profile scaling
- Live color reload from Hyprland-format colors file
- Configurable Wayland layer, colors, opacity, and cava settings
- Shell wrapper for automatic LD_PRELOAD detection
