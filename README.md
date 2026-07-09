# EXSS Device Test Recorder

A desktop app for recording **manual** pass/fail test results for products that
don't yet carry a unit ID/label of their own.

An operator tests each product **by hand**, then enters the results into this
app. On submit, the app:

1. **Saves the results to a MySQL database**, and
2. For labelled products, **writes a CSV into the print folder** (`S:\`), which
   the label-printer software watches and picks up automatically to print the
   product's label.

The UI is a single-screen Tkinter application.

---

## Supported unit types

The operator picks one unit type per test. Each type has its own list of
PASS/FAIL checks and, where applicable, numeric measurements.

| Unit type   | Code | Identifier the operator scans / enters | PASS/FAIL checks | Measurements                              | Label printed? |
|-------------|------|----------------------------------------|------------------|-------------------------------------------|----------------|
| Base Module | EXB  | Module SN (e.g. `EXSP125001000`)       | 10               | —                                         | **Yes** → `S:\` CSV |
| Top Module  | EXS  | Module SN (e.g. `EXBS125001000`)       | 3                | Horn volume (dBA)                         | No             |
| PCBA        | —    | Main PCBA S/N (`BLN-XXXX-XXXXXXXXRX`)   | 11               | 12 V rail, 3.3 V rail, 24 V rail, charge current | No      |
| Charge Dock | CHRG | Main PCBA S/N (`BLN-XXXX-XXXXXXXXRX`)   | 1 (charge test)  | —                                         | **Yes** → `S:\` CSV |

Notes:

- **Base / Top modules** are looked up by their scanned serial number, which
  must already exist in the corresponding assembly table. The scan may be the
  full contract-manufacturer QR (SN plus tab-separated component fields); the
  app keeps only the leading serial.
- **PCBA** results are keyed directly by the Main PCBA S/N — no assembly lookup.
- **Charge Dock** is looked up by its Main PCBA S/N; on a passing test the app
  generates and assigns the next `CHRG` device serial for the current factory
  and year.

## How a test is recorded

1. Select the **unit type** and the **operator** role (Production, Engineering,
   Troubleshooting, RMA).
2. Scan the serial / PCBA number.
3. Mark every check **PASS** or **FAIL**, and enter any required measurements.
4. Click **Submit Results**. The app validates the inputs, writes one test-run
   row to the database, and shows a pass/fail summary.
5. On a **fully passing** test for a labelled unit (Base Module, Charge Dock),
   the app drops a label CSV into `S:\` and offers to reprint until the operator
   confirms the label printed correctly.

Measurements are checked against the upper/lower limits in `config.ini`; a
reading outside its limits counts as a failure.

---

## Requirements

- **Python 3.x** (developed against 3.14) with **Tkinter** (bundled with the
  standard Windows Python installer).
- **[mysql-connector-python](https://pypi.org/project/mysql-connector-python/)** — the only third-party dependency (see `requirements.txt`).
- Network access to the MySQL server, and the `S:\` network drive mapped on the
  test PC (only needed for units that print a label).

## Setup

```powershell
# From the project root
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configuration

Two INI files must sit **next to the script (or the built `.exe`)**:

### `db_config.ini` — MySQL credentials (required, not in version control)

The database credentials live in `db_config.ini`, base64-encoded so they aren't
stored as plain text. Prepare it manually: write the plaintext below, then
base64-encode the whole thing and save the result as `db_config.ini`.

```ini
[DB]
host   = your-db-host
port   = 3306
user   = your-db-user
passwd = your-db-password
db     = your-database-name
```

Encode it (for example):

```powershell
py -c "import base64; open('db_config.ini','wb').write(base64.b64encode(open('db_config_plain.ini','rb').read()))"
```

`db_config.ini` is git-ignored — **never commit it.**

### `config.ini` — measurement limits (included in the repo)

Upper/lower control limits (UCL/LCL) used to flag out-of-range measurements as
failures. `[exs limits]` holds the Top Module horn-volume limits; `[exb limits]`
holds the PCBA rail/charge-current limits. A missing file simply disables the
limit checks.

## Running

```powershell
.\.venv\Scripts\Activate.ps1
py UnserializedProductTest.py
```

The header bar shows which database host/schema the app is writing to, so the
operator can confirm they're pointed at the right server.

---

## Label CSV output

For labelled units, a single-row CSV is written to `S:\` (a mapped network drive
shared with the label-printer PC). The filename encodes the unit prefix and a
timestamp (e.g. `EXB2025040914302205.csv`). Product name, description, printer
name, and label template are looked up from the `product` and `printer_config`
tables by product ID.

## Database tables

Each submit appends one test-run row. Result tables: `exb_test` (Base),
`exs_test` (Top), `exss_pcba_test` (PCBA), `chrg_test` (Charge Dock). Assembly
tables holding the pre-assigned `device_sn`: `exb_assembly`, `exs_assembly`,
`chrg_assembly`. Passing tests also stamp `date_of_manufacture` on the `device`
row. All schema/column names are defined as constants near the top of
`UnserializedProductTest.py` — update them there if the database changes.

## Building a standalone executable (optional)

The app detects when it's frozen, so it can be packaged with PyInstaller:

```powershell
pip install pyinstaller
pyinstaller --onefile --noconsole UnserializedProductTest.py
```

Copy `db_config.ini` and `config.ini` next to the generated `.exe`.
