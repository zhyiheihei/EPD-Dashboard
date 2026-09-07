{
  lib,
  stdenv,
  makeWrapper,
  python3,
}:

# EPD-Dashboard 服务端：不构建 wheel，直接以 PYTHONPATH 方式打包源码目录，
# 依赖由 withPackages 的 python 环境提供（fastapi/uvicorn/psycopg/pillow/bleak）。
let
  pythonEnv = python3.withPackages (ps:
    with ps; [
      fastapi
      uvicorn
      psycopg
      pillow
      bleak
    ]);
in
stdenv.mkDerivation {
  pname = "epd-food-server";
  version = "0.1.0";

  src = ./.;

  nativeBuildInputs = [ makeWrapper ];

  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    mkdir -p $out/lib $out/bin
    cp -r epd_food_server $out/lib/epd_food_server
    find $out/lib -name '__pycache__' -type d -exec rm -rf {} +
    makeWrapper ${pythonEnv}/bin/python3 $out/bin/epd-food-server \
      --set PYTHONPATH "$out/lib" \
      --add-flags "-m epd_food_server"
    runHook postInstall
  '';

  # 运行期通过 fontconfig 找系统字体（部署模块会显式传 EPD_FOOD_FONT_PATH）
  meta = {
    description = "家庭食品存储看板服务端（REST API + PostgreSQL + BLE 推送）";
    mainProgram = "epd-food-server";
    platforms = lib.platforms.linux;
  };
}
