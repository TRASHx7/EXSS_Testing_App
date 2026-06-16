"""
EXSS Device Test Recording System
Records pass/fail test results for Base Module, Top Module, and PCBA devices.

How it works (high level):
  1. The operator selects a unit type (Base Module, Top Module, or PCBA).
  2. They scan the module SN (EXB Base Module SN / EXS Top Module SN) or the
     PCBA number and mark each test PASS or FAIL.
  3. On submit the results are written to MySQL. The scanned module SN must
     already exist in the assembly table; if all tests pass it is used for the label.
  4. For Base Module and Top Module a CSV file is also dropped in S:\\ so the label
     printer software can pick it up automatically.

Configuration files (located next to the .exe / .py):
  db_config.ini  – base64-encoded MySQL credentials
  config.ini     – UCL/LCL pass/fail limits for measurements
"""
import csv
import os
import re
import sys
import base64
import configparser
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox

import mysql.connector


# ---------------------------------------------------------------------------
# Test item definitions
# Each entry is (display label shown in the UI, database column name).
# The order here controls the order the rows appear on screen.
# ---------------------------------------------------------------------------
BASE_MODULE_TESTS = [
    ("LOW Alarm Switch",  "low_alarm_switch"),
    ("HIGH Alarm Switch", "high_alarm_switch"),
    ("Power Switch",      "power_switch"),
    ("Green LED",         "green_led"),
    ("Yellow LED",        "yellow_led"),
    ("BAT Siren Switch",  "bat_siren_switch"),
    ("Test Button",       "test_button"),
    ("Connector A",       "connector_a"),
    ("LOW Siren Switch",  "low_siren_switch"),
    ("Connector B",       "connector_b"),
]

TOP_MODULE_TESTS = [
    ("Horn",                "horn"),
    ("Strobe 1 Light",      "strobe_1_light"),
    ("Strobe 2 Light",      "strobe_2_light"),
]

PCBA_TESTS = [
    ("LOW Alarm Switch",        "low_alarm_switch"),
    ("HIGH Alarm Switch",       "high_alarm_switch"),
    ("Power Switch",            "power_switch"),
    ("Green LED",               "green_led"),
    ("Yellow LED",              "yellow_led"),
    ("Critical Low BAT 00101",  "critical_low_bat_00101"),
    ("BAT Siren Switch 00000",  "bat_siren_switch_00000"),
    ("Test Button 11001",       "test_button_11001"),
    ("Connector A 10011",       "connector_a_10011"),
    ("LOW Siren Switch 10010",  "low_siren_switch_10010"),
    ("Connector B 01001",       "connector_b_01001"),
]


# ===========================================================================
# DATABASE SCHEMA CONFIGURATION
# If table or column names change in the database, update them here.
# ===========================================================================

# --- Top Module (EXS) test results table ------------------------------------
TBL_TOP          = "exs_test"          # stores one row per test run
COL_TOP_SN       = "device_sn"         # serial number column
COL_TOP_HORN_VOL = "horn_volume_dba"   # measured horn volume (float, dBA)

# --- Base Module (EXB) test results table -----------------------------------
TBL_BASE         = "exb_test"          # stores one row per test run
COL_BASE_SN      = "device_sn"         # serial number column

# --- PCBA test results table ------------------------------------------------
TBL_PCBA         = "exss_pcba_test"    # stores one row per test run
COL_PCBA_SN      = "main_pcba"         # PCBA serial number column (e.g. BLN-XXXX-XXXXXXXXRX)
COL_PCBA_CHARGE  = "charge_current_a"  # measured charge current (float, A)
COL_PCBA_24V     = "24v0_rail_v"       # measured 24 V rail voltage (float)
COL_PCBA_3V3     = "3v3_rail_v"        # measured 3.3 V rail voltage (float)
COL_PCBA_12V     = "12v0_rail_v"       # measured 12 V rail voltage (float)

# --- Assembly tables --------------------------------------------------------
# These track which PCBA / horn was installed in which device and hold the
# device_sn (already assigned before testing).
TBL_BASE_ASSEMBLY = "exb_assembly"
TBL_TOP_ASSEMBLY  = "exs_assembly"

# ===========================================================================

# Float measurements for PCBA tests – (display label, db column, unit string).
# Order here controls the order they appear in the Measurements section.
PCBA_MEASUREMENTS = [
    ("12V Rail",       COL_PCBA_12V,    "V"),
    ("Charge Current", COL_PCBA_CHARGE, "A"),
    ("3.3V Rail",      COL_PCBA_3V3,    "V"),
    ("24V Rail",       COL_PCBA_24V,    "V"),
]

# Label for the serial number input field, keyed by unit type
SN_LABELS = {
    "Base Module": "EXB Base Module SN", # operator scans the base module SN barcode
    "Top Module":  "EXS Top Module SN",  # operator scans the top module SN barcode
    "PCBA":        "Main PCBA S/N",
}

# Product IDs in the `product` table – used to pull printer / label info.
# Update these if the product records are ever recreated with new IDs.
PRODUCT_ID_BASE = 78   # EXB (Base Module)
PRODUCT_ID_TOP  = 79   # EXS (Top Module)

OPERATOR_OPTIONS = ["PRODUCTION", "ENGINEERING", "TROUBLESHOOTING", "RMA"]


# ---------------------------------------------------------------------------
# Locate config files next to the executable.
# sys.frozen is True when running as a PyInstaller .exe; otherwise use the
# directory that contains this .py file.
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE  = os.path.join(APP_DIR, "db_config.ini")   # MySQL credentials
LIMITS_FILE  = os.path.join(APP_DIR, "config.ini")      # UCL/LCL limits


