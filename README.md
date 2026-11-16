# hummin-flake

A Siri-like assistant for NixOS. Hold **Super+Space** to talk, tap to type. Voice in / voice
out, selected text as context, screenshots with Ctrl+S, a "Hey Hummin" wake word, and a tray
menubar that drives an LLM fleet. Connects to local and remote LLMs.

On the [serpantinum](https://github.com/ilyamiro/serpantinum) desktop it gets the full native
UI — bar button, popup / side-panel / dropdown chat, Hummin settings page. On any other NixOS
setup it still works: tray menu, dropdown (kitty) look, wake word.

| Piece | What it is |
|---|---|
| `hummin` CLI | the assistant itself — a maintained fork of Pi, kept synced with upstream |
| `hummin-assistant` | voice/keyboard daemon (whisper.cpp, piper, openWakeWord) + systemd user unit |
| `hummin-menubar` | tray applet: fleet control, model switching |
| serpantinum UI | three patches grafting the shell UI in — applied **only when the host runs serpantinum** |

Removal is a rebuild: delete the lines below and your system returns to exactly what it was —
on serpantinum hosts the shell literally rebuilds without the UI. No residue.

## Install

```nix
# flake.nix
inputs.hummin-flake.url = "github:rennf93/hummin-flake";
```

```nix
# your configuration
imports = [ hummin-flake.nixosModules.default ];
services.hummin.enable = true;
```

If the host runs serpantinum (its overlay active), the UI is grafted onto **your** serpantinum
build — the module's patch overlay is applied last, so it's the final word. The patches target
upstream serpantinum; if they don't fit your version the build fails loudly, never half-installs.

### Keybinding (Super+Space)

On serpantinum, in the Lua keybind file (`~/.config/hypr/config/keybinds.lua` on the author's
machine — any Hyprland keybinding works, these just call the daemon):

```lua
hl.bind(mainMod .. " + SPACE", hl.dsp.exec_cmd("hummin-assistant key down"))
hl.bind(mainMod .. " + SPACE", hl.dsp.exec_cmd("hummin-assistant key up"), { release = true })
hl.bind(mainMod .. " + Super_L", hl.dsp.exec_cmd("hummin-assistant key up"), { release = true, ignore_mods = true })
```

Stock-Hyprland equivalent (classic syntax):

```
bind = SUPER, SPACE, exec, hummin-assistant key down
bindrl = SUPER, SPACE, exec, hummin-assistant key up
```

Tap opens the assistant in your chosen look; hold is push-to-talk. The daemon decides
tap-vs-hold (300 ms) — the binds only tell it the key went down and up.

### Looks

Default is the popup (serpantinum hosts). Side panel and dropdown are switchable in the Hummin
settings page, or in settings. The **dropdown** look runs hummin in a kitty window on a
`hummin` special workspace (works on serpantinum's Lua dispatcher and stock Hyprland alike)
and needs one window rule for class `hummin-dropdown` plus that special workspace. Skip this
and use popup/side — they need nothing extra.

### Settings

- serpantinum host: the Hummin settings page manages the `assistant` key of
  `~/.config/serpantinum/settings.json` (same place the shell reads).
- any other desktop: `~/.config/hummin/settings.json` — seeded on first login with
  `{"look": "popup", "voice": "typing", "wakeWord": true, "model": "..."}`, yours to edit.

`~/.config/hummin/env` (optional `KEY=value` lines) is loaded by the daemon unit — provider
API keys, `HUMMIN_CONFIRM=1` for tool-confirm notifications, etc. The menubar drives remote
fleets via its own `servers.json`; local llama.cpp/OpenAI-compatible endpoints appear on their own.

## Versions and maintenance

`flake.lock` pins the exact revs the author's machine runs (see the header of `flake.nix`).
Bump like any flake: `nix flake update serpantinum hummin hummin-menubar`, and if the patch
contexts moved, adjust `patches/`. CI keeps this honest: builds on every push, runs the daemon
test suite, and weekly applies the patch chain against the pinned serpantinum and against
upstream master (advisory) so drift is caught the day it happens.

Troubleshooting: `journalctl --user -u hummin-assistant -u hummin-menubar`.

## Layout

```
hummin-assistant/   daemon source (vendored; extracted repo may replace it)
patches/            the three serpantinum UI patches (applied in order: button → assistant → fleet)
module.nix          services.hummin.enable — the NixOS module
pkgs.nix            the three package builders
```

## Credits

Hummin is a maintained fork of Pi. hummin-menubar and the hummin-assistant daemon are original
work by [@rennf93](https://github.com/rennf93); serpantinum is
[@ilyamiro](https://github.com/ilyamiro)'s. Kept external by design — nothing patches
serpantinum except the three optional UI patches, and everything removes cleanly.
