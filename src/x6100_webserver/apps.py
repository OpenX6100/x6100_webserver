from datetime import datetime, timezone, timedelta
from importlib import resources
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import stat
import tempfile
import time
import zipfile
from urllib.parse import quote, urlparse

import bottle

from . import adif_parse
from . import ft8_state
from . import models
from . import settings

app = bottle.Bottle()

bottle.TEMPLATE_PATH += [
    resources.files('x6100_webserver').joinpath('views'),
]

STATIC_PATH = resources.files('x6100_webserver').joinpath('static')


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
_TXT_EXT = ".txt"


def _normalize_txt_for_save(text: str) -> bytes:
    """Normalize to ASCII (strip non-ASCII) and Linux line endings (\\n only)."""
    # Linux line endings: no \\r\\n or \\r
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    # ASCII only: replace non-ASCII with space
    s = "".join(c if ord(c) < 128 else " " for c in s)
    return s.encode("ascii")


def _url_quote_path(path: str) -> str:
    return quote(path or "", safe="/")


def _filebrowser_root() -> pathlib.Path:
    root = pathlib.Path(settings.FILEBROWSER_PATH or "/")
    try:
        return root.resolve()
    except Exception:
        return root


def _filebrowser_default_start() -> str:
    """Default start dir relative to root; empty shows root directly."""
    return settings.FILEBROWSER_DEFAULT_START or ""


def _resolve_filebrowser_path(filepath: str) -> pathlib.Path:
    rel = filepath.lstrip("/")
    root = _filebrowser_root()
    try:
        candidate = (root / rel).resolve()
    except Exception:
        candidate = root / rel

    # Prevent directory traversal when FILEBROWSER_PATH is not '/'.
    try:
        candidate.relative_to(root)
    except Exception:
        bottle.abort(403, "forbidden")

    return candidate


# Bands API

@app.get('/api/bands')
def get_bands(dbcon):
    bands = models.read_bands(dbcon)
    bottle.response.content_type = 'application/json'
    return json.dumps([x.asdict() for x in bands])


@app.put('/api/bands')
def add_band(dbcon):
    data = bottle.request.json
    try:
        band_param = models.BandParams(**data)
        models.add_band(dbcon, band_param)
        bottle.response.status = 201
        return {"status": "OK"}
    except ValueError as e:
        bottle.response.status = 400
        return {"status": "error", "msg": str(e)}


@app.post('/api/bands/<band_id:int>')
def update_band(band_id, dbcon):
    data = bottle.request.json
    try:
        band_param = models.BandParams(id=band_id, **data)
        models.update_band(dbcon, band_param)
        return {"status": "OK"}
    except ValueError as e:
        bottle.response.status = 400
        return {"status": "error", "msg": str(e)}


@app.delete('/api/bands/<band_id:int>')
def delete_band(band_id, dbcon):
    try:
        models.delete_band(dbcon, band_id)
        return {"status": "OK"}
    except ValueError as e:
        bottle.response.status = 400
        return {"status": "error", "msg": str(e)}


# Digital modes routes

@app.get('/api/digital_modes')
def get_digital_modes(dbcon):
    d_modes = models.read_digital_modes(dbcon)
    bottle.response.content_type = 'application/json'
    return json.dumps([x.asdict() for x in d_modes])


@app.put('/api/digital_modes')
def add_digital_mode(dbcon):
    data = bottle.request.json
    try:
        d_mode = models.DigitalMode(**data)
        models.add_digital_mode(dbcon, d_mode)
        bottle.response.status = 201
        return {"status": "OK"}
    except ValueError as e:
        bottle.response.status = 400
        return {"status": "error", "msg": str(e)}


@app.post('/api/digital_modes/<mode_id:int>')
def update_digital_mode(mode_id, dbcon):
    data = bottle.request.json
    try:
        d_mode = models.DigitalMode(id=mode_id, **data)
        models.update_digital_mode(dbcon, d_mode)
        return {"status": "OK"}
    except ValueError as e:
        bottle.response.status = 400
        return {"status": "error", "msg": str(e)}


@app.delete('/api/digital_modes/<mode_id:int>')
def delete_digital_mode(mode_id, dbcon):
    try:
        models.delete_digital_mode(dbcon, mode_id)
        return {"status": "OK"}
    except ValueError as e:
        bottle.response.status = 400
        return {"status": "error", "msg": str(e)}

