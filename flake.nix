# Hummin, packaged for NixOS: the assistant daemon (voice/keyboard/wake word), the tray
# menubar and the hummin CLI — plus, when the host runs serpantinum, its native shell UI
# (bar button, popup/side-panel chat, settings page) grafted onto the host's serpantinum.
# Pins mirror the author's nixos-config flake.lock so releases build what the author runs
# (nixpkgs ac588ef / hummin d517599 / hummin-menubar 233022c / serpantinum v2.5.1 = 83add3a).
{
  description = "Hummin — Siri-like assistant for NixOS (native UI on serpantinum)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/ac588ef1c914865dad517b337ea0eb9694a4021c";
    serpantinum = { url = "github:ilyamiro/serpantinum/v2.5.1"; inputs.nixpkgs.follows = "nixpkgs"; };
    hummin = {
      url = "github:rennf93/hummin/d5175998122b7c7bfe18c634f8849555b1608087";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.nixpkgs-darwin-x64.follows = "nixpkgs";
    };
    hummin-menubar = { url = "github:rennf93/hummin-menubar-linux/233022caa8863c668879248fa21c0c98553b3c2d"; flake = false; };
  };

  outputs = { self, nixpkgs, serpantinum, hummin, hummin-menubar }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      packages = import ./pkgs.nix {
        inherit pkgs;
        humminPkg = hummin.packages.${system}.default;
        humminMenubarSrc = hummin-menubar;
        daemonSrc = ./hummin-assistant;
      };
      # Eval/build checks, not real machines:
      #   nix build .#nixosConfigurations.<name>.config.system.build.toplevel
      # eval-test: serpantinum host (proves the patch graft composes).
      # eval-test-generic: plain NixOS host (proves the tray/dropdown/wake-word path and
      # the module-managed settings work without serpantinum).
      testHost = name: extra: nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          (_: {
            networking.hostName = name;
            fileSystems."/" = { device = "/dev/disk/by-label/none"; fsType = "ext4"; };
            boot.loader.systemd-boot.enable = true;
            boot.loader.efi.canTouchEfiVariables = true;
            system.stateVersion = "26.05";
            services.hummin.enable = true;
          })
          extra
          self.nixosModules.default
        ];
      };
    in
    {
      packages.${system} = packages;

      nixosModules.default = { pkgs, ... }: {
        imports = [ ./module.nix ];
        _module.args = {
          humminPkg = hummin.packages.${pkgs.stdenv.hostPlatform.system}.default;
          humminMenubarSrc = hummin-menubar;
        };
      };

      nixosConfigurations = {
        eval-test = testHost "hummin-eval-test" ({ pkgs, ... }: {
          nixpkgs.overlays = [ serpantinum.overlays.default ];
          environment.systemPackages = [ pkgs.serpantinum ]; # the host runs serpantinum
        });
        eval-test-generic = testHost "hummin-generic-test" { };
      };
    };
}
