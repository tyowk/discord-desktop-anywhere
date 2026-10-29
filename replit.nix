{ pkgs }: {
  deps = [
    # --- Virtual desktop and VNC (noVNC is only used for its static web client) ---
    pkgs.xorg.xorgserver
    pkgs.x11vnc
    pkgs.novnc
    pkgs.openbox
    pkgs.xdotool
    pkgs.xorg.xsetroot
    pkgs.xorg.xdpyinfo
    pkgs.python311

    # --- Audio: virtual speaker + live encoding for the browser ---
    pkgs.pulseaudio
    pkgs.ffmpeg

    # --- Utilities ---
    pkgs.curl
    pkgs.imagemagick   # native-resolution screenshots
    pkgs.patchelf      # lets the downloaded Discord binaries run on Nix
    pkgs.glibc
    pkgs.file
    pkgs.gnutar
    pkgs.gzip
    pkgs.util-linux    # setpriv: children die together with the supervisor
    pkgs.xdg-utils
    pkgs.dbus

    # --- Electron / Discord runtime libraries ---
    pkgs.nss
    pkgs.nspr
    pkgs.gtk3
    pkgs.glib
    pkgs.alsa-lib
    pkgs.at-spi2-atk
    pkgs.at-spi2-core
    pkgs.atk
    pkgs.cups
    pkgs.libdrm
    pkgs.mesa
    pkgs.libxkbcommon
    pkgs.pango
    pkgs.cairo
    pkgs.expat
    pkgs.libpulseaudio
    pkgs.libnotify
    pkgs.libuuid
    pkgs.systemd
    pkgs.xorg.libX11
    pkgs.xorg.libXcomposite
    pkgs.xorg.libXdamage
    pkgs.xorg.libXext
    pkgs.xorg.libXfixes
    pkgs.xorg.libXrandr
    pkgs.xorg.libXtst
    pkgs.xorg.libXScrnSaver
    pkgs.xorg.libxcb
    pkgs.xorg.libxshmfence
    pkgs.fontconfig
    pkgs.freetype
    pkgs.dejavu_fonts
    pkgs.noto-fonts
    pkgs.noto-fonts-emoji
  ];

  env = {
    LOADER = "${pkgs.glibc}/lib/ld-linux-x86-64.so.2";
    NOVNC_DIR = "${pkgs.novnc}/share/webapps/novnc";

    LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
      pkgs.nss
      pkgs.nspr
      pkgs.gtk3
      pkgs.glib
      pkgs.alsa-lib
      pkgs.at-spi2-atk
      pkgs.at-spi2-core
      pkgs.atk
      pkgs.cups
      pkgs.libdrm
      pkgs.mesa
      pkgs.libxkbcommon
      pkgs.pango
      pkgs.cairo
      pkgs.expat
      pkgs.libpulseaudio
      pkgs.libnotify
      pkgs.libuuid
      pkgs.systemd
      pkgs.fontconfig
      pkgs.freetype
      pkgs.xorg.libX11
      pkgs.xorg.libXcomposite
      pkgs.xorg.libXdamage
      pkgs.xorg.libXext
      pkgs.xorg.libXfixes
      pkgs.xorg.libXrandr
      pkgs.xorg.libXtst
      pkgs.xorg.libXScrnSaver
      pkgs.xorg.libxcb
      pkgs.xorg.libxshmfence
      pkgs.stdenv.cc.cc.lib
    ];
  };
}
