# The Hummin binaries. Models are hash-pinned fetchurls; the daemon source is this repo's
# hummin-assistant/ tree (extracted to its own repo later, this becomes a flake input).
{ pkgs, humminPkg, humminMenubarSrc, daemonSrc }:
let
  # The repo's flake still names its wrapper after upstream's `pi`. Its bundled-extension seeder
  # copies read-only store files into ~/.hummin/agent/extensions and dies with EACCES on the next
  # update — make them writable before each start.
  hummin-cli = pkgs.writeShellScriptBin "hummin" ''
    chmod -R u+w "$HOME/.hummin/agent/extensions" 2>/dev/null || true
    exec ${humminPkg}/bin/pi "$@"
  '';

  # Same layout and launcher as the repo's `make install`
  menubar = pkgs.runCommand "hummin-menubar" { nativeBuildInputs = [ pkgs.makeWrapper ]; } ''
    mkdir -p $out/lib/hummin-menubar
    cp -R ${humminMenubarSrc}/hummin_menubar ${humminMenubarSrc}/icon $out/lib/hummin-menubar/
    makeWrapper ${pkgs.python3}/bin/python3 $out/bin/hummin-menubar \
      --set PYTHONPATH $out/lib/hummin-menubar --add-flags "-m hummin_menubar"
  '';

  # CPU only (base.en: ~1.7 s on a short clip). Upgrade: `whisper-cpp.override { vulkanSupport = true; }` is in cache.nixos.org
  whisperModel = pkgs.fetchurl {
    url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin";
    sha256 = "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002";
  };

  # piper looks for the .json next to the .onnx
  voice = pkgs.linkFarm "piper-voice-lessac" [
    {
      name = "en_US-lessac-medium.onnx";
      path = pkgs.fetchurl {
        url = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx";
        sha256 = "5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f";
      };
    }
    {
      name = "en_US-lessac-medium.onnx.json";
      path = pkgs.fetchurl {
        url = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json";
        sha256 = "efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0";
      };
    }
  ];

  oww = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1";
  melspec = pkgs.fetchurl { url = "${oww}/melspectrogram.onnx"; hash = "sha256-uisOD4t7h1NposicsTNg/1O6xDbyiVzO2fR5+mXrF28="; };
  embedding = pkgs.fetchurl { url = "${oww}/embedding_model.onnx"; hash = "sha256-cNFkKQwdCV0dTuFJvF4AVDJQpzFrWfMdBWz/e9MHXB8="; };

  # Filter out Python's caches: a dirty worktree and a clean clone at the same commit must
  # evaluate to the same system.
  daemonSource = builtins.path {
    path = daemonSrc;
    name = "hummin-assistant-src";
    filter = path: type: type != "directory" || baseNameOf path != "__pycache__";
  };
  # trained by hummin-assistant/wakeword/train.sh (synthetic Piper voices; see hey_hummin.yml)
  wakeModel = daemonSource + "/wakeword/hey_hummin.onnx";

  openwakeword = pkgs.python3Packages.buildPythonPackage rec {
    pname = "openwakeword";
    version = "0.6.0";
    pyproject = true;
    build-system = [ pkgs.python3Packages.setuptools ];
    src = pkgs.python3Packages.fetchPypi { inherit pname version; hash = "sha256-NoWNkPEYPjB0hVl6kSpOPDOEsU6pkj+D/q/658FWVWU="; };
    # tflite-runtime has no wheel here (we run onnx); speexdsp-ns is optional and not in nixpkgs
    pythonRemoveDeps = [ "tflite-runtime" "speexdsp-ns" ];
    dependencies = with pkgs.python3Packages; [ onnxruntime scipy scikit-learn tqdm requests ];
    doCheck = false;
  };

  assistant = pkgs.runCommand "hummin-assistant"
    { nativeBuildInputs = [ pkgs.makeWrapper ]; } ''
    mkdir -p $out/lib
    cp -R ${daemonSource} $out/lib/hummin-assistant
    chmod -R u+w $out/lib/hummin-assistant

    # Stubs stand in for mic, speakers, display and notifications
    (
      cd $out/lib/hummin-assistant
      export HOME=$TMPDIR XDG_RUNTIME_DIR=$TMPDIR PYTHONDONTWRITEBYTECODE=1
      ${pkgs.python3}/bin/python3 -m unittest discover -s tests -t .
    )
    find $out/lib -name __pycache__ -prune -exec rm -rf {} +

    makeWrapper ${pkgs.python3}/bin/python3 $out/bin/hummin-assistant \
      --set PYTHONPATH $out/lib/hummin-assistant --add-flags "-m hummin_assistant" \
      --prefix PATH : ${pkgs.lib.makeBinPath (with pkgs; [ whisper-cpp pipewire piper-tts wl-clipboard grim libnotify kitty ])} \
      --set HUMMIN_ASSISTANT_WHISPER_MODEL ${whisperModel} \
      --set HUMMIN_ASSISTANT_VOICE ${voice}/en_US-lessac-medium.onnx \
      --set HUMMIN_ASSISTANT_WAKE $out/bin/hummin-wakeword

    makeWrapper ${pkgs.python3.withPackages (_: [ openwakeword ])}/bin/python3 $out/bin/hummin-wakeword \
      --set PYTHONPATH $out/lib/hummin-assistant --add-flags "-m hummin_assistant.wakeword" \
      --prefix PATH : ${pkgs.pipewire}/bin \
      --set HUMMIN_ASSISTANT_WAKE_MODEL ${wakeModel} \
      --set HUMMIN_ASSISTANT_MELSPEC ${melspec} \
      --set HUMMIN_ASSISTANT_EMBEDDING ${embedding}
  '';
in
{
  hummin = hummin-cli;
  hummin-menubar = menubar;
  hummin-assistant = assistant;
}
