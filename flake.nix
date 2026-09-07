{
  description = "EPD-Dashboard：家庭食品存储看板（服务端 + Tauri 客户端）";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
  };

  outputs =
    inputs@{ self, flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      flake.nixosModules.food-storage = ./deploy/food-storage.nix;

      perSystem =
        {
          pkgs,
          system,
          self',
          ...
        }:
        {
          packages = rec {
            epd-food-server = pkgs.callPackage ./server/package.nix { };
            default = epd-food-server;
          };

          devShells = rec {
            # 服务端开发 / 测试
            default = pkgs.mkShell {
              packages = [
                (pkgs.python3.withPackages (ps: with ps; [
                  fastapi
                  uvicorn
                  psycopg
                  pillow
                  bleak
                  pytest
                  pytest-asyncio
                  httpx
                ]))
                pkgs.postgresql # 本地起临时实例测试 db.py
                pkgs.fontconfig # fc-match 字体发现
                pkgs.noto-fonts-cjk-sans
              ];
              shellHook = ''
                export EPD_FOOD_FONT_PATH=$(fc-match -f '%{file}' 'Noto Sans CJK SC:style=Bold')
                export EPD_FOOD_DSN="host=/tmp/epd-food-dev-pg port=15433 dbname=fooddev user=$USER"
              '';
            };

            # 客户端（Tauri 2 + Vue 3）开发 / 构建
            client = pkgs.mkShell {
              packages = with pkgs; [
                nodejs_24
                rustc
                cargo
                rustfmt
                clippy
                rust-analyzer
                # Tauri 2 Linux 构建依赖
                pkg-config
                dbus
                openssl_3
                glib
                gtk3
                webkitgtk_4_1
                librsvg
                libsoup_3
                cairo
                pango
                gdk-pixbuf
                glib-networking
                curl
                wget
                file
                unzip
              ];
              shellHook = ''
                export GIO_MODULE_DIR="${pkgs.glib-networking}/lib/gio/modules/"
              '';
            };
          };

          checks = {
            server-tests = pkgs.runCommand "epd-food-server-tests"
              {
                nativeBuildInputs = [
                  (pkgs.python3.withPackages (ps: with ps; [
                    fastapi
                    uvicorn
                    psycopg
                    pillow
                    bleak
                    pytest
                    pytest-asyncio
                    httpx
                  ]))
                  pkgs.fontconfig
                  pkgs.noto-fonts-cjk-sans
                ];
              }
              ''
                export EPD_FOOD_FONT_PATH=$(fc-match -f '%{file}' 'Noto Sans CJK SC:style=Bold')
                cd ${self}
                pytest tests/ -v
                touch $out
              '';
          };
        };
    };
}
