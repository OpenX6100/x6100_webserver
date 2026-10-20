from datetime import datetime, timezone, timedelta
from importlib import resources
import json
import os
import pathlib
import subprocess
from urllib.parse import quote_plus

import bottle

from . import models
from . import settings

app = bottle.Bottle()

bottle.TEMPLATE_PATH += [
    resources.files('x6100_webserver').joinpath('views'),
]

STATIC_PATH = resources.files('x6100_webserver').joinpath('static')

SSTV_PICS_PATH = pathlib.Path('/mnt/sstv/tx_pics')
SSTV_TEXT_PATH = pathlib.Path('/mnt/sstv/tx_text')
SSTV_MAX_PNG_SIZE = 500 * 1024
SSTV_MAX_PNG_WIDTH = 800
SSTV_MAX_PNG_HEIGHT = 600
SSTV_MAX_TEMPLATE_SIZE = 16 * 1024


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


@app.route('/files/')
@app.route('/files/<filepath:path>')
@app.route('/files/<filepath:path>/')
def files(filepath=""):
    path = pathlib.Path(settings.FILEBROWSER_PATH) / filepath
    if path.is_file():
        os.sync()
        response = bottle.static_file(str(path.relative_to(settings.FILEBROWSER_PATH)), root=settings.FILEBROWSER_PATH, download=True)
        response.set_header("Cache-Control", "private, no-cache, no-store")
        return response
    else:
        dirs = []
        files = []
        for item in sorted(path.iterdir()):
            if item.is_dir():
                dirs.append(item.relative_to(path))
            else:
                files.append(item.relative_to(path))
        return bottle.template('files', dirs=dirs, files=files)

# SSTV resources routes

def _sstv_safe_name(filename, suffix):
    if not filename:
        raise ValueError("Missing filename")
    name = pathlib.Path(filename).name
    if (name != filename or name in (".", "..") or name.startswith(".")
            or not name.lower().endswith(suffix)):
        raise ValueError(f"Only {suffix} files are allowed")
    return name


def _sstv_read_upload(upload, max_size):
    data = upload.file.read(max_size + 1)
    if len(data) > max_size:
        raise ValueError("File is too large")
    return data


def _sstv_write_file(directory, filename, data):
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / (filename + ".upload")
    target = directory / filename
    try:
        with tmp.open("wb") as fp:
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        tmp.replace(target)
        os.sync()
    finally:
        if tmp.exists():
            tmp.unlink()


@app.get('/sstv')
def sstv():
    SSTV_PICS_PATH.mkdir(parents=True, exist_ok=True)
    SSTV_TEXT_PATH.mkdir(parents=True, exist_ok=True)
    pics = sorted((p.name for p in SSTV_PICS_PATH.iterdir()
                   if p.is_file() and not p.name.startswith('.')
                   and p.suffix.lower() == '.png'), key=str.lower)
    templates = sorted((p.name for p in SSTV_TEXT_PATH.iterdir()
                        if p.is_file() and not p.name.startswith('.')
                        and p.name.lower().endswith('.sstvtxt')), key=str.lower)
    return bottle.template('sstv', pics=pics, templates=templates,
                           status=bottle.request.query.get('status', ''),
                           error=bottle.request.query.get('error', ''))



@app.get('/sstv/image/<filename:path>')
def sstv_image(filename):
    try:
        name = _sstv_safe_name(filename, '.png')
    except ValueError:
        bottle.abort(404)
    return bottle.static_file(name, root=str(SSTV_PICS_PATH))


@app.post('/sstv/upload/image')
def sstv_upload_image():
    upload = bottle.request.files.get('file')
    try:
        if upload is None:
            raise ValueError("Choose a PNG file first")
        name = _sstv_safe_name(upload.filename, '.png')
        data = _sstv_read_upload(upload, SSTV_MAX_PNG_SIZE)
        if (len(data) < 24 or data[:8] != b'\x89PNG\r\n\x1a\n'
                or data[12:16] != b'IHDR'):
            raise ValueError("The selected file is not a valid PNG")
        width = int.from_bytes(data[16:20], 'big')
        height = int.from_bytes(data[20:24], 'big')
        if width < 1 or height < 1:
            raise ValueError("The PNG has invalid dimensions")
        if width > SSTV_MAX_PNG_WIDTH or height > SSTV_MAX_PNG_HEIGHT:
            raise ValueError("Maximum image dimensions are 800x600 pixels")
        if width < height:
            raise ValueError("The image must use landscape orientation")
        _sstv_write_file(SSTV_PICS_PATH, name, data)
        bottle.redirect('/sstv?status=Image+uploaded')
    except (ValueError, OSError) as e:
        bottle.redirect('/sstv?error=' + quote_plus(str(e)))


