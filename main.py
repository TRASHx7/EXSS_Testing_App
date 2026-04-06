"""
EXSS Device Test Recording System
Records pass/fail test results for Base Module, Top Module, and PCBA devices.
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
# Test item definitions  –  (display label, database column name)
# ---------------------------------------------------------------------------
BASE_MODULE_TESTS = [
    ("LOW Alarm Switch",  "low_alarm_switch"),
    ("HIGH Alarm Switch", "high_alarm_switch"),
    ("Power Switch",      "power_switch"),
    ("LOW Siren Switch",  "low_siren_switch"),
    ("BAT Siren Switch",  "bat_siren_switch"),
    ("Yellow LED",        "yellow_led"),
    ("Green LED",         "green_led"),
    ("Test Button",       "test_button"),
    ("Connector A",       "connector_a"),
    ("Connector B",       "connector_b"),
]

TOP_MODULE_TESTS = [
    ("Horn",                "horn"),
    ("Red Strobe Light",    "red_strobe_light"),
    ("Yellow Strobe Light", "yellow_strobe_light"),
]

PCBA_TESTS = BASE_MODULE_TESTS   # same columns as base module

# ===========================================================================
# DATABASE SCHEMA CONFIGURATION
# Type your table and column names below to match your existing database.
# ===========================================================================

# --- Top Module table -------------------------------------------------------
TBL_TOP          = "exs_test"          # <-- table name
COL_TOP_SN       = "device_sn"         # <-- serial number column (primary key)
COL_TOP_HORN_VOL = "horn_volume_dba"   # <-- horn volume column (float, dBA)

# --- Base Module table ------------------------------------------------------
TBL_BASE         = "exb_test"          # <-- table name
COL_BASE_SN      = "device_sn"         # <-- serial number column (primary key)

# --- PCBA table -------------------------------------------------------------
TBL_PCBA         = "exss_pcba_test"    # <-- table name
COL_PCBA_SN      = "main_pcba"         # <-- serial number column (primary key)
COL_PCBA_24V     = "24v0_rail_v"       # <-- 24 V rail voltage column (float)
COL_PCBA_3V3     = "3v3_rail_v"        # <-- 3.3 V rail voltage column (float)
COL_PCBA_12V     = "12v0_rail_v"       # <-- 12 V rail voltage column (float)

# --- Assembly tables (serial number assignment) ----------------------------
TBL_BASE_ASSEMBLY = "exb_assembly"
TBL_TOP_ASSEMBLY  = "exs_assembly"

# ===========================================================================

# Voltage rails for PCBA – (display label, db column)
# Order here determines the order displayed in the UI.
PCBA_VOLTAGE_RAILS = [
    ("24V",  COL_PCBA_24V),
    ("3.3V", COL_PCBA_3V3),
    ("12V",  COL_PCBA_12V),
]

# Serial number field labels per unit type
SN_LABELS = {
    "Base Module":  "Main PCBA S/N",
    "Top Module": "Horn Serial Number",
    "PCBA":       "Main PCBA S/N",
}

# ---------------------------------------------------------------------------
# Locate files next to the executable (works both frozen and as .py)
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE  = os.path.join(APP_DIR, "db_config.ini")
LIMITS_FILE  = os.path.join(APP_DIR, "config.ini")


# ---------------------------------------------------------------------------
# Database config loader
# ---------------------------------------------------------------------------
def load_db_config() -> dict:
    """Decode the base64-encoded .ini file and return connection kwargs."""
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
    """Load UCL/LCL limits from config.ini. Returns empty config if missing."""
    cfg = configparser.ConfigParser()
    if os.path.exists(LIMITS_FILE):
        cfg.read(LIMITS_FILE)
    return cfg


def _safe_config_value(cfg: configparser.ConfigParser, section: str, key: str) -> str:
    return cfg.get(section, key, fallback="").strip()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _to_tinyint(val: str):
    """Convert 'PASS' → 1, 'FAIL' → 0, '' → None (NULL)."""
    if val == "PASS":
        return 1
    if val == "FAIL":
        return 0
    return None


def _replace_into(cursor, table: str, columns: list, values: list):
    """Build and execute an INSERT INTO statement with backtick-quoted names."""
    col_str      = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    cursor.execute(
        f"INSERT INTO `{table}` ({col_str}) VALUES ({placeholders})",
        values,
    )


def _csv_file_name(unit: str) -> str:
    prefix = "EXB" if unit == "Base Module" else "EXS"
    now = datetime.now()
    hundredths = now.microsecond // 10000
    return f"{prefix}{now.strftime('%Y%m%d%H%M%S')}{hundredths:02d}.csv"


def _write_printer_csv(unit: str, device_id: int, limits: configparser.ConfigParser, device_sn: str = "") -> None:
    section = "exb_printer" if unit == "Base Module" else "exs_printer"
    if not limits.has_section(section):
        raise ValueError(f"Missing printer section '{section}' in config.ini")

    name = _safe_config_value(limits, section, "exb_name" if unit == "Base Module" else "exs_name")
    description1 = _safe_config_value(limits, section, "exb_description1" if unit == "Base Module" else "exs_description1")
    description2 = _safe_config_value(limits, section, "exb_description2" if unit == "Base Module" else "exs_description2")
    printer = _safe_config_value(limits, section, "printer")
    quantity = _safe_config_value(limits, section, "quantity")

    if description2.upper() == "NA":
        description2 = ""

    output_dir = "S:\\"
    if not os.path.isdir(output_dir):
        raise FileNotFoundError(f"Output directory not available: {output_dir}")

    file_path = os.path.join(output_dir, _csv_file_name(unit))
    headers = [
        "deviceSN",
        "name",
        "description1",
        "description2",
        "date",
        "deviceID",
        "certification",
        "image",
        "Label",
        "Printer",
        "Quantity",
    ]
    now = datetime.now()
    row = [
        device_sn,
        name,
        description1,
        description2,
        f"{now.month}/{now.day}/{now.year}",
        str(device_id),
        "",
        "",
        "",
        printer,
        quantity,
    ]

    with open(file_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(headers)
        writer.writerow(row)


# ---------------------------------------------------------------------------
# Bubble widget – coloured circle toggle for PASS / FAIL
# ---------------------------------------------------------------------------
class BubbleButton(tk.Canvas):
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
        result_var.trace_add("write", lambda *_: self._draw())

        self.bind("<Button-1>", self._click)
        self.bind("<Enter>",    self._enter)
        self.bind("<Leave>",    self._leave)

    def _draw(self):
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
    _ROW_COLOURS = ("#FFFFFF", "#F5F5F5")

    def __init__(self, parent, label: str, result_var: tk.StringVar,
                 row_index: int, **kwargs):
        bg = self._ROW_COLOURS[row_index % 2]
        super().__init__(parent, bg=bg, **kwargs)

        tk.Label(self, text=label, font=("Segoe UI", 10),
                 bg=bg, width=22, anchor="w").pack(side="left", padx=(12, 5), pady=6)

        BubbleButton(self, "PASS", result_var, bg=bg).pack(side="left", padx=14)
        BubbleButton(self, "FAIL", result_var, bg=bg).pack(side="left", padx=14)


# ---------------------------------------------------------------------------
# Float input field (voltage / dBA)
# ---------------------------------------------------------------------------
class FloatInput(tk.Frame):
    def __init__(self, parent, field_label: str, var: tk.StringVar,
                 unit: str = "V", **kwargs):
        super().__init__(parent, **kwargs)
        bg = self.cget("bg")
        tk.Label(self, text=field_label, font=("Segoe UI", 10),
                 width=12, anchor="w", bg=bg).pack(side="left", padx=4)
        tk.Entry(self, textvariable=var, width=10,
                 font=("Segoe UI", 10), justify="right").pack(side="left", padx=4)
        tk.Label(self, text=unit, font=("Segoe UI", 10),
                 bg=bg).pack(side="left")


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class TestApp:
    _BG        = "#ECEFF1"
    _HEADER_BG = "#1565C0"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("EXSS Device Test Recorder")
        self.root.state("zoomed")
        self.root.resizable(True, True)
        self.root.configure(bg=self._BG)

        try:
            self._db_config = load_db_config()
        except FileNotFoundError as exc:
            messagebox.showwarning("Config Missing", str(exc))
            self._db_config = None
        except Exception as exc:
            messagebox.showerror("Config Error", str(exc))
            self._db_config = None

        self._limits = load_limits()

        self._unit_var    = tk.StringVar(value="")
        self._sn_var      = tk.StringVar()
        self._horn_vol_var = tk.StringVar()
        # keyed by db column name
        self._test_results: dict[str, tk.StringVar] = {}
        self._voltage_vars: dict[str, tk.StringVar] = {}

        self._active_canvas = None

        self._build_static_ui()

    # ------------------------------------------------------------------
    # Static UI (header / unit selector / submit bar)
    # ------------------------------------------------------------------
    def _build_static_ui(self):
        hdr = tk.Frame(self.root, bg=self._HEADER_BG, height=56)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="Device Test Recording System",
                 font=("Segoe UI", 14, "bold"),
                 bg=self._HEADER_BG, fg="white").pack(expand=True)

        sel = tk.LabelFrame(self.root, text="  Select Unit Type  ",
                            font=("Segoe UI", 10, "bold"),
                            bg=self._BG, padx=12, pady=8)
        sel.pack(fill="x", padx=20, pady=(12, 4))

        for unit in ("Base Module", "Top Module", "PCBA"):
            tk.Radiobutton(
                sel, text=unit, variable=self._unit_var, value=unit,
                command=self._on_unit_select,
                font=("Segoe UI", 11), bg=self._BG,
                activebackground=self._BG,
            ).pack(side="left", padx=14)

        self._content = tk.Frame(self.root, bg=self._BG)
        self._content.pack(fill="both", expand=True, padx=20, pady=4)

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
        if self._active_canvas:
            try:
                self._active_canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass
            self._active_canvas = None

        for w in self._content.winfo_children():
            w.destroy()

        self._test_results.clear()
        self._voltage_vars.clear()
        self._sn_var.set("")
        self._horn_vol_var.set("")
    def _on_unit_select(self):
        self._clear_content()
        unit = self._unit_var.get()

        # Serial number input (always shown)
        self._build_sn_field(SN_LABELS[unit])

        if unit == "Base Module":
            self._build_test_list(BASE_MODULE_TESTS)

        elif unit == "Top Module":
            self._build_test_list(TOP_MODULE_TESTS)
            self._build_extra_floats([("Horn Volume:", self._horn_vol_var, "dBA")])

        elif unit == "PCBA":
            self._build_test_list(PCBA_TESTS)
            v_vars = {}
            for _, col in PCBA_VOLTAGE_RAILS:
                v_vars[col] = tk.StringVar()
                self._voltage_vars[col] = v_vars[col]
            self._build_extra_floats(
                [(f"{lbl} Rail:", self._voltage_vars[col], "V")
                 for lbl, col in PCBA_VOLTAGE_RAILS]
            )

        self._submit_btn.config(state="normal")
        self._status_lbl.config(text="")

    def _build_sn_field(self, label: str):
        sn_frame = tk.Frame(self._content, bg=self._BG)
        sn_frame.pack(fill="x", pady=(0, 6))
        tk.Label(sn_frame, text=label, font=("Segoe UI", 10, "bold"),
                 bg=self._BG, width=18, anchor="w").pack(side="left", padx=(0, 6))
        tk.Entry(sn_frame, textvariable=self._sn_var, width=26,
                 font=("Segoe UI", 10)).pack(side="left")

    def _build_extra_floats(self, fields: list):
        """Build a row of float input fields (voltages, dBA, etc.)."""
        frame = tk.LabelFrame(self._content, text="  Measurements  ",
                              font=("Segoe UI", 10, "bold"),
                              bg=self._BG, padx=8, pady=8)
        frame.pack(fill="x", pady=(8, 0))
        for lbl, var, unit in fields:
            FloatInput(frame, lbl, var, unit=unit, bg=self._BG).pack(
                anchor="w", pady=2)

    def _build_test_list(self, items: list[tuple[str, str]]):
        """items: list of (display_label, db_column)"""
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

        canvas = tk.Canvas(list_wrap, bg="#FFFFFF",
                           highlightthickness=1, highlightbackground="#B0BEC5")
        scrollbar = ttk.Scrollbar(list_wrap, orient="vertical",
                                  command=canvas.yview)
        inner = tk.Frame(canvas, bg="#FFFFFF")

        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for idx, (label, col) in enumerate(items):
            var = tk.StringVar(value="")
            self._test_results[col] = var
            TestItemRow(inner, label, var, idx).pack(fill="x")

        self._active_canvas = canvas
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _validate(self) -> bool:
        # Serial number is always required
        if not self._sn_var.get().strip():
            messagebox.showwarning("Missing ID",
                                   "Please enter the Device ID / S/N.")
            return False

        unit = self._unit_var.get()

        # Horn volume required for Top Module
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

        # PCBA S/N format: BLN-XXXX-XXXXXXXXRX
        if unit == "PCBA":
            pcba_sn = self._sn_var.get().strip()
            if not re.fullmatch(r"BLN-\d{4}-\d{8}R\d+", pcba_sn):
                messagebox.showwarning("Invalid S/N",
                                       f'"{pcba_sn}" is not a valid PCBA serial number.\n'
                                       "Expected format: BLN-XXXX-XXXXXXXXRX\n"
                                       "Example: BLN-3724-94001026R3")
                return False

        # Voltage rails required for PCBA
        for lbl, col in PCBA_VOLTAGE_RAILS if unit == "PCBA" else []:
            val = self._voltage_vars[col].get().strip()
            if not val:
                messagebox.showwarning("Missing Value",
                                       f"Please enter a reading for the {lbl} rail.")
                return False
            try:
                float(val)
            except ValueError:
                messagebox.showwarning("Invalid Value",
                                       f'"{val}" is not a valid number for the {lbl} rail.')
                return False

        # All bubbles must be selected
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
    # Submit
    # ------------------------------------------------------------------
    # Failure checker  –  returns list of human-readable failure descriptions
    # ------------------------------------------------------------------
    def _collect_failures(self) -> list:
        failures = []
        unit = self._unit_var.get()

        # Bubble failures
        if unit == "Base Module":
            test_list = BASE_MODULE_TESTS
        elif unit == "Top Module":
            test_list = TOP_MODULE_TESTS
        else:
            test_list = PCBA_TESTS

        for label, col in test_list:
            if self._test_results[col].get() == "FAIL":
                failures.append(f"{label}  —  FAIL")

        sec = "limits"

        # Horn volume vs UCL/LCL
        if unit == "Top Module" and self._limits.has_section(sec):
            val = float(self._horn_vol_var.get().strip())
            lcl = self._limits.getfloat(sec, "horn_volume__dba_lcl", fallback=None)
            ucl = self._limits.getfloat(sec, "horn_volume__dba_ucl", fallback=None)
            if (lcl is not None and val < lcl) or (ucl is not None and val > ucl):
                failures.append(
                    f"Horn Volume  —  {val} dBA  (limits: {lcl} – {ucl} dBA)"
                )

        # Voltage rails vs UCL/LCL
        if unit == "PCBA" and self._limits.has_section(sec):
            # Maps db column name to the key prefix used in config.ini
            key_map = {
                COL_PCBA_24V: "24v0_rail_v",
                COL_PCBA_3V3: "3v3_rail_v",
                COL_PCBA_12V: "12v0_rail_v",
            }
            for lbl, col in PCBA_VOLTAGE_RAILS:
                val = float(self._voltage_vars[col].get().strip())
                k   = key_map.get(col, "")
                lcl = self._limits.getfloat(sec, f"{k}_lcl", fallback=None)
                ucl = self._limits.getfloat(sec, f"{k}_ucl", fallback=None)
                if (lcl is not None and val < lcl) or (ucl is not None and val > ucl):
                    failures.append(
                        f"{lbl} Rail  —  {val} V  (limits: {lcl} – {ucl} V)"
                    )

        return failures

    # ------------------------------------------------------------------
    def _submit(self):
        if not self._validate():
            return

        if not self._db_config:
            messagebox.showerror("No Config",
                                 "Database configuration is not loaded.\n"
                                 "Run create_config.py --encode first.")
            return

        try:
            assigned_serial, device_id = self._save_to_db()
        except ValueError as exc:
            messagebox.showerror("Assembly Lookup Error", str(exc))
            return
        except mysql.connector.Error as exc:
            messagebox.showerror("Database Error",
                                 f"Failed to save results:\n{exc.msg}")
            return
        except Exception as exc:
            messagebox.showerror("Error", f"Unexpected error:\n{exc}")
            return

        failures = self._collect_failures()
        if failures:
            msg = "Results saved.\n\nThe following tests failed:\n\n" + "\n".join(f"  \u2022 {f}" for f in failures)
            messagebox.showwarning("Test Failures", msg)
        else:
            if assigned_serial:
                messagebox.showinfo(
                    "Saved",
                    f"Test results saved successfully!\n\n"
                    f"Assigned Serial Number:  {assigned_serial}",
                )
            else:
                messagebox.showinfo("Saved", "Test results saved successfully!")
        self._status_lbl.config(
            text="Saved at " + datetime.now().strftime("%H:%M:%S"),
            fg="#2E7D32",
        )
        try:
            if self._unit_var.get() in ("Base Module", "Top Module") and device_id is not None:
                _write_printer_csv(self._unit_var.get(), device_id, self._limits, assigned_serial or "")
        except Exception as exc:
            messagebox.showerror("CSV Error",
                                 f"Test results saved, but failed to create CSV file:\n{exc}")

        # Reset field values but stay on the same unit type page
        self._sn_var.set("")
        self._horn_vol_var.set("")
        for var in self._test_results.values():
            var.set("")
        for var in self._voltage_vars.values():
            var.set("")

    # ------------------------------------------------------------------
    # Serial number generation
    # ------------------------------------------------------------------
    def _next_serial(self, cursor, unit: str, material_code: str) -> str:
        """Return the next formatted serial number for the given unit and material.

        Base Module: EXB{material}{yy}{sssss}
        Top Module:  EXS{material}{yy}{sssss}
        Sequence is shared across materials, starts at 00100, resets each calendar year.
        """
        yy = datetime.now().strftime("%y")

        if unit == "Base Module":
            type_prefix, sn_col, table = "EXB", "device_sn", TBL_BASE_ASSEMBLY
        else:
            type_prefix, sn_col, table = "EXS", "device_sn", TBL_TOP_ASSEMBLY

        # Use '_' wildcard to match any material letter, keeping a shared sequence
        cursor.execute(
            f"SELECT MAX(CAST(RIGHT(`{sn_col}`, 5) AS UNSIGNED)) "
            f"FROM `{table}` "
            f"WHERE `{sn_col}` LIKE %s AND LENGTH(`{sn_col}`) = 11",
            (f"{type_prefix}_{yy}%",),
        )
        row = cursor.fetchone()
        last_seq = row[0] if row and row[0] is not None else 99  # seed so first = 00100
        next_seq = max(last_seq + 1, 100)
        return f"{type_prefix}{material_code}{yy}{next_seq:05d}"

    # ------------------------------------------------------------------
    # Database write  –  one REPLACE INTO per unit type
    # ------------------------------------------------------------------
    def _save_to_db(self) -> tuple[str | None, int | None]:
        """Save test results and assign a serial number for passing Base/Top tests.

        Returns the assigned serial string and the device_id for EXB/EXS.
        For PCBA or failing tests, the serial may be None and device_id may be None.
        Raises ValueError if the device_id is not found in the assembly table
        (the entire transaction is rolled back in that case).
        """
        conn = mysql.connector.connect(**self._db_config)
        assigned_serial = None
        assigned_device_id = None
        try:
            cur  = conn.cursor(buffered=True)
            unit = self._unit_var.get()
            sn   = self._sn_var.get().strip()

            # 1 = all tests passed, 0 = at least one failure
            overall = 0 if self._collect_failures() else 1

            if unit == "Base Module":
                pcba_sn = sn  # operator-entered PCBA serial number

                # Look up device_id, material, and existing device_sn from assembly table by PCBA serial
                cur.execute(
                    f"SELECT `device_id`, `material`, `device_sn` FROM `{TBL_BASE_ASSEMBLY}` "
                    f"WHERE `main_pcba` = %s ORDER BY `device_id` DESC LIMIT 1",
                    (pcba_sn,),
                )
                asm_row = cur.fetchone()
                if not asm_row:
                    raise ValueError(
                        f"PCBA serial '{pcba_sn}' was not found in {TBL_BASE_ASSEMBLY}.\n"
                        "Please check the PCBA serial number."
                    )
                device_id, material_code, existing_sn = asm_row[0], asm_row[1], asm_row[2]
                assigned_device_id = device_id
                if not material_code:
                    raise ValueError(
                        f"No material is set for PCBA serial '{pcba_sn}' in {TBL_BASE_ASSEMBLY}.\n"
                        "Please set the material before submitting test results."
                    )

                cols = ["device_id"] + [col for _, col in BASE_MODULE_TESTS] + ["test_result"]
                vals = ([device_id]
                        + [_to_tinyint(self._test_results[col].get())
                           for _, col in BASE_MODULE_TESTS]
                        + [overall])
                _replace_into(cur, TBL_BASE, cols, vals)

                if overall == 1:
                    if existing_sn:
                        serial = existing_sn
                    else:
                        serial = self._next_serial(cur, unit, material_code)
                        cur.execute(
                            f"UPDATE `{TBL_BASE_ASSEMBLY}` "
                            f"SET `device_sn` = %s, `date_modified` = NOW() "
                            f"WHERE `device_id` = %s",
                            (serial, device_id),
                        )
                    cur.execute(
                        "UPDATE `device` "
                        "SET `date_of_manufacture` = NOW() "
                        "WHERE `device_id` = %s",
                        (device_id,),
                    )
                    assigned_serial = serial

            elif unit == "Top Module":
                horn_sn = sn  # operator-entered horn serial number

                # Look up device_id, material, and existing device_sn from assembly table by horn serial
                cur.execute(
                    f"SELECT `device_id`, `material`, `device_sn` FROM `{TBL_TOP_ASSEMBLY}` "
                    f"WHERE `horn_sn` = %s ORDER BY `device_id` DESC LIMIT 1",
                    (horn_sn,),
                )
                asm_row = cur.fetchone()
                if not asm_row:
                    raise ValueError(
                        f"Horn serial '{horn_sn}' was not found in {TBL_TOP_ASSEMBLY}.\n"
                        "Please check the horn serial number."
                    )
                device_id, material_code, existing_sn = asm_row[0], asm_row[1], asm_row[2]
                assigned_device_id = device_id
                if not material_code:
                    raise ValueError(
                        f"No material is set for horn serial '{horn_sn}' in {TBL_TOP_ASSEMBLY}.\n"
                        "Please set the material before submitting test results."
                    )

                horn_vol_str = self._horn_vol_var.get().strip()
                cols = (["device_id"]
                        + [col for _, col in TOP_MODULE_TESTS]
                        + [COL_TOP_HORN_VOL, "test_result"])
                vals = ([device_id]
                        + [_to_tinyint(self._test_results[col].get())
                           for _, col in TOP_MODULE_TESTS]
                        + [float(horn_vol_str), overall])
                _replace_into(cur, TBL_TOP, cols, vals)

                if overall == 1:
                    if existing_sn:
                        serial = existing_sn
                    else:
                        serial = self._next_serial(cur, unit, material_code)
                        cur.execute(
                            f"UPDATE `{TBL_TOP_ASSEMBLY}` "
                            f"SET `device_sn` = %s, `date_modified` = NOW() "
                            f"WHERE `device_id` = %s",
                            (serial, device_id),
                        )
                    cur.execute(
                        "UPDATE `device` "
                        "SET `date_of_manufacture` = NOW() "
                        "WHERE `device_id` = %s",
                        (device_id,),
                    )
                    assigned_serial = serial

            elif unit == "PCBA":
                cols = ([COL_PCBA_SN]
                        + [col for _, col in PCBA_TESTS]
                        + [col for _, col in PCBA_VOLTAGE_RAILS]
                        + ["test_result"])
                vals = ([sn]
                        + [_to_tinyint(self._test_results[col].get())
                           for _, col in PCBA_TESTS]
                        + [float(self._voltage_vars[col].get().strip())
                           for _, col in PCBA_VOLTAGE_RAILS]
                        + [overall])
                _replace_into(cur, TBL_PCBA, cols, vals)

            conn.commit()
            return assigned_serial, assigned_device_id

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
