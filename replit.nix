{ pkgs }: {
  deps = [
    # --- Virtual Desktop, VNC, and noVNC ---
    pkgs.xorg.xorgserver
    pkgs.x11vnc
    pkgs.novnc
    pkgs.python311Packages.websockify
    pkgs.openbox
    pkgs.xdotool
    pkgs.xorg.xsetroot
    pkgs.xorg.xdpyinfo
    pkgs.python311

    # --- Utilities ---
    pkgs.wget
    pkgs.curl
    pkgs.patchelf
    pkgs.glibc
    pkgs.file
    pkgs.gnutar
    pkgs.gzip
    pkgs.xdg-utils
    pkgs.dbus

    # --- Electron / Discord Runtime Libraries ---
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