@app.post('/sstv/create/template')
def sstv_create_template():
    try:
        template_name = (bottle.request.forms.get('template_name') or '').strip()
        middle = (bottle.request.forms.get('middle') or '').strip()
        bottom = (bottle.request.forms.get('bottom') or '').strip()

        if not template_name:
            raise ValueError("Enter a template name")
        if template_name.lower().endswith('.sstvtxt'):
            template_name = template_name[:-8]
        if not template_name:
            raise ValueError("Enter a template name")
        if pathlib.Path(template_name).name != template_name or template_name in ('.', '..'):
            raise ValueError("Invalid template name")
        if any(c in template_name for c in ('/', '\\', '\x00')):
            raise ValueError("Invalid template name")

        filename = template_name + '.sstvtxt'
        filename = _sstv_safe_name(filename, '.sstvtxt')
        if (SSTV_TEXT_PATH / filename).exists():
            raise ValueError("A template with this name already exists")

        # The firmware appends the configured local callsign to 'bottom'.
        # Keep exactly one separating space when the user entered bottom text.
        if bottom:
            bottom += ' '

        obj = {"middle": middle, "bottom": bottom}
        data = (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        if len(data) > SSTV_MAX_TEMPLATE_SIZE:
            raise ValueError("Template is too large")
        _sstv_write_file(SSTV_TEXT_PATH, filename, data)
        bottle.redirect('/sstv?status=Template+created')
    except (ValueError, OSError) as e:
        bottle.redirect('/sstv?error=' + quote_plus(str(e)))


@app.post('/sstv/upload/template')
def sstv_upload_template():
    upload = bottle.request.files.get('file')
    try:
        if upload is None:
            raise ValueError("Choose an SSTVTXT file first")
        name = _sstv_safe_name(upload.filename, '.sstvtxt')
        data = _sstv_read_upload(upload, SSTV_MAX_TEMPLATE_SIZE)
        try:
            obj = json.loads(data.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("The template is not valid UTF-8 JSON")
        if not isinstance(obj, dict):
            raise ValueError("The template must contain a JSON object")
        for key in ('middle', 'bottom'):
            if key in obj and obj[key] is not None and not isinstance(obj[key], str):
                raise ValueError(f'"{key}" must be a string')
        _sstv_write_file(SSTV_TEXT_PATH, name, data)
        bottle.redirect('/sstv?status=Template+uploaded')
    except (ValueError, OSError) as e:
        bottle.redirect('/sstv?error=' + quote_plus(str(e)))


def _sstv_delete(directory, filename, suffix):
    name = _sstv_safe_name(filename, suffix)
    path = directory / name
    if not path.is_file():
        raise ValueError("File not found")
    path.unlink()
    os.sync()


@app.post('/sstv/delete/image')
def sstv_delete_image():
    try:
        _sstv_delete(SSTV_PICS_PATH, bottle.request.forms.get('filename'), '.png')
        bottle.redirect('/sstv?status=Image+deleted')
    except (ValueError, OSError) as e:
        bottle.redirect('/sstv?error=' + quote_plus(str(e)))


@app.post('/sstv/delete/template')
def sstv_delete_template():
    try:
        _sstv_delete(SSTV_TEXT_PATH, bottle.request.forms.get('filename'), '.sstvtxt')
        bottle.redirect('/sstv?status=Template+deleted')
    except (ValueError, OSError) as e:
        bottle.redirect('/sstv?error=' + quote_plus(str(e)))


# Timezone routes


@app.route('/time')
def time_editor():
    return bottle.template('time')


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