# ---------------------------------------------------------------------------
# Database config loader
# ---------------------------------------------------------------------------
def load_db_config() -> dict:
    """Read db_config.ini, decode the base64 payload, and return MySQL kwargs.

    The file is base64-encoded so that credentials are not stored as plain text.
    Use create_config.py --encode to regenerate it if the password changes.
    """
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(
            f"Database config not found: {CONFIG_FILE}\n"
            "Run  create_config.py --encode  to create it."
        )
    with open(CONFIG_FILE, "r") as fh:
        encoded = fh.read().strip()

    decoded = base64.b64decode(encoded).decode("utf-8")
    cfg = configparser.ConfigParser()
    cfg.read_string(decoded)

    return {
        "host":     cfg["DB"]["host"].strip(),
        "port":     int(cfg["DB"]["port"].strip()),
        "user":     cfg["DB"]["user"].strip(),
        "password": cfg["DB"]["passwd"].strip(),
        "database": cfg["DB"]["db"].strip(),
    }


# ---------------------------------------------------------------------------
# Limits loader
# ---------------------------------------------------------------------------
def load_limits() -> configparser.ConfigParser:
    """Load UCL/LCL measurement limits from config.ini.

    Returns an empty ConfigParser if the file is missing so the rest of the
    code can still run (limits checks simply won't trigger).
    """
    cfg = configparser.ConfigParser()
    if os.path.exists(LIMITS_FILE):
        cfg.read(LIMITS_FILE)
    return cfg


def _safe_config_value(cfg: configparser.ConfigParser, section: str, key: str) -> str:
    """Return a config value as a stripped string, or '' if not found."""
    return cfg.get(section, key, fallback="").strip()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _to_tinyint(val: str):
    """Convert a bubble result string to a MySQL TINYINT value.

    'PASS' → 1, 'FAIL' → 0, anything else (empty / not yet selected) → None (NULL).
    """
    if val == "PASS":
        return 1
    if val == "FAIL":
        return 0
    return None


# ---------------------------------------------------------------------------
# Scanned serial-number parsing (EXS / EXB modules)
# ---------------------------------------------------------------------------
# The operator scans the contract-manufacturer QR label into the SN box. That
# barcode packs several fields separated by tabs:
#
#     SN <delim> MC <delim> CC1 <delim> CC2 <delim> XX
#
# where MC/CC1/CC2/XX are component identifiers this app doesn't use. Only the
# device SN – the leading "EX…" token – is needed here; everything from the
# first separator onward is discarded. The separator is a tab, but depending on
# the scanner it may arrive as a real tab character or as the literal two
# characters "\t". Since the SN is always letters and digits only, we take the
# leading run of letters/digits, which is robust to either form (and to a bare
# SN with no trailing fields).
#
# The SN follows the EXSS serialization scheme:  EXMFfyyssssss
#     EX      constant prefix
#     M       module:        S = Top (siren/strobe),  B = Base
#     F       steel finish:  S = stainless,  P = painted   <-- label "model" value
#     f       factory location (single char, defined in the database)
#     yy      2-digit year code
#     ssssss  6-digit serial, starting at 001000 and resetting each year


def _device_sn_from_scan(scanned: str) -> str:
    """Return the device SN from a scanned module barcode/QR.

    The scan may be the full contract-manufacturer barcode (the SN followed by
    tab-separated component fields). The SN is the leading run of letters and
    digits, so this works whether the separator is a real tab or the literal
    characters "\\t", and whether or not any trailing fields are present.
    """
    match = re.match(r"[A-Za-z0-9]+", scanned.strip())
    return match.group(0) if match else scanned.strip()


def _steel_finish_from_sn(device_sn: str) -> str:
    """Return the steel-finish code ('S' or 'P') encoded in an EXSS serial.

    The finish is the 4th character (F) of EXMFfyyssssss. Returns '' if the SN
    is too short or that character isn't a recognised finish code.
    """
    if len(device_sn) >= 4 and device_sn[:2].upper() == "EX":
        finish = device_sn[3].upper()
        if finish in ("S", "P"):
            return finish
    return ""


def _replace_into(cursor, table: str, columns: list, values: list):
    """Execute an INSERT INTO statement for the given table.

    Column and table names are backtick-quoted to safely handle reserved words.
    Values are passed as parameterised placeholders to prevent SQL injection.
    """
    col_str      = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    cursor.execute(
        f"INSERT INTO `{table}` ({col_str}) VALUES ({placeholders})",
        values,
    )


def _csv_file_name(unit: str) -> str:
    """Generate a unique CSV filename based on unit type and current timestamp.

    Format:  EXB20250409143022050.csv  (EXB/EXS + YYYYmmddHHMMSShh)
    The two-digit hundredths suffix makes collisions extremely unlikely even
    if two units finish testing within the same second.
    """
    prefix = "EXB" if unit == "Base Module" else "EXS"
    now = datetime.now()
    hundredths = now.microsecond // 10000
    return f"{prefix}{now.strftime('%Y%m%d%H%M%S')}{hundredths:02d}.csv"


