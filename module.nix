# services.hummin.enable installs the whole Hummin stack on any NixOS machine: hummin CLI,
# assistant daemon (voice/keyboard/wake word) and the tray menubar. When the host ALSO runs
# serpantinum (its overlay active → prev.serpantinum exists), the shell UI — bar button,
# popup/side-panel chat, Hummin settings page — is grafted onto the host's serpantinum build.
# Without serpantinum the stack still works everywhere Wayland does: tray menu, dropdown
# (kitty) look, wake word; settings are managed at ~/.config/hummin/settings.json instead of
# serpantinum's settings.json.
#
# No eval-time peek at pkgs: peeking at pkgs.serpantinum inside the module that contributes
# the defining overlay recurses forever (pkgs → overlay list → config → pkgs). So the UI is
# decided inside the overlay via `prev ? serpantinum`, and serpantinum-vs-generic settings at
# daemon start by checking which settings file exists.
{ config, lib, pkgs, humminPkg, humminMenubarSrc, ... }:
let
  packages = import ./pkgs.nix { inherit pkgs humminPkg humminMenubarSrc; daemonSrc = ./hummin-assistant; };
  cfg = config.services.hummin;
  # serpantinum hosts read the shell's settings.json; elsewhere the module-managed one
  assistantWrapper = pkgs.writeShellScript "hummin-assistant-start" ''
    if [ -f "$HOME/.config/serpantinum/settings.json" ]; then
      exec ${packages.hummin-assistant}/bin/hummin-assistant daemon
    fi
    QS_SETTINGS="$HOME/.config/hummin/settings.json" exec ${packages.hummin-assistant}/bin/hummin-assistant daemon
  '';
in
{
  options.services.hummin.enable = lib.mkEnableOption ''
    Hummin: assistant daemon, tray menubar and (on serpantinum hosts) the shell UI (patches serpantinum)'';

  config = lib.mkIf cfg.enable {
    # The shell UI, only where serpantinum exists to receive it. mkAfter so nothing later in
    # the overlay list can redefine away the patched result. Patches track upstream serpantinum
    # (the author's CI applies them to current upstream weekly); when they don't fit the host's
    # serpantinum version the build fails loudly, never silently.
    nixpkgs.overlays = lib.mkAfter [
      (final: prev: lib.optionalAttrs (prev ? serpantinum) {
        serpantinum = prev.serpantinum.overrideAttrs (old: {
          patches = (old.patches or [ ]) ++ [
            ./patches/serpantinum-hummin-button.patch
            ./patches/serpantinum-hummin-assistant.patch
            ./patches/serpantinum-hummin-fleet.patch
          ];
        });
      })
    ];

    environment.systemPackages = [
      packages.hummin
      packages.hummin-menubar
      packages.hummin-assistant
    ];

    # Same units as the author's machine; his nixos-config carries the full "why" comments.
    systemd.user.services.hummin-menubar = {
      description = "hummin menubar (LLM fleet tray control)";
      partOf = [ "graphical-session.target" ];
      after = [ "graphical-session.target" ];
      wantedBy = [ "graphical-session.target" ];
      # NixOS units get their own PATH, which hid servers.json's ssh, sudo and the systemctl its sudo rules name
      enableDefaultPath = false;
      serviceConfig = {
        ExecStart = "${packages.hummin-menubar}/bin/hummin-menubar";
        # It registers with the tray once, so wait for the StatusNotifierWatcher (any desktop's)
        ExecStartPre = "${pkgs.bash}/bin/bash -c 'for i in {1..60}; do ${pkgs.systemd}/bin/busctl --user status org.kde.StatusNotifierWatcher >/dev/null 2>&1 && exit 0; sleep 1; done'";
        Restart = "on-failure";
        RestartSec = 2;
      };
    };

    systemd.user.services.hummin-assistant = {
      description = "hummin assistant (voice and keyboard front end)";
      partOf = [ "graphical-session.target" ];
      after = [ "graphical-session.target" ];
      wantedBy = [ "graphical-session.target" ];
      # Same as hummin-menubar: the daemon's PATH is the wrapper's plus the session's (hummin, hyprctl)
      enableDefaultPath = false;
      serviceConfig = {
        ExecStart = "${assistantWrapper}";
        WorkingDirectory = "%h";
        EnvironmentFile = "-%h/.config/hummin/env";
        Restart = "on-failure";
        RestartSec = 2; # the 100 ms default hits the start limit (5 in 10 s) before the session is up
      };
    };

    # Seed the module-managed settings once, only where serpantinum's settings.json doesn't
    # already exist (that file is the signal the wrapper keys on). User edits to the copy
    # persist — it lives in the home, not the store.
    systemd.user.services.hummin-settings = {
      wantedBy = [ "graphical-session-pre.target" ];
      before = [ "graphical-session-pre.target" ];
      unitConfig.ConditionPathExists = "!%h/.config/serpantinum/settings.json";
      serviceConfig.Type = "oneshot";
      script = ''
        mkdir -p "$HOME/.config/hummin"
        ${pkgs.jq}/bin/jq '.' ${pkgs.writeText "hummin-settings-defaults.json" (builtins.toJSON {
          look = "popup"; voice = "typing"; wakeWord = true; model = "openrouter/z-ai/glm-5.3-flash";
        })} > "$HOME/.config/hummin/settings.json"
      '';
    };
  };
}
