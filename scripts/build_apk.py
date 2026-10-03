#!/usr/bin/env python3
"""Собрать APK, назвать его app-<N>.apk и положить в releases/.

Версия — просто счётчик N (1, 2, 3, ...). Он:
  * вшивается в приложение:  --dart-define=APP_VERSION=N
  * шьётся в имя файла:      releases/app-<N>.apk
  * из этого же имени manifest() на маке читает «кто последняя».

Парадокса нет: N не нужно знать в code — приложение при запуске само
спрашивает /update/manifest и сравнивает со своим APP_VERSION.

Использование:
  python3 scripts/build_apk.py                # N = max(существующих) + 1
  python3 scripts/build_apk.py --version 3    # явно
  python3 scripts/build_apk.py --force        # перезаписать app-<N>.apk

Требует flutter в PATH и Android SDK (собирается там, где есть flutter).
Значения SERVER_URL / AUTH_TOKEN / UPDATE_BASE_URL берутся из secrets/defs.json.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
RELEASES = ROOT / "releases"
DEFS = ROOT / "secrets" / "defs.json"
BUILT = APP / "build" / "app" / "outputs" / "flutter-apk" / "app-release.apk"
DEFAULT_HOST = "https://llm.iq-factura.com"


def _def(name: str, default: str = "") -> str:
    if not DEFS.exists():
        return default
    data = json.loads(DEFS.read_text())
    return str(data.get(name, default))


def existing_versions() -> list[int]:
    out = []
    if RELEASES.exists():
        for f in RELEASES.glob("app-*.apk"):
            m = re.fullmatch(r"app-(\d+)\.apk", f.name)
            if m:
                out.append(int(m.group(1)))
    return out


def next_version() -> int:
    vs = existing_versions()
    return (max(vs) + 1) if vs else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", type=int, help="N (по умолчанию max+1)")
    ap.add_argument("--force", action="store_true",
                    help="перезаписать уже существующий app-<N>.apk")
    ap.add_argument("--no-build", action="store_true",
                    help="просто переименовать/скопировать готовый app-release.apk")
    a = ap.parse_args()

    server = _def("SERVER_URL", DEFAULT_HOST)
    base = _def("UPDATE_BASE_URL", server)
    token = _def("AUTH_TOKEN", "")
    n = a.version or next_version()
    dst = RELEASES / f"app-{n}.apk"

    if dst.exists() and not a.force:
        sys.exit(f"❌ {dst} уже существует; существующие: "
                 f"{sorted(existing_versions())}. Используйте --version <N> или --force.")

    if not a.no_build:
        print(f"==> flutter: версия={n}  SERVER_URL={server}  UPDATE_BASE_URL={base}  "
              f"AUTH_TOKEN={'<set>' if token else '<пусто>'}")
        subprocess.run(["flutter", "pub", "get"], cwd=APP, check=True)
        subprocess.run(
            [
                "flutter", "build", "apk", "--release",
                f"--dart-define=SERVER_URL={server}",
                f"--dart-define=AUTH_TOKEN={token}",
                f"--dart-define=UPDATE_BASE_URL={base}",
                f"--dart-define=APP_VERSION={n}",
            ],
            cwd=APP, check=True,
        )

    if not BUILT.exists():
        sys.exit(f"❌ не найден собранный {BUILT} (flutter build apk не прошёл?)")

    RELEASES.mkdir(exist_ok=True)
    shutil.copyfile(BUILT, dst)
    mb = dst.stat().st_size / 1048576
    print(f"✅ {dst}  ({mb:.1f} МБ) — релиз N={n}")
    print(f"   существующие релизы: {sorted(existing_versions())}")


if __name__ == "__main__":
    main()