def _write_printer_csv(unit: str, device_id: int, db_config: dict, device_sn: str = "", model: str = "") -> None:
    """Write a label-printer CSV to S:\\ after a successful test.

    The label printer software (running on the printer PC) watches S:\\ and
    picks up any new CSV files automatically. Each CSV has exactly one data row.

    Columns written:
      deviceSN      – the newly assigned device serial number
      name          – product.current_hw_model  
      model         – the material/finish on the metal frame, as EXSS-P (painted steel) or EXSS-S (stainless steel)
      description1  – product.model_description (human-readable product name)
      description2  – left blank (reserved for future use)
      year          – today's date in YYYY format
      deviceID      – the internal device_id from the device table
      certification – blank (not currently used)
      image         – blank (not currently used)
      Label         – printer_config.label_file_name (.lab template filename)
      Printer       – printer_config.printer_name (name of the label printer)
      Quantity      – hardcoded to 1 (always print one label per device)

    Product IDs used to look up data:
      Base Module → product_id 78
      Top Module  → product_id 79
    """
    # Select the product_id for this unit type
    product_id = PRODUCT_ID_BASE if unit == "Base Module" else PRODUCT_ID_TOP

    name = ""
    description1 = ""
    printer = ""
    label = ""

    # Open a short-lived connection just for the two lookup queries
    conn = mysql.connector.connect(**db_config)
    try:
        cur = conn.cursor(buffered=True)

        # Pull the hardware model and description from the product table
        cur.execute(
            "SELECT `current_hw_model`, `model_description` "
            "FROM `product` "
            "WHERE `product_id` = %s "
            "LIMIT 1",
            (product_id,),
        )
        row = cur.fetchone()
        if row:
            name         = row[0] or ""
            description1 = row[1] or ""

        # Pull the printer name and label template filename from printer_config
        cur.execute(
            "SELECT `printer_name`, `label_file_name` "
            "FROM `printer_config` "
            "WHERE `product_id` = %s "
            "LIMIT 1",
            (product_id,),
        )
        row = cur.fetchone()
        if row:
            printer = row[0] or ""
            label   = row[1] or ""
    finally:
        conn.close()

    description2 = ""   # reserved column – leave blank

    # S:\ is a mapped network drive shared with the printer PC
    output_dir = "S:\\"
    if not os.path.isdir(output_dir):
        raise FileNotFoundError(f"Output directory not available: {output_dir}")

    file_path = os.path.join(output_dir, _csv_file_name(unit))

    headers = [
        "deviceSN",
        "name",
        "model",
        "description1",
        "description2",
        "year",
        "deviceID",
        "certification",
        "image",
        "Label",
        "Printer",
        "Quantity",
    ]
    # Steel finish ("P"/"S") becomes the model label "EXSS-P"/"EXSS-S"; blank if unknown
    model_label = f"EXSS-{model}" if model else ""

    now = datetime.now()
    csv_row = [
        device_sn,
        name,
        model_label,
        description1,
        description2,
        f"{now.year}",
        str(device_id),
        "",          # certification – not used
        "",          # image – not used
        label,
        printer,
        "1",         # always print one label
    ]

    with open(file_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(headers)
        writer.writerow(csv_row)


# ---------------------------------------------------------------------------
# Print-again dialog
# ---------------------------------------------------------------------------
def _ask_print_again(root: tk.Tk) -> bool:
    """Show a modal dialog after a passing test asking whether to reprint the label.

    Returns True  → operator clicked "Print Again" (regenerate the CSV)
    Returns False → operator clicked "Next Device" (reset and continue)
    """
    result = {"value": False}

    dlg = tk.Toplevel(root)
    dlg.title("Label Printed")
    dlg.resizable(False, False)
    dlg.grab_set()   # block interaction with the main window until closed
    dlg.focus_force()

    # Centre the dialog over the main window
    dlg.update_idletasks()
    x = root.winfo_x() + (root.winfo_width()  - dlg.winfo_width())  // 2
    y = root.winfo_y() + (root.winfo_height() - dlg.winfo_height()) // 2
    dlg.geometry(f"+{x}+{y}")

    tk.Label(
        dlg,
        text="Label sent to printer.\n\nDid the label print correctly?",
        font=("Segoe UI", 11),
        padx=24, pady=18,
    ).pack()

    btn_frame = tk.Frame(dlg, pady=12)
    btn_frame.pack()

    def on_print_again():
        result["value"] = True
        dlg.destroy()

    def on_next_device():
        result["value"] = False
        dlg.destroy()

    tk.Button(
        btn_frame, text="Print Again",
        command=on_print_again,
        font=("Segoe UI", 10, "bold"),
        bg="#F44336", fg="white",
        relief="flat", padx=16, pady=6,
        cursor="hand2",
        activebackground="#D32F2F", activeforeground="white",
    ).pack(side="left", padx=10)

    tk.Button(
        btn_frame, text="Next Device",
        command=on_next_device,
        font=("Segoe UI", 10, "bold"),
        bg="#1565C0", fg="white",
        relief="flat", padx=16, pady=6,
        cursor="hand2",
        activebackground="#1976D2", activeforeground="white",
    ).pack(side="left", padx=10)

    root.wait_window(dlg)
    return result["value"]


# ---------------------------------------------------------------------------
# Bubble widget – coloured circle toggle for PASS / FAIL
# ---------------------------------------------------------------------------
class BubbleButton(tk.Canvas):
    """A circular toggle button that represents one half of a PASS/FAIL pair.

    Each test row has two BubbleButtons sharing the same StringVar. Clicking
    either bubble sets the var to its own type ("PASS" or "FAIL"), which
    causes both bubbles to redraw – the selected one goes active (solid colour)
    and the other returns to inactive (pale).

    Colour states:
      inactive – pale tint  (no selection yet, or the other bubble is selected)
      hover    – mid tint   (mouse is over this bubble)
      active   – solid fill (this bubble is the current selection)
    """
    _COLOURS = {
        "PASS": {
            "active":   ("#4CAF50", "#2E7D32"),
            "hover":    ("#A5D6A7", "#4CAF50"),
            "inactive": ("#E8F5E9", "#81C784"),
        },
        "FAIL": {
            "active":   ("#F44336", "#B71C1C"),
            "hover":    ("#EF9A9A", "#F44336"),
            "inactive": ("#FFEBEE", "#EF9A9A"),
        },
    }

    def __init__(self, parent, bubble_type: str, result_var: tk.StringVar, **kwargs):
        super().__init__(parent, width=28, height=28,
                         highlightthickness=0, **kwargs)
        self._type    = bubble_type   # "PASS" or "FAIL"
        self._var     = result_var
        self._hovered = False

        self._draw()
        # Redraw whenever the shared StringVar changes (e.g. the partner bubble is clicked)
        result_var.trace_add("write", lambda *_: self._draw())

        self.bind("<Button-1>", self._click)
        self.bind("<Enter>",    self._enter)
        self.bind("<Leave>",    self._leave)

    def _draw(self):
        """Repaint the circle based on current selection and hover state."""
        self.delete("all")
        val = self._var.get()
        pal = self._COLOURS[self._type]

        if val == self._type:
            fill, outline = pal["active"]
        elif self._hovered:
            fill, outline = pal["hover"]
        else:
            fill, outline = pal["inactive"]

        self.create_oval(3, 3, 25, 25, fill=fill, outline=outline, width=2)

    def _click(self, _event):
        # Once a bubble has been selected it cannot be deselected, only switched
        self._var.set(self._type)

    def _enter(self, _event):
        self._hovered = True
        self._draw()

    def _leave(self, _event):
        self._hovered = False
        self._draw()


# ---------------------------------------------------------------------------
# One row in the test list
# ---------------------------------------------------------------------------
class TestItemRow(tk.Frame):
    """A single test item row: label + PASS bubble + FAIL bubble.

    Rows alternate between white and light grey for readability.
    """
    _ROW_COLOURS = ("#FFFFFF", "#F5F5F5")

    def __init__(self, parent, label: str, result_var: tk.StringVar,
                 row_index: int, **kwargs):
        bg = self._ROW_COLOURS[row_index % 2]   # alternate row colour
        super().__init__(parent, bg=bg, **kwargs)

        tk.Label(self, text=label, font=("Segoe UI", 10),
                 bg=bg, width=22, anchor="w").pack(side="left", padx=(12, 5), pady=6)

        BubbleButton(self, "PASS", result_var, bg=bg).pack(side="left", padx=14)
        BubbleButton(self, "FAIL", result_var, bg=bg).pack(side="left", padx=14)


# ---------------------------------------------------------------------------
# Float input field (voltage / dBA)
# ---------------------------------------------------------------------------
class FloatInput(tk.Frame):
    """A labelled text entry for a single numeric measurement (voltage, dBA, etc.)."""

    def __init__(self, parent, field_label: str, var: tk.StringVar,
                 unit: str = "V", **kwargs):
        super().__init__(parent, **kwargs)
        bg = self.cget("bg")
        tk.Label(self, text=field_label, font=("Segoe UI", 10),
                 width=16, anchor="w", bg=bg).pack(side="left", padx=4)
        tk.Entry(self, textvariable=var, width=10,
                 font=("Segoe UI", 10), justify="right").pack(side="left", padx=4)
        tk.Label(self, text=unit, font=("Segoe UI", 10),
                 bg=bg).pack(side="left")


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class TestApp:
    """Root application class. Owns the Tk window and all business logic.

    Layout (top to bottom):
      Header bar     – title
      Unit selector  – radio buttons: Base Module / Top Module / PCBA
      Content area   – rebuilt each time the operator selects a unit type
      Status bar     – save timestamp (left) + Submit button (right)
    """
    _BG        = "#ECEFF1"   # main window background (light blue-grey)
    _HEADER_BG = "#1565C0"   # header / column header background (dark blue)

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("EXSS Device Test Recorder")
        self.root.state("zoomed")
        self.root.resizable(True, True)
        self.root.configure(bg=self._BG)

        # Load database credentials at startup so errors surface immediately
        try:
            self._db_config = load_db_config()
        except FileNotFoundError as exc:
            messagebox.showwarning("Config Missing", str(exc))
            self._db_config = None
        except Exception as exc:
            messagebox.showerror("Config Error", str(exc))
            self._db_config = None

        # UCL/LCL limits used to flag out-of-range measurements as failures
        self._limits = load_limits()

        # Tkinter variables shared between the UI and submit logic
        self._unit_var     = tk.StringVar(value="")         # currently selected unit type
        self._sn_var       = tk.StringVar()                 # serial / PCBA number entered by operator
        self._horn_vol_var = tk.StringVar()                 # horn volume reading (Top Module only)
        self._operator_var = tk.StringVar(value="PRODUCTION")  # operator type dropdown

        # Populated when the content area is built; keyed by DB column name
        self._test_results: dict[str, tk.StringVar] = {}   # PASS/FAIL per test item
        self._voltage_vars: dict[str, tk.StringVar] = {}   # float readings per voltage rail

        # Reference to the scrollable canvas so we can unbind the mousewheel on rebuild
        self._active_canvas = None

        self._build_static_ui()

    # ------------------------------------------------------------------
    # Static UI (header / unit selector / submit bar)
    # These widgets are created once and never rebuilt.
    # ------------------------------------------------------------------
    def _build_static_ui(self):
        # ── Header ──────────────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=self._HEADER_BG, height=56)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="Device Test Recording System",
                 font=("Segoe UI", 14, "bold"),
                 bg=self._HEADER_BG, fg="white").pack(expand=True)

        # Database indicator – shows which host/schema the app is writing to so
        # the operator can confirm they're pointed at the right server (e.g.
        # production vs. a test database). Placed top-right so it doesn't shift
        # the centred title. Falls back to "not loaded" if the config failed.
        if self._db_config:
            db_text = f"DB: {self._db_config['host']} / {self._db_config['database']}"
        else:
            db_text = "DB: not loaded"
        tk.Label(hdr, text=db_text, font=("Segoe UI", 9, "bold"),
                 bg=self._HEADER_BG, fg="#BBDEFB").place(
                     relx=1.0, rely=0.5, anchor="e", x=-16)

        # ── Unit type selector ───────────────────────────────────────────
        sel = tk.LabelFrame(self.root, text="  Select Unit Type  ",
                            font=("Segoe UI", 10, "bold"),
                            bg=self._BG, padx=12, pady=8)
        sel.pack(fill="x", padx=20, pady=(12, 4))

        for unit in ("Base Module", "Top Module", "PCBA"):
            tk.Radiobutton(
                sel, text=unit, variable=self._unit_var, value=unit,
                command=self._on_unit_select,   # rebuilds the content area
                font=("Segoe UI", 11), bg=self._BG,
                activebackground=self._BG,
            ).pack(side="left", padx=14)

        tk.Frame(sel, bg="#B0BEC5", width=2).pack(side="left", fill="y", padx=20, pady=4)
        tk.Label(sel, text="Operator:", font=("Segoe UI", 10, "bold"),
                 bg=self._BG).pack(side="left", padx=(0, 6))
        ttk.Combobox(
            sel, textvariable=self._operator_var,
            values=OPERATOR_OPTIONS, state="readonly",
            font=("Segoe UI", 10), width=16,
        ).pack(side="left")

        # ── Dynamic content area (rebuilt on unit change) ────────────────
        self._content = tk.Frame(self.root, bg=self._BG)
        self._content.pack(fill="both", expand=True, padx=20, pady=4)

        # ── Bottom bar: status label + submit button ─────────────────────
        bar = tk.Frame(self.root, bg=self._BG)
        bar.pack(fill="x", padx=20, pady=8)

        self._status_lbl = tk.Label(bar, text="", font=("Segoe UI", 9),
                                    bg=self._BG, fg="#666666")
        self._status_lbl.pack(side="left", pady=6)

        self._submit_btn = tk.Button(
            bar, text="Submit Results",
            command=self._submit,
            font=("Segoe UI", 11, "bold"),
            bg=self._HEADER_BG, fg="white",
            relief="flat", padx=22, pady=7,
            state="disabled", cursor="hand2",
            activebackground="#1976D2", activeforeground="white",
        )
        self._submit_btn.pack(side="right")

    # ------------------------------------------------------------------
    # Dynamic content helpers
    # ------------------------------------------------------------------
    def _clear_content(self):
        """Destroy all widgets in the content area and reset all state vars.

        The mousewheel binding is global (bind_all), so it must be explicitly
        removed before rebuilding to avoid stacking multiple handlers.
        """
        if self._active_canvas:
            try:
                self._active_canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass
            self._active_canvas = None

        for w in self._content.winfo_children():
            w.destroy()

        # Clear dictionaries so stale column keys from the previous unit don't linger
        self._test_results.clear()
        self._voltage_vars.clear()
        self._sn_var.set("")
        self._horn_vol_var.set("")

    def _on_unit_select(self):
        """Rebuild the content area whenever the operator picks a different unit type."""
        self._clear_content()
        unit = self._unit_var.get()

        # Serial number input is always shown first
        self._build_sn_field(SN_LABELS[unit])

        if unit == "Base Module":
            self._build_test_list(BASE_MODULE_TESTS)

        elif unit == "Top Module":
            self._build_test_list(TOP_MODULE_TESTS)
            # Horn volume is an additional measurement only for Top Module
            self._build_extra_floats([("Horn Volume:", self._horn_vol_var, "dBA")])

        elif unit == "PCBA":
            self._build_test_list(PCBA_TESTS)
            # Create one StringVar per measurement field, then build the input fields
            for _, col, _ in PCBA_MEASUREMENTS:
                self._voltage_vars[col] = tk.StringVar()
            self._build_extra_floats(
                [(lbl, self._voltage_vars[col], unit)
                 for lbl, col, unit in PCBA_MEASUREMENTS]
            )

        self._submit_btn.config(state="normal")
        self._status_lbl.config(text="")

    def _build_sn_field(self, label: str):
        """Build the serial number / PCBA number input at the top of the content area."""
        sn_frame = tk.Frame(self._content, bg=self._BG)
        sn_frame.pack(fill="x", pady=(0, 6))
        tk.Label(sn_frame, text=label, font=("Segoe UI", 10, "bold"),
                 bg=self._BG, width=18, anchor="w").pack(side="left", padx=(0, 6))
        entry = tk.Entry(sn_frame, textvariable=self._sn_var, width=26,
                         font=("Segoe UI", 10))
        entry.pack(side="left")

        # For Base/Top the operator scans the contract-manufacturer QR, which
        # packs the SN plus component fields. Strip it down to just the SN in the
        # box once the scan finishes – scanners send Enter (<Return>); <FocusOut>
        # covers the case where focus moves on before that.
        if self._unit_var.get() in ("Base Module", "Top Module"):
            entry.bind("<Return>",   self._trim_sn_to_serial)
            entry.bind("<FocusOut>", self._trim_sn_to_serial)

    def _trim_sn_to_serial(self, _event=None):
        """Collapse a scanned module QR string in the SN box down to just the SN."""
        raw = self._sn_var.get()
        sn  = _device_sn_from_scan(raw)
        if sn != raw:
            self._sn_var.set(sn)

    def _build_extra_floats(self, fields: list):
        """Build the Measurements section with one FloatInput per field.

        fields: list of (label, StringVar, unit_string) e.g. [("24V Rail:", var, "V")]
        """
        frame = tk.LabelFrame(self._content, text="  Measurements  ",
                              font=("Segoe UI", 10, "bold"),
                              bg=self._BG, padx=8, pady=8)
        frame.pack(fill="x", pady=(8, 0))
        for lbl, var, unit in fields:
            FloatInput(frame, lbl, var, unit=unit, bg=self._BG).pack(
                anchor="w", pady=2)

    def _build_test_list(self, items: list[tuple[str, str]]):
        """Build the scrollable PASS/FAIL test list.

        Creates a Canvas + Scrollbar so the list can scroll if it overflows
        the window height. Each item gets a TestItemRow and a StringVar stored
        in self._test_results keyed by DB column name.

        items: list of (display_label, db_column)
        """
        # Column header row (not scrollable – stays pinned at the top)
        hdr = tk.Frame(self._content, bg=self._HEADER_BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Test Item", font=("Segoe UI", 10),
                 bg=self._HEADER_BG, fg="white",
                 width=22, anchor="w").pack(side="left", padx=(12, 5), pady=5)
        tk.Label(hdr, text="PASS", font=("Segoe UI", 10, "bold"),
                 bg=self._HEADER_BG, fg="#A5D6A7").pack(side="left", padx=14)
        tk.Label(hdr, text="FAIL", font=("Segoe UI", 10, "bold"),
                 bg=self._HEADER_BG, fg="#EF9A9A").pack(side="left", padx=8)
        # Spacer to compensate for the scrollbar that sits to the right of the list rows
        tk.Frame(hdr, bg=self._HEADER_BG, width=17).pack(side="right")

        list_wrap = tk.Frame(self._content, bg=self._BG)
        list_wrap.pack(fill="both", expand=True)

        # Canvas holds the inner frame; scrollbar controls canvas.yview
        canvas = tk.Canvas(list_wrap, bg="#FFFFFF",
                           highlightthickness=1, highlightbackground="#B0BEC5")
        scrollbar = ttk.Scrollbar(list_wrap, orient="vertical",
                                  command=canvas.yview)
        inner = tk.Frame(canvas, bg="#FFFFFF")

        # Update the scroll region whenever the inner frame resizes
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for idx, (label, col) in enumerate(items):
            var = tk.StringVar(value="")
            self._test_results[col] = var          # store so _submit can read results
            TestItemRow(inner, label, var, idx).pack(fill="x")

        # Save reference so _clear_content can remove the global mousewheel binding
        self._active_canvas = canvas
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

    # ------------------------------------------------------------------
    # Validation  –  called before any DB writes
    # ------------------------------------------------------------------
    def _validate(self) -> bool:
        """Check all inputs are present and within expected formats.

        Shows a warning dialog for the first problem found and returns False
        so _submit can bail out early. Returns True when everything is valid.
        """
        # Serial number is always required
        if not self._sn_var.get().strip():
            messagebox.showwarning("Missing ID",
                                   "Please enter the Device ID / S/N.")
            return False

        unit = self._unit_var.get()

        # Base / Top modules are identified by a scanned EXSS serial number. The
        # steel-finish code (the 4th character) drives the "model" value on the
        # printed label, so make sure we can read it before going any further.
        if unit in ("Base Module", "Top Module"):
            device_sn = _device_sn_from_scan(self._sn_var.get())
            if not _steel_finish_from_sn(device_sn):
                messagebox.showwarning(
                    "Invalid Module S/N",
                    f'Could not read the steel finish from "{device_sn}".\n\n'
                    "Expected an EXSS serial like EXSP125001000, where the 4th "
                    "character is the finish (S = stainless, P = painted).\n\n"
                    "Please re-scan the module QR code.")
                return False

        # Horn volume must be present and numeric for Top Module
        if unit == "Top Module":
            val = self._horn_vol_var.get().strip()
            if not val:
                messagebox.showwarning("Missing Value",
                                       "Please enter the Horn Volume (dBA).")
                return False
            try:
                float(val)
            except ValueError:
                messagebox.showwarning("Invalid Value",
                                       f'"{val}" is not a valid number for Horn Volume.')
                return False

        # PCBA serial numbers must match the Blackline format: BLN-XXXX-XXXXXXXXRX
        if unit == "PCBA":
            pcba_sn = self._sn_var.get().strip()
            if not re.fullmatch(r"BLN-\d{4}-\d{8}R\d+", pcba_sn):
                messagebox.showwarning("Invalid S/N",
                                       f'"{pcba_sn}" is not a valid PCBA serial number.\n'
                                       "Expected format: BLN-XXXX-XXXXXXXXRX\n"
                                       "Example: BLN-3724-94001026R3")
                return False

        # All measurement fields must be present and numeric for PCBA
        for lbl, col, _ in PCBA_MEASUREMENTS if unit == "PCBA" else []:
            val = self._voltage_vars[col].get().strip()
            if not val:
                messagebox.showwarning("Missing Value",
                                       f"Please enter a reading for {lbl}.")
                return False
            try:
                float(val)
            except ValueError:
                messagebox.showwarning("Invalid Value",
                                       f'"{val}" is not a valid number for {lbl}.')
                return False

        # Every test item bubble must have a selection before submitting
        if unit == "Base Module":
            test_list = BASE_MODULE_TESTS
        elif unit == "Top Module":
            test_list = TOP_MODULE_TESTS
        else:
            test_list = PCBA_TESTS

        for label, col in test_list:
            if not self._test_results[col].get():
                messagebox.showwarning("Incomplete",
                                       f"Please select PASS or FAIL for:\n{label}")
                return False

        return True

    # ------------------------------------------------------------------
    # Failure checker  –  returns list of human-readable failure descriptions
    # ------------------------------------------------------------------
    def _collect_failures(self) -> list:
        """Return a list of failure descriptions for the current submission.

        A failure is either:
          • A bubble marked FAIL by the operator, or
          • A measurement that falls outside the UCL/LCL defined in config.ini.

        An empty list means everything passed.
        """
        failures = []
        unit = self._unit_var.get()

        # Collect any bubbles explicitly marked FAIL
        if unit == "Base Module":
            test_list = BASE_MODULE_TESTS
        elif unit == "Top Module":
            test_list = TOP_MODULE_TESTS
        else:
            test_list = PCBA_TESTS

        for label, col in test_list:
            if self._test_results[col].get() == "FAIL":
                failures.append(f"{label}  —  FAIL")

        sec = "limits"   # section name in config.ini

        # Check horn volume against UCL/LCL (Top Module only)
        if unit == "Top Module" and self._limits.has_section(sec):
            val = float(self._horn_vol_var.get().strip())
            lcl = self._limits.getfloat(sec, "horn_volume__dba_lcl", fallback=None)
            ucl = self._limits.getfloat(sec, "horn_volume__dba_ucl", fallback=None)
            if (lcl is not None and val < lcl) or (ucl is not None and val > ucl):
                failures.append(
                    f"Horn Volume  —  {val} dBA  (limits: {lcl} – {ucl} dBA)"
                )

        # Check each PCBA measurement against UCL/LCL
        if unit == "PCBA" and self._limits.has_section(sec):
            # Maps DB column name → key prefix used in config.ini
            key_map = {
                COL_PCBA_CHARGE: "charge_current_a",
                COL_PCBA_24V:    "24v0_rail_v",
                COL_PCBA_3V3:    "3v3_rail_v",
                COL_PCBA_12V:    "12v0_rail_v",
            }
            for lbl, col, unit_str in PCBA_MEASUREMENTS:
                val = float(self._voltage_vars[col].get().strip())
                k   = key_map.get(col, "")
                lcl = self._limits.getfloat(sec, f"{k}_lcl", fallback=None)
                ucl = self._limits.getfloat(sec, f"{k}_ucl", fallback=None)
                if (lcl is not None and val < lcl) or (ucl is not None and val > ucl):
                    failures.append(
                        f"{lbl}  —  {val} {unit_str}  (limits: {lcl} – {ucl} {unit_str})"
                    )

        return failures

    # ------------------------------------------------------------------
    # Submit  –  orchestrates validation → DB write → CSV → UI reset
    # ------------------------------------------------------------------
    def _submit(self):
        """Handle the Submit Results button click.

        Flow:
          1. Validate all inputs (abort on error).
          2. Write results to the database and get back the assigned serial + device_id.
          3. Show success / failure summary to the operator.
          4. Write the printer CSV to S:\\ (Base Module and Top Module only).
          5. Reset all input fields so the next unit can be tested immediately.
        """
        if not self._validate():
            return

        if not self._db_config:
            messagebox.showerror("No Config",
                                 "Database configuration is not loaded.\n"
                                 "Run create_config.py --encode first.")
            return

        try:
            assigned_serial, device_id, finish = self._save_to_db()
        except ValueError as exc:
            # Raised when the scanned module SN doesn't exist in the assembly table
            messagebox.showerror("Assembly Lookup Error", str(exc))
            return
        except mysql.connector.Error as exc:
            messagebox.showerror("Database Error",
                                 f"Failed to save results:\n{exc.msg}")
            return
        except Exception as exc:
            messagebox.showerror("Error", f"Unexpected error:\n{exc}")
            return

        # Show the operator what (if anything) failed
        failures = self._collect_failures()
        if failures:
            msg = "Results saved.\n\nThe following tests failed:\n\n" + "\n".join(f"  \u2022 {f}" for f in failures)
            messagebox.showwarning("Test Failures", msg)
        else:
            if assigned_serial:
                messagebox.showinfo(
                    "Saved",
                    f"Test results saved successfully!\n\n"
                    f"Serial Number:  {assigned_serial}",
                )
            else:
                messagebox.showinfo("Saved", "Test results saved successfully!")

        self._status_lbl.config(
            text="Saved at " + datetime.now().strftime("%H:%M:%S"),
            fg="#2E7D32",
        )

        # Drop the printer CSV only on a fully passing test, then let the
        # operator reprint as many times as needed before moving on.
        if self._unit_var.get() in ("Base Module", "Top Module") and device_id is not None and not failures:
            try:
                _write_printer_csv(self._unit_var.get(), device_id, self._db_config, assigned_serial or "", finish)
            except Exception as exc:
                messagebox.showerror("CSV Error",
                                     f"Test results saved, but failed to create CSV file:\n{exc}")

            # Keep prompting until the operator confirms the label printed or skips
            while _ask_print_again(self.root):
                try:
                    _write_printer_csv(self._unit_var.get(), device_id, self._db_config, assigned_serial or "", finish)
                except Exception as exc:
                    messagebox.showerror("CSV Error",
                                         f"Failed to reprint CSV file:\n{exc}")
                    break

        # Reset all input fields so the operator can immediately test the next unit
        self._sn_var.set("")
        self._horn_vol_var.set("")
        for var in self._test_results.values():
            var.set("")
        for var in self._voltage_vars.values():
            var.set("")

    # ------------------------------------------------------------------
    # Database write  –  one INSERT per unit type
    # ------------------------------------------------------------------
    def _save_to_db(self) -> tuple[str | None, int | None, str]:
        """Write test results to the database.

        Returns:
          (assigned_serial, device_id, finish)
            assigned_serial – the device serial number string, or None for PCBA / failures
            device_id       – the internal device_id, or None for PCBA
            finish          – steel-finish code 'S'/'P' for the label, or '' (PCBA)

        Raises ValueError if the operator-scanned module SN is not found in
        the assembly table (e.g. a typo). The whole transaction is rolled back.

        Serial number logic (Base Module and Top Module only):
          • The operator scans the module SN (device_sn); it must already exist
            in the assembly table. On a passing test it is used for the label CSV.
          • On a failed test, no serial is returned.
        """
        conn = mysql.connector.connect(**self._db_config)
        assigned_serial    = None
        assigned_device_id = None
        finish             = ""    # steel finish (S/P) parsed from the module SN
        try:
            cur  = conn.cursor(buffered=True)
            unit = self._unit_var.get()
            sn   = self._sn_var.get().strip()

            # overall: 1 = all tests passed, 0 = at least one failure
            overall = 0 if self._collect_failures() else 1

            # ── Base Module ──────────────────────────────────────────────
            if unit == "Base Module":
                # The scan may be the full contract-manufacturer barcode; keep
                # only the first field as the SN and read the steel finish from it.
                device_sn = _device_sn_from_scan(sn)
                finish    = _steel_finish_from_sn(device_sn)

                # Find the latest assembly record for this device SN.
                # ORDER BY device_id DESC ensures we get the most recent entry.
                cur.execute(
                    f"SELECT `device_id` FROM `{TBL_BASE_ASSEMBLY}` "
                    f"WHERE `device_sn` = %s ORDER BY `device_id` DESC LIMIT 1",
                    (device_sn,),
                )
                asm_row = cur.fetchone()
                if not asm_row:
                    raise ValueError(
                        f"Base Module SN '{device_sn}' was not found in {TBL_BASE_ASSEMBLY}.\n"
                        "Please check the EXB Base Module SN."
                    )
                device_id = asm_row[0]
                assigned_device_id = device_id   # returned to caller for CSV writing

                # Write test results row. exb_test_id is the auto-increment primary
                # key, so each submit appends a new test run for this device_id.
                cols = ["device_id"] + [col for _, col in BASE_MODULE_TESTS] + ["test_result", "operator"]
                vals = ([device_id]
                        + [_to_tinyint(self._test_results[col].get())
                           for _, col in BASE_MODULE_TESTS]
                        + [overall, self._operator_var.get()])
                _replace_into(cur, TBL_BASE, cols, vals)

                if overall == 1:
                    # Record manufacture date in the device table
                    cur.execute(
                        "UPDATE `device` "
                        "SET `date_of_manufacture` = NOW() "
                        "WHERE `device_id` = %s",
                        (device_id,),
                    )
                    assigned_serial = device_sn

            # ── Top Module ───────────────────────────────────────────────
            elif unit == "Top Module":
                # Same as Base: strip any component fields off the scan and read
                # the steel finish from the SN.
                device_sn = _device_sn_from_scan(sn)
                finish    = _steel_finish_from_sn(device_sn)

                # Find the latest assembly record for this device SN
                cur.execute(
                    f"SELECT `device_id` FROM `{TBL_TOP_ASSEMBLY}` "
                    f"WHERE `device_sn` = %s ORDER BY `device_id` DESC LIMIT 1",
                    (device_sn,),
                )
                asm_row = cur.fetchone()
                if not asm_row:
                    raise ValueError(
                        f"Top Module SN '{device_sn}' was not found in {TBL_TOP_ASSEMBLY}.\n"
                        "Please check the EXS Top Module SN."
                    )
                device_id = asm_row[0]
                assigned_device_id = device_id   # returned to caller for CSV writing

                horn_vol_str = self._horn_vol_var.get().strip()
                cols = (["device_id"]
                        + [col for _, col in TOP_MODULE_TESTS]
                        + [COL_TOP_HORN_VOL, "test_result", "operator"])
                vals = ([device_id]
                        + [_to_tinyint(self._test_results[col].get())
                           for _, col in TOP_MODULE_TESTS]
                        + [float(horn_vol_str), overall, self._operator_var.get()])
                _replace_into(cur, TBL_TOP, cols, vals)

                if overall == 1:
                    cur.execute(
                        "UPDATE `device` "
                        "SET `date_of_manufacture` = NOW() "
                        "WHERE `device_id` = %s",
                        (device_id,),
                    )
                    assigned_serial = device_sn

            # ── PCBA ─────────────────────────────────────────────────────
            elif unit == "PCBA":
                # PCBA test results are keyed directly by the PCBA serial number.
                # No assembly lookup or serial generation needed.
                cols = ([COL_PCBA_SN]
                        + [col for _, col in PCBA_TESTS]
                        + [col for _, col, _ in PCBA_MEASUREMENTS]
                        + ["test_result", "operator"])
                vals = ([sn]
                        + [_to_tinyint(self._test_results[col].get())
                           for _, col in PCBA_TESTS]
                        + [float(self._voltage_vars[col].get().strip())
                           for _, col, _ in PCBA_MEASUREMENTS]
                        + [overall, self._operator_var.get()])
                _replace_into(cur, TBL_PCBA, cols, vals)

            conn.commit()
            return assigned_serial, assigned_device_id, finish

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    root = tk.Tk()
    app = TestApp(root)
    root.mainloop()
