# Changelog

## Unreleased

- Bump hummin-menubar to 3a2042a (warm rows adopted when already running, `~` expanded in `warmKeyFile`, tray registration keeps retrying, warm/cold label only on running rows, mode label ignores ssh warnings, a server mid-request counts as warm).
- Bump serpantinum to v2.5.2 (cf41260); the three patches apply cleanly and the patched build succeeds.

## 0.1.0 — 2026-11-17

First release: Hummin as one flake for any NixOS machine.

- `services.hummin.enable` — hummin CLI, assistant daemon (voice/keyboard/wake word) and
  tray menubar as systemd user units.
- On serpantinum hosts: the shell UI (bar button, popup / side-panel / dropdown chat, Hummin
  settings page) grafted onto the host's serpantinum via an order-independent overlay.
  Elsewhere: tray menu, dropdown (kitty) look, wake word; settings at
  `~/.config/hummin/settings.json` (picked at daemon start by which settings file exists).
- Dropdown look dispatches serpantinum's Lua dispatcher first, stock Hyprland
  `togglespecialworkspace` as fallback.
- CI: both eval hosts built per push, daemon test suite on 3.13/3.15, weekly patch-drift
  against pinned serpantinum and upstream master (advisory), hygiene (gitleaks, statix,
  deadnix, nixpkgs-fmt, actionlint).
- Pinned to the revs the author runs: nixpkgs ac588ef, hummin d517599, hummin-menubar
  233022c, serpantinum v2.5.1 (83add3a).