# Main routes

@app.route('/static/<filepath:path>')
def server_static(filepath):
    return bottle.static_file(filepath, root=STATIC_PATH)


@app.route('/')
def home():
    return bottle.template('index')


@app.route('/bands')
def bands():
    return bottle.template('bands')


@app.route('/digital_modes')
def digital_modes():
    return bottle.template('digital_modes')


def _serve_filebrowser_file(root: pathlib.Path, path: pathlib.Path, *, download: bool):
    os.sync()
    response = bottle.static_file(
        str(path.relative_to(root)),
        root=str(root),
        download=download,
    )
    response.set_header("Cache-Control", "private, no-cache, no-store")
    return response


def _file_view_page(filepath: str, path: pathlib.Path):
    suffix = path.suffix.lower()
    quoted = _url_quote_path(filepath)
    if suffix in _IMAGE_EXTS:
        return bottle.template(
            'file_view',
            filepath=filepath,
            filepath_url=quoted,
            is_image=True,
            image_url=f"/files/{quoted}?raw",
            text_content="",
            truncated=False,
            is_txt_editable=False,
        )

    max_bytes = 512 * 1024
    truncated = False
    try:
        with path.open("rb") as f:
            data = f.read(max_bytes + 1)
        if len(data) > max_bytes:
            data = data[:max_bytes]
            truncated = True
        text_content = data.decode("utf-8", errors="replace")
    except Exception as e:
        text_content = f"[error reading file: {e}]"

    # Only allow editing .txt when not truncated (avoid partial overwrite).
    is_txt_editable = suffix == _TXT_EXT and not truncated
    return bottle.template(
        'file_view',
        filepath=filepath,
        filepath_url=quoted,
        is_image=False,
        image_url="",
        text_content=text_content,
        truncated=truncated,
        is_txt_editable=is_txt_editable,
    )


@app.route('/files')
@app.route('/files/')
@app.route('/files/<filepath:path>')
@app.route('/files/<filepath:path>/')
def files(filepath=""):
    root = _filebrowser_root()
    path = _resolve_filebrowser_path(filepath)
    # Default start: at root, redirect unless the user navigated up via ".."
    default_start = _filebrowser_default_start()
    if default_start and path == root:
        ref = bottle.request.get_header("Referer")
        ref_path = urlparse(ref).path if ref else ""
        # Referer /files/xxx/ means ".." to root — do not redirect away
        from_root = ref_path.startswith("/files/") and ref_path.rstrip("/").count("/") >= 2
        if not from_root:
            return bottle.redirect(f"/files/{_url_quote_path(default_start)}/")

    want_view = "view" in bottle.request.query
    want_raw = "raw" in bottle.request.query

    if path.is_file():
        if want_view:
            return _file_view_page(filepath, path)
        return _serve_filebrowser_file(root, path, download=not want_raw)

    if not path.exists():
        bottle.abort(404, "not found")
    if not path.is_dir():
        bottle.abort(404, "not found")

    try:
        rel_dir = path.relative_to(root)
    except Exception:
        rel_dir = pathlib.Path("")

    # Parent link: show ".." when not at root
    has_parent = str(rel_dir) not in ("", ".")
    parent_path = ""
    if has_parent:
        parent_rel = rel_dir.parent
        parent_path = "" if str(parent_rel) == "." else parent_rel.as_posix()

    dirs = []
    files_list = []
    try:
        for item in sorted(path.iterdir()):
            item_rel = item.relative_to(root).as_posix()
            entry = {"name": item.name, "path": item_rel, "path_url": _url_quote_path(item_rel)}
            if item.is_dir():
                dirs.append(entry)
            else:
                files_list.append(entry)
    except PermissionError:
        bottle.abort(403, "forbidden")

    return bottle.template(
        'files',
        has_parent=has_parent,
        parent_path=parent_path,
        parent_path_url=_url_quote_path(parent_path),
        dirs=dirs,
        files=files_list,
    )


@app.route('/raw/<filepath:path>')
def file_raw_redirect(filepath=""):
    return bottle.redirect(f"/files/{_url_quote_path(filepath)}?raw")


@app.route('/view/<filepath:path>')
def file_view_redirect(filepath=""):
    return bottle.redirect(f"/files/{_url_quote_path(filepath)}?view")


