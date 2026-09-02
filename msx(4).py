#!/usr/bin/env python3
"""MSX Launcher 1.1 - Codespaces/Playit compatibility edition v4 (2026).

ONE-FILE drop-in replacement for the original msx.py launcher.

Goals
-----
* Keep the original MSX `sel*.msx` executable untouched.
* Download the original MSX executable automatically when it is absent.
* Make legacy MSX 1.1.x Playit calls work with modern Playit 1.0.x.
* Work in GitHub Codespaces where systemd is normally unavailable.
* Repair the Forge installation race/failure seen with MSX 1.1.x before MSX
  tries to start the server.
* Force every Java process launched by legacy MSX to the Java version selected in MSX.
* Force Forge's generated run.sh to the Java version selected in MSX.
* Keep modern playitd alive when legacy MSX runs `pkill playit` during cleanup.
* Clamp impossible RAM settings to the actual Codespace memory limit.

Usage
-----
Upload ONLY this file to a repository/Codespace and run:

    python3 msx.py

Hidden shim modes are used internally. Do not run them by hand.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# General paths / settings
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
LINKS_URL = "https://minecraft-sx.github.io/data/links.json"
CONFIG = ROOT / "configuracion.json"
SERVER_DIR = ROOT / "servidor_minecraft"
COMPAT_DIR = ROOT / ".msx_compat"
SHIM_DIR = COMPAT_DIR / "bin"
PLAYIT_SHIM = SHIM_DIR / "playit"
JAVA_SHIM = SHIM_DIR / "java"
PKILL_SHIM = SHIM_DIR / "pkill"
KILLALL_SHIM = SHIM_DIR / "killall"
LOG_FILE = COMPAT_DIR / "msx_compat.log"
LOCK_FILE = COMPAT_DIR / "playit.lock"
FORGE_INSTALL_LOG = COMPAT_DIR / "forge_install.log"

# Playit modern daemon/API.
PLAYIT_SOCKET = "/run/playit/playitd.sock"
PLAYIT_SECRET = "/etc/playit/playit.toml"
PLAYIT_DAEMON_CANDIDATES = (
    "/opt/playit/playitd",
    "/usr/bin/playitd",
    "/usr/local/bin/playitd",
)
PLAYIT_DAEMON_LOG = "/var/log/playit/playit.log"
PLAYIT_DASHBOARD = "https://playit.gg/account/tunnels"
PLAYIT_API = "https://api.playit.gg"
LOCAL_MINECRAFT_PORT = 25565

COMPAT_VERSION = "4.0"


def info(message: str) -> None:
    print(message, flush=True)


def err(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
    _log(message)


def _log(message: str) -> None:
    """Diagnostic log. Never write the Playit secret."""
    try:
        COMPAT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def _sudo() -> list[str]:
    """Codespaces normally has passwordless sudo. -n prevents hanging on prompts."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    return ["sudo", "-n"]


def _run(
    command: list[str],
    *,
    cwd: Optional[Path] = None,
    capture: bool = False,
    timeout: Optional[float] = None,
    env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "text": True,
        "cwd": str(cwd) if cwd else None,
        "timeout": timeout,
        "env": env,
    }
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return subprocess.run(command, **kwargs)


@contextlib.contextmanager
def _exclusive_lock():
    """Serialize legacy Playit calls that MSX fires back-to-back."""
    COMPAT_DIR.mkdir(parents=True, exist_ok=True)
    fh = LOCK_FILE.open("a+")
    try:
        if os.name != "nt":
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if os.name != "nt":
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        fh.close()


# ---------------------------------------------------------------------------
# Original MSX launcher behavior
# ---------------------------------------------------------------------------


def _write_gitignore() -> None:
    path = ROOT / ".gitignore"
    if path.exists():
        return
    path.write_text(
        """/tailscale-cs
/work_area
composer.*
/Python*
*.output
/Modgest
/thanos
/vendor
/bkdir
java/
*.exe
*.msi
*.txt
*.pyc
*.msp
*.msx
.msx_compat/
playit.toml
""",
        encoding="utf-8",
    )


def _download_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "MSX-Compat-v4/2026"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def _download_file(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "MSX-Compat-v4/2026"})
    temp = destination.with_suffix(destination.suffix + ".part")
    with urllib.request.urlopen(request, timeout=90) as response, temp.open("wb") as fh:
        shutil.copyfileobj(response, fh)
    temp.replace(destination)


