# File browser root; empty or "/" allows browsing from /, including ".." up to /
FILEBROWSER_PATH = ""
# Default start dir relative to root (e.g. "mnt" -> /mnt); empty stays at root
FILEBROWSER_DEFAULT_START = ""
# Use tmpfs; JPG written by GUI via libjpeg
REMOTE_SCREEN_PATH = "/dev/shm/remote_screen.jpg"
REMOTE_INPUT_PATH = "/tmp/x6100_remote_ctrl"
# FT8 structured state shm (GUI ft8_remote.c writes; webserver read-only)
FT8_STATE_PATH = "/dev/shm/x6100_ft8_state"

# Logbook paths (DATA partition; same as x6100_gui)
FT_LOG_ADI_PATH = "/mnt/ft_log.adi"
INCOMING_LOG_ADI_PATH = "/mnt/incoming_log.adi"
QSO_LOG_DB_PATH = "/mnt/qso_log.db"
# Init script for stop/start around logbook mutations (does not affect boot autostart)
GUI_INIT_SCRIPT = "/etc/init.d/S95gui"
# GUI binary OTA (zip with single member named as content sha256)
GUI_BIN_PATH = "/usr/sbin/x6100_gui"
GUI_OTA_MAX_BYTES = 32 * 1024 * 1024