@app.post('/api/save_txt/<filepath:path>')
def save_txt(filepath=""):
    """Save .txt file: ASCII only, Linux line endings (\\n)."""
    path = _resolve_filebrowser_path(filepath)
    if not path.exists() or not path.is_file():
        bottle.abort(404, "not found")
    if path.suffix.lower() != _TXT_EXT:
        bottle.response.status = 400
        return {"status": "error", "msg": "only .txt files can be saved"}
    try:
        body = bottle.request.body.read()
        text = body.decode("utf-8", errors="replace")
    except Exception as e:
        bottle.response.status = 400
        return {"status": "error", "msg": f"invalid request body: {e}"}
    try:
        data = _normalize_txt_for_save(text)
        path.write_bytes(data)
    except PermissionError:
        bottle.response.status = 403
        return {"status": "error", "msg": "permission denied"}
    except Exception as e:
        bottle.response.status = 500
        return {"status": "error", "msg": str(e)}
    return {"status": "OK"}


# Remote control routes

@app.route('/remote')
def remote():
    return bottle.template('remote')


@app.route('/ft8')
def ft8_page():
    return bottle.template('ft8')


@app.get('/api/ft8/state')
def ft8_state_api():
    return ft8_state.to_dict(settings.FT8_STATE_PATH)


@app.post('/api/ft8/command')
def ft8_command():
    data = bottle.request.json or {}
    line, err = ft8_state.build_command_line(data.get("verb"), data.get("arg"))
    if err:
        bottle.response.status = 400
        return {"status": "error", "msg": err}
    return _write_remote_command(line)


@app.route('/dmesg')
def dmesg_view():
    lines = []
    error = ""
    try:
        # Keep it simple for BusyBox environments (no GNU-only flags).
        cp = subprocess.run(["dmesg"], capture_output=True, text=True)
        if cp.returncode != 0:
            error = (cp.stderr or "").strip() or f"dmesg exited with code {cp.returncode}"
        else:
            all_lines = (cp.stdout or "").splitlines()
            lines = all_lines[-200:]
    except Exception as e:
        error = str(e)

    return bottle.template('dmesg', lines=lines, error=error)


REMOTE_SCREEN_REQ_PATH = "/tmp/remote_screen.req"


def _serve_screen(jpg):
    resp = bottle.static_file(jpg.name, root=str(jpg.parent))
    resp.set_header("Content-Type", "image/jpeg")
    resp.set_header("Cache-Control", "no-store, no-cache, must-revalidate")
    resp.set_header("Pragma", "no-cache")
    return resp


@app.get('/api/remote_screen')
def remote_screen():
    # Async handshake via req mtime as the only shared token:
    # - no param: touch when idle, else join pending capture; always return 202 {time}
    # - ?time=: return JPEG when jpg.mtime > token, else 202 {time}
    jpg = pathlib.Path(settings.REMOTE_SCREEN_PATH)
    try:
        jpg_st = jpg.stat()
        jpg_mtime = jpg_st.st_mtime if jpg_st.st_size > 0 else 0.0
    except FileNotFoundError:
        jpg_mtime = 0.0

    time_arg = bottle.request.query.get('time')
    if time_arg:
        try:
            token = float(time_arg)
        except ValueError:
            bottle.response.status = 400
            return {"status": "error", "msg": "invalid time"}
        if token < jpg_mtime:
            return _serve_screen(jpg)
        bottle.response.status = 202
        return {"status": "pending", "time": token}

    # no param: trigger when idle (incl. first run), else join the pending capture
    req = pathlib.Path(REMOTE_SCREEN_REQ_PATH)
    try:
        req_mtime = req.stat().st_mtime
    except FileNotFoundError:
        req_mtime = 0.0
    if req_mtime <= jpg_mtime:
        try:
            req.touch()
            req_mtime = req.stat().st_mtime
        except Exception as e:
            bottle.response.status = 500
            return {"status": "error", "msg": str(e)}
    bottle.response.status = 202
    return {"status": "pending", "time": req_mtime}