def _find_local_msx() -> Optional[Path]:
    files = sorted(ROOT.glob("sel*.msx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _get_msx_binary() -> Optional[Path]:
    # A local executable is preferred so a test is reproducible. With only msx.py in the
    # repository, it behaves like the original launcher and downloads the current binary.
    local = _find_local_msx()
    auto_update = os.environ.get("MSX_AUTO_UPDATE", "0").lower() in {"1", "true", "yes", "on"}
    if local and not auto_update:
        return local

    try:
        data = _download_json(LINKS_URL)
        url = data.get("latest_win" if os.name == "nt" else "latest")
        if not isinstance(url, str) or not url:
            raise RuntimeError("links.json no contiene una descarga compatible")
        name = url.rsplit("/", 1)[-1]
        target = ROOT / name
        if target.exists():
            return target
        info(f"[MSX Compat] Descargando MSX original: {name}")
        _download_file(url, target)
        return target
    except Exception as exc:
        err(f"[MSX Compat] No se pudo consultar/descargar MSX: {exc}")
        return local


# ---------------------------------------------------------------------------
# Config / RAM / Java / Forge repair
# ---------------------------------------------------------------------------


def _load_config() -> Optional[dict[str, Any]]:
    if not CONFIG.exists():
        return None
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_config(config: dict[str, Any]) -> None:
    temp = CONFIG.with_suffix(".json.msx-compat.tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    temp.replace(CONFIG)


def _memory_limit_gib() -> Optional[float]:
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text(encoding="utf-8").strip()
        if raw != "max":
            number = int(raw)
            if number > 0:
                return number / (1024 ** 3)
    except Exception:
        pass
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / (1024 ** 2)
    except Exception:
        pass
    return None


def _safe_ram_gib(limit: float) -> int:
    # Keep room for VS Code, Linux, Playit and Forge native/direct memory.
    return max(2, int(limit * 0.68))


def _preflight_config(*, noisy: bool = False) -> Optional[dict[str, Any]]:
    config = _load_config()
    if config is None:
        return None

    changed = False

    # Reverse engineering MSX 1.1.0 shows "local" is its no-tunnel value. "none" causes
    # the empty "La IP del servidor es:" state seen during testing.
    if str(config.get("servicio_a_usar", "")).lower() == "none":
        config["servicio_a_usar"] = "local"
        changed = True
        if noisy:
            err("[MSX Compat] Corregido servicio_a_usar: none -> local.")

    limit = _memory_limit_gib()
    requested = config.get("gbs_de_ram_a_usar")
    if limit and isinstance(requested, (int, float)):
        safe_max = _safe_ram_gib(limit)
        if requested > safe_max:
            old = requested
            config["gbs_de_ram_a_usar"] = safe_max
            flags = config.get("flags")
            if isinstance(flags, str):
                flags = re.sub(r"-Xmx\d+[Gg]", f"-Xmx{safe_max}G", flags)
                flags = re.sub(r"-Xms\d+[Gg]", f"-Xms{min(2, safe_max)}G", flags)
                config["flags"] = flags
            changed = True
            if noisy:
                err(
                    f"[MSX Compat] RAM corregida: {old} GB -> {safe_max} GB "
                    f"(Codespace ≈ {limit:.1f} GiB)."
                )

    # Ensure flags and the numeric setting do not drift apart.
    ram = config.get("gbs_de_ram_a_usar")
    flags = config.get("flags")
    if isinstance(ram, (int, float)) and isinstance(flags, str):
        ram_int = max(1, int(ram))
        new_flags = re.sub(r"-Xmx\d+[Gg]", f"-Xmx{ram_int}G", flags)
        new_flags = re.sub(r"-Xms\d+[Gg]", f"-Xms{min(2, ram_int)}G", new_flags)
        if new_flags != flags:
            config["flags"] = new_flags
            changed = True

    if changed:
        try:
            _save_config(config)
        except Exception as exc:
            _log(f"could not save config repair: {exc}")
    return config


def _selected_java_version(config: Optional[dict[str, Any]] = None) -> Optional[str]:
    config = config or _load_config()
    if not config:
        return None
    version = str(config.get("version_jdk", "")).strip()
    return version if version.isdigit() else None


def _java_path(version: str) -> Path:
    return Path(f"/usr/lib/jvm/java-{version}-openjdk-amd64/bin/java")


def _ensure_java(version: str) -> Optional[Path]:
    path = _java_path(version)
    if path.exists() and os.access(path, os.X_OK):
        return path
    if os.name == "nt":
        return None

    err(f"[MSX Compat] Java {version} no está disponible; instalándolo...")
    try:
        update = _run(_sudo() + ["apt-get", "update"], capture=True, timeout=180)
        if update.returncode != 0:
            _log(f"apt update failed: {(update.stdout or '')[-1500:]}")
        install = _run(
            _sudo() + ["apt-get", "install", "-y", f"openjdk-{version}-jre-headless"],
            capture=True,
            timeout=300,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        if install.returncode != 0:
            err(f"[MSX Compat] No se pudo instalar Java {version}.")
            _log((install.stdout or "")[-3000:])
            return None
    except Exception as exc:
        err(f"[MSX Compat] Error instalando Java {version}: {exc}")
        return None
    return path if path.exists() else None


def _forge_unix_args() -> Optional[Path]:
    base = SERVER_DIR / "libraries" / "net" / "minecraftforge" / "forge"
    if not base.exists():
        return None
    files = list(base.glob("*/unix_args.txt"))
    if not files:
        return None
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def _find_forge_installer() -> Optional[Path]:
    preferred = SERVER_DIR / "forge.jar"
    if preferred.exists():
        return preferred
    patterns = ("*forge*installer*.jar", "forge-*.jar")
    for pattern in patterns:
        candidates = [p for p in SERVER_DIR.glob(pattern) if p.is_file()]
        if candidates:
            return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    return None


def _patch_user_jvm_args(config: dict[str, Any]) -> None:
    path = SERVER_DIR / "user_jvm_args.txt"
    if not path.exists():
        return
    try:
        ram = int(config.get("gbs_de_ram_a_usar", 4))
    except Exception:
        ram = 4
    ram = max(2, ram)
    text = path.read_text(encoding="utf-8", errors="replace")

    # Forge's generated file is often comments only. Add explicit values at the end. If our
    # previous managed block exists, replace it instead of accumulating duplicates.
    begin = "# --- MSX COMPAT MANAGED RAM ---"
    end = "# --- END MSX COMPAT MANAGED RAM ---"
    managed = f"{begin}\n-Xms{min(2, ram)}G\n-Xmx{ram}G\n{end}"
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
    if pattern.search(text):
        new = pattern.sub(managed, text)
    else:
        new = text.rstrip() + "\n\n" + managed + "\n"
    if new != text:
        path.write_text(new, encoding="utf-8")


def _patch_forge_run_sh(config: Optional[dict[str, Any]] = None) -> None:
    run_sh = SERVER_DIR / "run.sh"
    config = config or _load_config()
    if not run_sh.exists() or not config:
        return
    java_version = _selected_java_version(config)
    if not java_version:
        return
    java = _java_path(java_version)
    if not java.exists():
        return
    try:
        text = run_sh.read_text(encoding="utf-8", errors="replace")
        # Generated Forge scripts invoke a bare `java`. Pin it so Codespaces' Java 25 can
        # never replace Java 17 when the user later runs ./run.sh manually.
        new = re.sub(r"(?m)^\s*java\s+", f"{java} ", text, count=1)
        if new != text:
            backup = run_sh.with_name("run.sh.msx-backup")
            if not backup.exists():
                shutil.copy2(run_sh, backup)
            run_sh.write_text(new, encoding="utf-8")
            run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR)
            _log(f"run.sh pinned to Java {java_version}")
        _patch_user_jvm_args(config)
    except Exception as exc:
        _log(f"run.sh repair failed: {exc}")


def _repair_forge_before_msx_start(*, noisy: bool = True) -> bool:
    """Make an incomplete MSX Forge install startable BEFORE MSX calls iniciar_servidor().

    MSX calls conectar_tunel() immediately before iniciar_servidor(). Our Playit compatibility
    shim runs during conectar_tunel(), so this is the safest place to finish a failed
    `forge.jar --installServer` operation. The legacy `playit tunnels list` call waits on the
    same lock, so MSX cannot receive its IP and proceed until this repair is complete.
    """
    config = _preflight_config(noisy=noisy)
    if not config:
        return True
    if str(config.get("server_type", "")).lower() != "forge":
        return True
    if not SERVER_DIR.exists():
        return True

    args_file = _forge_unix_args()
    if args_file:
        _patch_forge_run_sh(config)
        return True

    installer = _find_forge_installer()
    if not installer:
        # During configuration MSX may not have downloaded it yet. Do not invent a download URL;
        # leave installation to MSX and log enough to diagnose if startup is attempted too early.
        _log("Forge repair skipped: installer jar not found")
        return True

    java_version = _selected_java_version(config) or "17"
    java = _ensure_java(java_version)
    if not java:
        err(f"[MSX Compat] Forge necesita Java {java_version}, pero no pude prepararlo.")
        return False

    if noisy:
        err(
            f"[MSX Compat] MSX dejó Forge sin terminar. Completando instalación con Java {java_version}..."
        )

    COMPAT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with FORGE_INSTALL_LOG.open("w", encoding="utf-8") as log:
            process = subprocess.run(
                [str(java), "-jar", str(installer.name), "--installServer"],
                cwd=str(SERVER_DIR),
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=600,
            )
    except subprocess.TimeoutExpired:
        err("[MSX Compat] La instalación de Forge excedió 10 minutos y fue detenida.")
        return False
    except Exception as exc:
        err(f"[MSX Compat] Error ejecutando el instalador de Forge: {exc}")
        return False

    if process.returncode != 0 or not _forge_unix_args():
        tail = ""
        try:
            lines = FORGE_INSTALL_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-18:])
        except Exception:
            pass
        err("[MSX Compat] Forge no terminó de instalar correctamente.")
        if tail:
            err("[MSX Compat] Últimas líneas del instalador:\n" + tail)
        return False

    _patch_forge_run_sh(config)
    if noisy:
        err("[MSX Compat] Forge quedó instalado y listo para que MSX lo inicie.")
    return True


def _compat_watcher(stop: threading.Event) -> None:
    # Configuration is created/changed while the interactive .msx binary is already running.
    # Keep RAM and generated Forge files corrected as soon as they appear.
    while not stop.wait(0.35):
        try:
            config = _preflight_config(noisy=False)
            if config:
                _patch_forge_run_sh(config)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Playit modern compatibility
# ---------------------------------------------------------------------------


def _find_real_playit() -> Optional[str]:
    candidates = [
        os.environ.get("MSX_REAL_PLAYIT"),
        "/usr/bin/playit",
        "/usr/local/bin/playit",
        "/opt/playit/playit",
    ]
    for item in candidates:
        if not item:
            continue
        path = Path(item)
        try:
            if path.exists() and os.access(path, os.X_OK):
                return str(path.resolve())
        except OSError:
            continue
    return None


def _install_playit() -> Optional[str]:
    if os.name == "nt":
        return None
    err("[MSX Compat] Instalando el agente oficial de Playit...")
    try:
        # Ensure the tools needed by the official Playit apt repository exist.
        p = _run(
            _sudo() + ["apt-get", "update"],
            capture=True,
            timeout=180,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        if p.returncode != 0:
            _log("apt update before Playit returned nonzero")
        p = _run(
            _sudo() + ["apt-get", "install", "-y", "curl", "gnupg"],
            capture=True,
            timeout=180,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        if p.returncode != 0:
            _log((p.stdout or "")[-2000:])

        # Piped repository setup is simplest here, but keep output in the diagnostic log.
        commands = [
            "curl -SsL https://playit-cloud.github.io/ppa/key.gpg | "
            "gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/playit.gpg >/dev/null",
            'echo "deb [signed-by=/etc/apt/trusted.gpg.d/playit.gpg] '
            'https://playit-cloud.github.io/ppa/data ./" | '
            "sudo tee /etc/apt/sources.list.d/playit-cloud.list >/dev/null",
            "sudo apt-get update",
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y playit",
        ]
        for command in commands:
            result = subprocess.run(
                ["bash", "-lc", command],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=300,
            )
            if result.returncode != 0:
                _log((result.stdout or "")[-3000:])
                err(f"[MSX Compat] Falló la instalación de Playit (código {result.returncode}).")
                return None
    except Exception as exc:
        err(f"[MSX Compat] Error instalando Playit: {exc}")
        return None
    return _find_real_playit()


def _find_playitd() -> Optional[str]:
    for candidate in PLAYIT_DAEMON_CANDIDATES:
        if Path(candidate).exists() and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("playitd")


def _playit_status(real: str) -> tuple[int, str]:
    try:
        result = _run(_sudo() + [real, "status"], capture=True, timeout=12)
        return result.returncode, result.stdout or ""
    except Exception as exc:
        return 1, str(exc)


def _playit_secret_configured(text: str) -> bool:
    return bool(re.search(r"Secret\s+configured:\s*true", text, re.I))


def _ensure_playit_daemon(real: str) -> bool:
    if Path(PLAYIT_SOCKET).exists():
        rc, text = _playit_status(real)
        if rc == 0 and ("phase: running" in text.lower() or _playit_secret_configured(text)):
            return True

    daemon = _find_playitd()
    if not daemon:
        err("[MSX Compat] Playit está instalado, pero no encuentro playitd.")
        return False

    try:
        _run(_sudo() + ["rm", "-f", PLAYIT_SOCKET], capture=True, timeout=5)
        prep = _run(
            _sudo() + ["mkdir", "-p", "/run/playit", "/var/log/playit", "/etc/playit"],
            capture=True,
            timeout=10,
        )
        if prep.returncode != 0:
            err("[MSX Compat] No pude preparar /run/playit para Codespaces.")
            return False

        # No systemctl: launch the exact daemon manually and detach it from MSX's terminal.
        command = (
            f"nohup {shlex.quote(daemon)} --secret-path {shlex.quote(PLAYIT_SECRET)} "
            f"--socket-path {shlex.quote(PLAYIT_SOCKET)} -l {shlex.quote(PLAYIT_DAEMON_LOG)} "
            ">/tmp/msx-playitd.out 2>&1 </dev/null &"
        )
        result = _run(_sudo() + ["sh", "-c", command], capture=True, timeout=10)
        if result.returncode != 0:
            err(f"[MSX Compat] No pude iniciar playitd: {(result.stdout or '').strip()}")
            return False
    except Exception as exc:
        err(f"[MSX Compat] Error iniciando playitd: {exc}")
        return False

    for _ in range(80):
        if Path(PLAYIT_SOCKET).exists():
            rc, _ = _playit_status(real)
            if rc == 0:
                _log("playitd IPC ready")
                return True
        time.sleep(0.25)
    err("[MSX Compat] playitd arrancó, pero no apareció su socket IPC.")
    return False


def _setup_playit_if_needed(real: str) -> bool:
    rc, text = _playit_status(real)
    if rc == 0 and _playit_secret_configured(text):
        return True

    err("\n[MSX Compat] Este Codespace todavía no está vinculado con Playit.")
    err("[MSX Compat] Abre el enlace de reclamación que aparecerá. No descargues nada en tu PC.\n")
    try:
        result = _run(_sudo() + [real, "setup"])
    except Exception as exc:
        err(f"[MSX Compat] Error ejecutando playit setup: {exc}")
        return False
    if result.returncode != 0:
        err(f"[MSX Compat] playit setup terminó con código {result.returncode}.")
        return False

    for _ in range(80):
        rc, text = _playit_status(real)
        if rc == 0 and _playit_secret_configured(text):
            return True
        time.sleep(0.5)
    return False


def _read_playit_secret() -> Optional[str]:
    try:
        result = _run(_sudo() + ["cat", PLAYIT_SECRET], capture=True, timeout=5)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    content = (result.stdout or "").strip()
    if not content:
        return None
    if re.fullmatch(r"[0-9a-fA-F]+", content):
        return content
    match = re.search(r'^\s*secret_key\s*=\s*["\']([0-9a-fA-F]+)["\']\s*$', content, re.M)
    return match.group(1) if match else None


def _copy_legacy_playit_marker() -> None:
    # Legacy MSX checks this old path before deciding whether Playit was configured.
    targets = [Path.home() / ".config" / "playit_gg" / "playit.toml"]
    fixed = Path("/home/codespace/.config/playit_gg/playit.toml")
    if fixed not in targets and fixed.parent.parent.exists():
        targets.append(fixed)
    user = getpass.getuser()
    for target in targets:
        try:
            _run(_sudo() + ["mkdir", "-p", str(target.parent)], capture=True, timeout=5)
            copied = _run(_sudo() + ["cp", PLAYIT_SECRET, str(target)], capture=True, timeout=5)
            if copied.returncode != 0:
                continue
            _run(_sudo() + ["chmod", "600", str(target)], capture=True, timeout=5)
            if str(target).startswith(str(Path.home())):
                _run(_sudo() + ["chown", f"{user}:{user}", str(target)], capture=True, timeout=5)
        except Exception:
            continue


def _playit_api(secret: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        PLAYIT_API + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Agent-Key {secret.strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "MSX-Codespaces-Compat-v4/2026",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"No se pudo contactar Playit: {exc.reason}") from None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Playit devolvió JSON inválido: {raw[:500]}") from exc

    if isinstance(parsed, dict) and parsed.get("status") == "success":
        data = parsed.get("data")
        return data if isinstance(data, dict) else {"value": data}
    if isinstance(parsed, dict) and "status" not in parsed:
        return parsed
    if isinstance(parsed, dict):
        raise RuntimeError(
            f"Playit API {path}: status={parsed.get('status')}, data={parsed.get('data')!r}"
        )
    raise RuntimeError(f"Respuesta inesperada de Playit: {str(parsed)[:500]}")


def _playit_rundata(secret: str) -> dict[str, Any]:
    return _playit_api(secret, "/v1/agents/rundata", {})


def _choose_tunnel(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    tunnels = data.get("tunnels")
    if not isinstance(tunnels, list) or not tunnels:
        return None
    for tunnel in tunnels:
        if (
            isinstance(tunnel, dict)
            and tunnel.get("tunnel_type") == "minecraft-java"
            and not tunnel.get("disabled_reason")
        ):
            return tunnel
    for tunnel in tunnels:
        if isinstance(tunnel, dict) and not tunnel.get("disabled_reason"):
            return tunnel
    return tunnels[0] if isinstance(tunnels[0], dict) else None


def _create_minecraft_tunnel(secret: str, agent_id: str) -> bool:
    payload = {
        "name": "MSX Minecraft",
        "tunnel_type": "minecraft-java",
        "port_type": "tcp",
        "port_count": 1,
        "origin": {
            "type": "agent",
            "data": {
                "agent_id": agent_id,
                "local_ip": "127.0.0.1",
                "local_port": LOCAL_MINECRAFT_PORT,
            },
        },
        "enabled": True,
        "alloc": None,
        "firewall_id": None,
        "proxy_protocol": None,
    }
    try:
        _playit_api(secret, "/tunnels/create", payload)
        err(
            f"[MSX Compat] Túnel Minecraft Java creado → 127.0.0.1:{LOCAL_MINECRAFT_PORT}."
        )
        return True
    except Exception as exc:
        detail = str(exc)
        _log(f"automatic tunnel creation failed: {detail}")
        if "RequiresVerifiedAccount" in detail or "EmailMustBeVerified" in detail:
            err("[MSX Compat] Playit exige verificar el correo antes de crear un túnel.")
        else:
            err(f"[MSX Compat] No pude crear el túnel automáticamente: {detail}")
        return False


def _ensure_tunnel(secret: str, *, allow_create: bool) -> Optional[str]:
    try:
        data = _playit_rundata(secret)
    except Exception as exc:
        err(f"[MSX Compat] No pude consultar los túneles de Playit: {exc}")
        return None

    tunnel = _choose_tunnel(data)
    if tunnel:
        address = tunnel.get("display_address")
        if isinstance(address, str) and address.strip():
            return address.strip()

    pending = data.get("pending")
    if isinstance(pending, list) and pending:
        for _ in range(25):
            time.sleep(1)
            try:
                data = _playit_rundata(secret)
                tunnel = _choose_tunnel(data)
                if tunnel and isinstance(tunnel.get("display_address"), str):
                    return tunnel["display_address"].strip()
                if not data.get("pending"):
                    break
            except Exception:
                break

    if allow_create:
        agent_id = data.get("agent_id")
        if isinstance(agent_id, str) and agent_id and _create_minecraft_tunnel(secret, agent_id):
            for _ in range(35):
                time.sleep(1)
                try:
                    data = _playit_rundata(secret)
                    tunnel = _choose_tunnel(data)
                    if tunnel and isinstance(tunnel.get("display_address"), str):
                        return tunnel["display_address"].strip()
                except Exception:
                    pass
    return None


def _legacy_tunnel_json(address: str) -> dict[str, Any]:
    # Exact path expected by legacy modules/tuneles.py:
    # tunnels[0].alloc.data.assigned_domain
    return {
        "tunnels": [
            {
                "alloc": {
                    "status": "allocated",
                    "data": {"assigned_domain": address},
                }
            }
        ]
    }


def _ensure_playit_ready() -> tuple[Optional[str], Optional[str]]:
    real = _find_real_playit() or _install_playit()
    if not real:
        return None, None
    if not _ensure_playit_daemon(real):
        return real, None
    if not _setup_playit_if_needed(real):
        return real, None
    _copy_legacy_playit_marker()
    return real, _read_playit_secret()


# ---------------------------------------------------------------------------
# The one-file Playit shim mode
# ---------------------------------------------------------------------------


def _playit_shim_auto() -> int:
    with _exclusive_lock():
        real, secret = _ensure_playit_ready()
        if not real or not secret:
            err(f"[MSX Compat] Playit no quedó listo. Revisa {LOG_FILE}.")
            return 1

        # Critical v4 retained fix: complete Forge BEFORE legacy conectar_tunel() is allowed to
        # return. MSX calls iniciar_servidor() immediately afterwards.
        if not _repair_forge_before_msx_start(noisy=True):
            return 1

        address = _ensure_tunnel(secret, allow_create=True)
        if address:
            print(f"MSX Playit listo: {address}", flush=True)
            _log(f"Playit ready; tunnel={address}")
        else:
            print(f"MSX Playit listo. Revisa {PLAYIT_DASHBOARD}", flush=True)
        return 0


def _playit_shim_tunnels_list() -> int:
    # stdout MUST be JSON only: legacy MSX redirects stdout to playit.tunnels and json.load()s it.
    with _exclusive_lock():
        real, secret = _ensure_playit_ready()
        address: Optional[str] = None
        if real and secret:
            # Run the repair again in case the old foreground/background Playit sequence differs.
            _repair_forge_before_msx_start(noisy=False)
            address = _ensure_tunnel(secret, allow_create=True)
        if not address:
            address = PLAYIT_DASHBOARD
            err("[MSX Compat] No pude obtener una dirección pública de Playit.")
        json.dump(_legacy_tunnel_json(address), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return 0


def _delegate_real_playit(args: list[str]) -> int:
    real = _find_real_playit()
    if not real:
        if args in (["--version"], ["version"], ["-V"]):
            print(f"MSX Playit compatibility bridge v{COMPAT_VERSION}")
            return 0
        real = _install_playit()
        if not real:
            return 127
    service_commands = {"status", "setup", "start", "stop", "reset", "attach", "account"}
    command = _sudo() + [real] + args if args and args[0] in service_commands else [real] + args
    try:
        return subprocess.call(command)
    except KeyboardInterrupt:
        return 130


def _playit_shim_main(args: list[str]) -> int:
    _log(f"shim invoked args={args!r}")
    if not args:
        return _playit_shim_auto()
    if len(args) >= 2 and args[0] == "tunnels" and args[1] == "list":
        return _playit_shim_tunnels_list()
    return _delegate_real_playit(args)


# ---------------------------------------------------------------------------
# Dynamic Java and legacy cleanup shims
# ---------------------------------------------------------------------------


def _rewrite_java_memory_args(args: list[str], config: Optional[dict[str, Any]]) -> list[str]:
    """Clamp stale -Xmx/-Xms flags even if legacy MSX built its command before config repair."""
    if not config:
        return args
    try:
        ram = max(2, int(config.get("gbs_de_ram_a_usar", 4)))
    except Exception:
        ram = 4
    xms = min(2, ram)
    out: list[str] = []
    for arg in args:
        if re.fullmatch(r"-Xmx\d+[Gg]", arg):
            out.append(f"-Xmx{ram}G")
        elif re.fullmatch(r"-Xms\d+[Gg]", arg):
            out.append(f"-Xms{xms}G")
        else:
            out.append(arg)
    return out


def _fallback_system_java() -> Optional[Path]:
    candidates = [Path("/usr/bin/java"), Path("/bin/java")]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        if resolved.exists() and os.access(resolved, os.X_OK) and resolved != JAVA_SHIM:
            return resolved
    return None


def _java_shim_main(args: list[str]) -> int:
    # This is the key v4 fix. The legacy MSX process is launched BEFORE the user selects
    # Java, so changing PATH/JAVA_HOME in the parent later cannot affect it. Intercept each
    # bare `java` call and choose the configured JDK at the moment Java is actually invoked.
    config = _preflight_config(noisy=False)
    version = _selected_java_version(config)
    java: Optional[Path] = None
    if version:
        java = _ensure_java(version)
    if java is None:
        java = _fallback_system_java()
    if java is None:
        err("[MSX Compat] No encuentro un ejecutable Java válido.")
        return 127

    rewritten = _rewrite_java_memory_args(args, config)
    serverish = any("unix_args.txt" in a or a == "nogui" for a in rewritten)
    if serverish and config:
        try:
            ram = int(config.get("gbs_de_ram_a_usar", 0))
        except Exception:
            ram = 0
        info(f"[MSX Compat] Iniciando servidor con Java {version or '?'}" + (f" / {ram} GB RAM" if ram else "") + "...")

    _log(f"java shim -> {java} args={' '.join(shlex.quote(a) for a in rewritten[:12])}")
    try:
        rc = subprocess.call([str(java), *rewritten])
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        err(f"[MSX Compat] No pude ejecutar {java}: {exc}")
        return 126

    if serverish and rc != 0:
        err(f"[MSX Compat] Minecraft/Forge terminó con código {rc}.")
        latest = SERVER_DIR / "logs" / "latest.log"
        if latest.exists():
            try:
                lines = latest.read_text(encoding="utf-8", errors="replace").splitlines()
                tail = "\n".join(lines[-28:])
                if tail:
                    err("[MSX Compat] Últimas líneas de logs/latest.log:\n" + tail)
            except Exception as exc:
                _log(f"could not read latest.log after java failure: {exc}")
    return rc


def _real_process_tool(tool: str) -> Optional[str]:
    for path in (f"/usr/bin/{tool}", f"/bin/{tool}"):
        if Path(path).exists() and os.access(path, os.X_OK):
            return path
    return None


def _kill_shim_main(tool: str, args: list[str]) -> int:
    # Legacy MSX tries `pkill playit` after the Minecraft process exits. Modern playitd was
    # started with sudo so the unprivileged legacy process cannot kill it, which produced the
    # scary "Operation not permitted" message. More importantly, playitd should stay alive and
    # reusable for the next server start. Only suppress Playit cleanup; delegate everything else.
    joined = " ".join(args).lower()
    if "playit" in joined:
        _log(f"suppressed legacy {tool} cleanup: {args!r}")
        return 0
    real = _real_process_tool(tool)
    if not real:
        return 127
    try:
        os.execv(real, [real, *args])
    except Exception as exc:
        _log(f"could not delegate {tool}: {exc}")
        return 126


# ---------------------------------------------------------------------------
# Launcher setup / self-tests
# ---------------------------------------------------------------------------


def _write_exec_shim(path: Path, hidden_mode: str) -> None:
    script = (
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        f" {hidden_mode} \"$@\"\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _install_runtime_shims() -> None:
    if os.name == "nt":
        return
    SHIM_DIR.mkdir(parents=True, exist_ok=True)
    # All wrappers re-enter THIS SAME file, keeping the user-facing distribution one-file.
    _write_exec_shim(PLAYIT_SHIM, "--_msx-playit-shim")
    _write_exec_shim(JAVA_SHIM, "--_msx-java-shim")
    _write_exec_shim(PKILL_SHIM, "--_msx-kill-shim pkill")
    _write_exec_shim(KILLALL_SHIM, "--_msx-kill-shim killall")


def _launcher_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["MSX_COMPAT_ROOT"] = str(ROOT)
    env["PATH"] = str(SHIM_DIR) + os.pathsep + env.get("PATH", "")

    # If a config already exists, also make its selected Java the inherited default. MSX itself
    # usually uses an absolute path, but this protects helper scripts and manual shell commands.
    config = _load_config()
    version = _selected_java_version(config)
    if version:
        java = _java_path(version)
        if java.exists():
            # Keep the compatibility shim FIRST in PATH. The shim reads configuracion.json
            # on every invocation, which matters because MSX creates/changes the Java version
            # after the legacy binary has already started.
            env["JAVA_HOME"] = str(java.parent.parent)
    return env


def _launch_msx(binary: Path) -> int:
    _preflight_config(noisy=True)
    _patch_forge_run_sh()

    if os.name == "nt":
        if binary.suffix.lower() == ".exe":
            return subprocess.call([str(binary)], cwd=ROOT)
        return subprocess.call([sys.executable, str(binary)], cwd=ROOT)

    _install_runtime_shims()
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    env = _launcher_environment()
    info(f"[MSX Compat v{COMPAT_VERSION}] Compatibilidad Codespaces + Playit + Forge activada.")

    stop = threading.Event()
    watcher = threading.Thread(target=_compat_watcher, args=(stop,), daemon=True)
    watcher.start()
    try:
        return subprocess.call([str(binary)], cwd=ROOT, env=env)
    finally:
        stop.set()
        watcher.join(timeout=1.5)
        _preflight_config(noisy=False)
        _patch_forge_run_sh()


def _self_test() -> int:
    sample = {
        "agent_id": "00000000-0000-0000-0000-000000000000",
        "tunnels": [
            {
                "tunnel_type": "minecraft-java",
                "display_address": "example.tun.ply.gg",
                "disabled_reason": None,
            }
        ],
        "pending": [],
    }
    assert _choose_tunnel(sample)["display_address"] == "example.tun.ply.gg"  # type: ignore[index]
    legacy = _legacy_tunnel_json("example.tun.ply.gg")
    assert legacy["tunnels"][0]["alloc"]["data"]["assigned_domain"] == "example.tun.ply.gg"
    assert _playit_secret_configured("Secret configured: true")
    assert _safe_ram_gib(15.0) == 10
    assert _rewrite_java_memory_args(["-Xms16G", "-Xmx16G", "-jar", "x.jar"], {"gbs_de_ram_a_usar": 10})[:2] == ["-Xms2G", "-Xmx10G"]
    print("MSX Compat v4 self-test: OK")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--_msx-playit-shim":
        return _playit_shim_main(args[1:])
    if args and args[0] == "--_msx-java-shim":
        return _java_shim_main(args[1:])
    if len(args) >= 2 and args[0] == "--_msx-kill-shim":
        return _kill_shim_main(args[1], args[2:])
    if args == ["--self-test"]:
        return _self_test()

    _write_gitignore()
    binary = _get_msx_binary()
    if not binary:
        err("[MSX Compat] No encontré MSX local y tampoco pude descargarlo.")
        return 1
    return _launch_msx(binary)


if __name__ == "__main__":
    raise SystemExit(main())