def _write_remote_command(line):
    fifo_path = pathlib.Path(settings.REMOTE_INPUT_PATH)
    try:
        if fifo_path.exists() and not stat.S_ISFIFO(fifo_path.stat().st_mode):
            fifo_path.unlink()
        if not fifo_path.exists():
            os.mkfifo(fifo_path, 0o666)
    except Exception:
        pass

    try:
        fd = os.open(str(fifo_path), os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as e:
        bottle.response.status = 503
        return {"status": "error", "msg": f"remote control unavailable: {e}"}

    return {"status": "OK"}


@app.post('/api/remote_input')
def remote_input():
    data = bottle.request.json or {}
    cmd_type = (data.get("type") or "").lower()
    name = (data.get("name") or "").upper()

    if cmd_type == "key":
        action = (data.get("action") or "click").lower()
        return _write_remote_command(f"KEY {name} {action}")

    if cmd_type == "knob":
        delta = data.get("delta")
        if delta is None:
            bottle.response.status = 400
            return {"status": "error", "msg": "delta is required"}
        return _write_remote_command(f"KNOB {name} {delta}")

    if cmd_type == "knob_press":
        action = (data.get("action") or "click").lower()
        return _write_remote_command(f"KNOB_PRESS {name} {action}")

    bottle.response.status = 400
    return {"status": "error", "msg": "unknown type"}

# Timezone routes


@app.route('/time')
def time_editor():
    return bottle.template('time')


# Logbook routes


def _gui_service(action: str) -> None:
    """Run S95gui start|stop|restart. Runtime only; does not change boot autostart."""
    script = getattr(settings, "GUI_INIT_SCRIPT", "/etc/init.d/S95gui")
    try:
        cp = subprocess.run(
            [script, action],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        raise RuntimeError(f"GUI {action} failed: {e}") from e
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip() or f"exit {cp.returncode}"
        raise RuntimeError(f"GUI {action} failed: {detail}")


def _with_gui_stopped(op):
    """Stop GUI, run op(), always try to start GUI again."""
    _gui_service("stop")
    # Match S95gui restart() delay so handles are released.
    time.sleep(1)
    try:
        result = op()
    except Exception:
        try:
            _gui_service("start")
        except Exception:
            pass
        raise
    try:
        _gui_service("start")
    except Exception as e:
        raise RuntimeError(
            f"operation completed but GUI start failed: {e}"
        ) from e
    return result


@app.route('/logbook')
def logbook():
    return bottle.template('logbook')


@app.get('/api/logbook/adi')
def logbook_adi_list():
    path = pathlib.Path(settings.FT_LOG_ADI_PATH)
    bottle.response.content_type = 'application/json'
    if not path.is_file():
        return {"exists": False, "count": 0, "records": []}
    try:
        records = adif_parse.parse_adif_file(path)
        records = adif_parse.records_newest_first(records)
        return {"exists": True, "count": len(records), "records": records}
    except Exception as e:
        bottle.response.status = 500
        return {"status": "error", "msg": str(e)}


@app.get('/api/logbook/adi/download')
def logbook_adi_download():
    path = pathlib.Path(settings.FT_LOG_ADI_PATH)
    if not path.is_file():
        bottle.abort(404, "ft_log.adi not found")
    try:
        os.sync()
    except Exception:
        pass
    response = bottle.static_file(
        path.name, root=str(path.parent), download=True)
    response.set_header("Cache-Control", "no-store, no-cache, must-revalidate")
    response.set_header("Pragma", "no-cache")
    return response


@app.post('/api/logbook/adi/upload')
def logbook_adi_upload():
    upload = bottle.request.files.get("file")
    if upload is None:
        bottle.response.status = 400
        return {"status": "error", "msg": "file is required"}
    name = pathlib.Path(upload.filename or "").name
    if pathlib.Path(name).suffix.lower() != ".adi":
        bottle.response.status = 400
        return {"status": "error", "msg": "only .adi files are accepted"}
    dest = pathlib.Path(settings.INCOMING_LOG_ADI_PATH)

    def do_upload():
        dest.parent.mkdir(parents=True, exist_ok=True)
        upload.save(str(dest), overwrite=True)

    try:
        _with_gui_stopped(do_upload)
    except Exception as e:
        bottle.response.status = 500
        return {"status": "error", "msg": str(e)}
    return {
        "status": "OK",
        "msg": "Uploaded to /mnt/incoming_log.adi. GUI restarted to import into worked DB.",
    }


@app.delete('/api/logbook/adi')
def logbook_adi_delete():
    path = pathlib.Path(settings.FT_LOG_ADI_PATH)

    def do_delete():
        if path.is_file():
            path.unlink()

    try:
        _with_gui_stopped(do_delete)
    except Exception as e:
        bottle.response.status = 500
        return {"status": "error", "msg": str(e)}
    return {
        "status": "OK",
        "msg": "Local ADI deleted. GUI restarted.",
    }


@app.post('/api/logbook/qsolog/reset')
def logbook_qsolog_reset():
    path = pathlib.Path(settings.QSO_LOG_DB_PATH)

    def do_reset():
        if not path.is_file():
            return "missing"
        con = sqlite3.connect(str(path))
        try:
            con.execute("DELETE FROM qso_log")
            con.commit()
        finally:
            con.close()
        return "cleared"

    try:
        result = _with_gui_stopped(do_reset)
    except Exception as e:
        bottle.response.status = 500
        return {"status": "error", "msg": str(e)}
    if result == "missing":
        return {
            "status": "OK",
            "msg": "qsolog DB not present (already empty). GUI restarted.",
        }
    return {
        "status": "OK",
        "msg": "qsolog DB cleared. GUI restarted.",
    }


@app.get('/api/get_time')
def get_time():
    tz = timezone(timedelta())
    server_time = datetime.now(tz).isoformat()
    bottle.response.content_type = 'application/json'
    return {"server_time": server_time}


def update_time_by_ntp(server_address):
    ntp_args = ["ntpdate", "-u", server_address]
    p = subprocess.Popen(
        ntp_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _, errs = p.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill()
        _, errs = p.communicate()
        bottle.response.status = 500
        return {"status": "error", "msg": "NTP update timeout"}

    if p.returncode != 0:
        bottle.response.status = 500
        return {"status": "error", "msg": f"NTP update failed: {errs.decode()}"}

    return {"status": "success", "msg": "NTP update successful"}


@app.post('/api/update_time')
def update_time():
    data = bottle.request.json

    update_mode = data.get("update_mode")
    if not update_mode:
        bottle.response.status = 400
        return {"status": "error", "msg": "update_mode is required"}

    if update_mode == "ntp":
        server_address = data.get("server_address")
        return update_time_by_ntp(server_address)

    elif update_mode == "manual":
        manual_time = data.get("manual_time")
        if not manual_time:
            bottle.response.status = 400
            return {"status": "error", "msg": "manual_time is required"}

        try:
            # Update system time manually
            manual_time = datetime.strptime(manual_time, "%Y-%m-%d %H:%M:%S")
            subprocess.run(
                ["date", "-s", manual_time.strftime("%Y-%m-%d %H:%M:%S")], check=True)
            return {"status": "success", "msg": "Server time updated manually"}
        except Exception as e:
            bottle.response.status = 500
            return {"status": "error", "msg": f"Failed to set manual time: {str(e)}"}

    else:
        bottle.response.status = 400
        return {"status": "error", "msg": f"unknown update_mode: {update_mode}"}


@app.get('/api/get_timezone')
def get_timezone():
    """Get the current server timezone."""
    try:
        p = subprocess.run(["realpath", "/etc/localtime"],
                           stdout=subprocess.PIPE, check=True)
        timezone_path = p.stdout.decode().strip()
        tz_list = timezone_path.split("/posix/")
        if len(tz_list) < 2:
            tz_list = timezone_path.split("/zoneinfo/")
        tz = tz_list[-1]
        return {"timezone": tz}
    except Exception as e:
        bottle.response.status = 500
        return {"status": "error", "msg": f"Failed to fetch timezone: {str(e)}"}


@app.post('/api/set_timezone')
def set_timezone():
    """Set the server timezone."""
    data = bottle.request.json
    timezone = data.get("timezone")
    if not timezone:
        bottle.response.status = 400
        return {"status": "error", "msg": "Timezone is required"}

    target_tz = f"/usr/share/zoneinfo/{timezone}"
    if not os.path.exists(target_tz):
        bottle.response.status = 400
        return {"status": "error", "msg": f"Invalid timezone: {timezone}"}

    try:
        subprocess.run(["ln", "-sf", target_tz, "/etc/localtime"], check=True)
        return {"status": "success", "msg": "Timezone updated successfully"}
    except subprocess.CalledProcessError as e:
        bottle.response.status = 500
        return {"status": "error", "msg": f"Failed to set timezone: {str(e)}"}


# GUI binary OTA


_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class OtaValidationError(ValueError):
    """Client-facing OTA package validation failure."""


def _ota_max_bytes() -> int:
    return int(getattr(settings, "GUI_OTA_MAX_BYTES", 32 * 1024 * 1024))


def _ota_gui_bin_path() -> pathlib.Path:
    return pathlib.Path(getattr(settings, "GUI_BIN_PATH", "/usr/sbin/x6100_gui"))


def _fsync_file(path: pathlib.Path) -> None:
    with open(path, "rb") as f:
        os.fsync(f.fileno())


def _fsync_dir(path: pathlib.Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ota_extract_and_verify(
    zip_path: pathlib.Path, work_dir: pathlib.Path
) -> tuple[pathlib.Path, str]:
    """Extract the single zip member and verify name == content sha256.

    Returns (extracted_path, sha256_hex).
    """
    max_bytes = _ota_max_bytes()
    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except zipfile.BadZipFile as e:
        raise OtaValidationError("not a valid zip file") from e

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) != 1:
            raise OtaValidationError(
                f"zip must contain exactly one file (found {len(infos)})"
            )
        info = infos[0]
        name = info.filename
        if "/" in name or "\\" in name or name in (".", ".."):
            raise OtaValidationError("zip member name must be a bare filename")
        if not _SHA256_HEX_RE.fullmatch(name):
            raise OtaValidationError(
                "zip member name must be a 64-char lowercase sha256 hex digest"
            )
        if info.file_size > max_bytes:
            raise OtaValidationError(
                f"payload exceeds max size ({max_bytes} bytes)"
            )

        out = work_dir / name
        h = hashlib.sha256()
        written = 0
        with zf.open(info, "r") as src, open(out, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise OtaValidationError(
                        f"payload exceeds max size ({max_bytes} bytes)"
                    )
                h.update(chunk)
                dst.write(chunk)

    digest = h.hexdigest()
    if digest != name:
        raise OtaValidationError(
            "content sha256 does not match zip member filename"
        )
    return out, digest


def _ota_install_gui_bin(src: pathlib.Path) -> None:
    """Write src to GUI_BIN_PATH via temp file + os.replace; keep .bak copy."""
    dest = _ota_gui_bin_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    new_path = dest.with_name(dest.name + ".new")
    bak_path = dest.with_name(dest.name + ".bak")

    shutil.copyfile(src, new_path)
    os.chmod(new_path, 0o755)
    _fsync_file(new_path)

    if dest.is_file():
        shutil.copyfile(dest, bak_path)
        _fsync_file(bak_path)

    os.replace(new_path, dest)
    _fsync_dir(dest.parent)


def _ota_apply_gui_zip(upload) -> str:
    """Validate uploaded zip, install GUI binary. Returns sha256 hex.

    Raises OtaValidationError on bad package; other Exception on I/O failure.
    Does not restart GUI.
    """
    max_bytes = _ota_max_bytes()
    work_dir = pathlib.Path(tempfile.mkdtemp(prefix="gui-ota-"))
    zip_path = work_dir / "upload.zip"
    try:
        upload.save(str(zip_path), overwrite=True)
        zip_size = zip_path.stat().st_size
        if zip_size > max_bytes:
            raise OtaValidationError(
                f"upload exceeds max size ({max_bytes} bytes)"
            )
        if zip_size == 0:
            raise OtaValidationError("empty upload")
        extracted, digest = _ota_extract_and_verify(zip_path, work_dir)
        _ota_install_gui_bin(extracted)
        return digest
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.route("/ota")
def ota_page():
    return bottle.template("ota")


@app.post("/api/ota/gui")
def ota_gui_upload():
    upload = bottle.request.files.get("file")
    if upload is None:
        bottle.response.status = 400
        return {"status": "error", "msg": "file is required"}

    try:
        digest = _ota_apply_gui_zip(upload)
    except OtaValidationError as e:
        bottle.response.status = 400
        return {"status": "error", "msg": str(e)}
    except Exception as e:
        bottle.response.status = 500
        return {"status": "error", "msg": str(e)}

    try:
        _gui_service("restart")
    except Exception as e:
        bottle.response.status = 500
        return {
            "status": "error",
            "sha256": digest,
            "msg": f"GUI binary replaced but restart failed: {e}",
        }

    return {
        "status": "OK",
        "sha256": digest,
        "msg": "GUI updated and restarted.",
    }
