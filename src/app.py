from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import openpyxl
import re, io, os, math, csv, json, zipfile, logging, uuid, time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Line Protection logger ───────────────────────────────────────────────────
_lp_log = logging.getLogger("line_protection")

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*", "methods": ["GET","POST","OPTIONS"], "allow_headers": ["Content-Type"]}})

# ─── VERSION (bump this on every release) ───────────────────────────────────
APP_VERSION = "1.1.10"

ALL_PRC = ["PRC-023", "PRC-025", "PRC-026"]

# ─── SLC "FeederOHL" COF / DVF constants (per SLC_Code.txt) ──────────────────
COF10, COF11, COF15 = 1.0, 1.1, 1.5
DVF85, DVF95         = 0.85, 0.95

# ─── Pre-compile regex patterns (compiled once at module load) ───────────────
_RE_SECTION   = re.compile(r'^\[([A-Za-z0-9_]+)\]')
_RE_KV        = re.compile(r'^([A-Za-z0-9_]+),\s*"([^"]*)"')
_RE_LINE_NAME = re.compile(r'L-\d{3}|LINE\s*#?\d', re.IGNORECASE)
_RE_PORT      = re.compile(r'\b(P[1-5F])\b', re.IGNORECASE)
_RE_NORM      = re.compile(r'\s*-\s*')

_SAFE_LOAD_PATTERNS = [
    re.compile(r'Safe\s*Load\s*=\s*(\d+)', re.IGNORECASE),
    re.compile(r'Minimum\s+Calculated\s+AMP\s+LINE\s+LIMIT\s+Rounded\s*\[A\]\s*=\s*(\d+)', re.IGNORECASE),
    re.compile(r'Min(?:imum)?\s+Calc[^\n]*=\s*(\d+)', re.IGNORECASE),
    re.compile(r'SOTF\s+AMP\s+LINE\s+LIMIT\s*\[A\]\s*=\s*(\d+)', re.IGNORECASE),
]

# COF / DVF as printed in a real SLC trace-log output (IpsTraceLine
# "Current Overload Factor = ..." / "Depressed Voltage Factor = ..."),
# used to surface what the SLC tool itself actually used, when an SLC
# output file is uploaded — see parse_cof_dvf().
_RE_COF = re.compile(r'Current\s+Overload\s+Factor\s*=\s*([\d.]+)', re.IGNORECASE)
_RE_DVF = re.compile(r'Depressed\s+Voltage\s+Factor\s*=\s*([\d.]+)', re.IGNORECASE)

# ─── Top-level import of olefile (avoids re-importing inside hot path) ───────
try:
    import olefile as _olefile
    _OLEFILE_AVAILABLE = True
except ImportError:
    _OLEFILE_AVAILABLE = False

# ─── Two separate thread pools to prevent nested-submit deadlock ─────────────
# _GROUP_EXECUTOR : group-level tasks  (batch_group_check / feeder_check)
# _FILE_EXECUTOR  : file-level tasks   (RDBs, calc, FR within each group)
_cpu            = os.cpu_count() or 4
_GROUP_EXECUTOR = ThreadPoolExecutor(max_workers=min(64, _cpu * 4))
_FILE_EXECUTOR  = ThreadPoolExecutor(max_workers=min(128, _cpu * 8))


def _safe_int(val):
    """Coerce *val* to int, returning None on any conversion failure."""
    if val is None: return None
    try: return int(float(str(val).strip()))
    except: return None

def _safe_float(val):
    """Coerce *val* to float, returning None on any conversion failure."""
    if val is None: return None
    try: return float(str(val).strip())
    except: return None

def extract_rdb_text(raw_bytes):
    """Extract text from RDB file (OLE compound or plain text)."""
    if _OLEFILE_AVAILABLE:
        try:
            parts = []
            ole = _olefile.OleFileIO(io.BytesIO(raw_bytes))
            for entry in ole.listdir():
                try:
                    data = ole.openstream(entry).read()
                    try: parts.append(data.decode("utf-8", errors="replace"))
                    except: parts.append(data.decode("latin-1", errors="replace"))
                except: pass
            ole.close()
            if parts:
                return "\n".join(parts)
        except: pass
    try: return raw_bytes.decode("utf-8", errors="replace")
    except: return raw_bytes.decode("latin-1", errors="replace")

def parse_rdb_sections(content):
    """Parse RDB ini-style sections into a dict."""
    sections, current, settings = {}, None, {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line: continue
        sec = _RE_SECTION.match(line)
        if sec:
            if current is not None: sections[current] = settings
            current, settings = sec.group(1).upper(), {}
            continue
        kv = _RE_KV.match(line)
        if kv and current is not None:
            settings[kv.group(1).upper()] = kv.group(2).strip()
    if current is not None: sections[current] = settings
    return sections


def parse_rdb_sections_by_relay(raw_bytes):
    """
    Relay-aware replacement for parse_rdb_sections(extract_rdb_text(raw)).

    Some .rdb files are combined panel exports that bundle settings for MORE
    THAN ONE physical relay in the same OLE compound file — e.g. a feeder's
    SEL-351S plus a companion SEL-551, each stored under its own
    "Relays/<relay name>/..." storage. Every relay reuses the same bare
    section names ("1".."6", "P1", "R", "T", "INFO", ...), so naively
    concatenating every stream (extract_rdb_text) and parsing the result as
    one flat dict lets whichever relay's stream happens to be read LAST
    silently overwrite an earlier relay's same-named section. In practice
    this meant an SEL-551's empty "1" section (it has no frequency element)
    clobbered the SEL-351S's real Group-1 settings, and E81/81D1P lookups
    then fell through to the SEL-351S's Group 2 by accident — reporting the
    wrong setting group entirely even though Group 1 was requested.

    This groups OLE streams by their relay storage folder (the "<relay
    name>" path segment, which is stable across both single- and
    multi-relay files), parses each relay's streams into its own sections
    dict, and returns the sections from whichever relay actually defines a
    frequency element (E81 in one of its Setting Groups) — that's the relay
    UF/UFLS checks are meant to read from. Falls back to the largest
    relay's sections if none has E81, and to the old flat single-dict
    parse if the file isn't an OLE compound file or only contains one
    relay to begin with — so ordinary single-relay RDBs (the overwhelming
    majority of uploads) are parsed exactly as before.
    """
    if not _OLEFILE_AVAILABLE:
        return parse_rdb_sections(extract_rdb_text(raw_bytes))

    try:
        ole = _olefile.OleFileIO(io.BytesIO(raw_bytes))
    except Exception:
        return parse_rdb_sections(extract_rdb_text(raw_bytes))

    groups = {}
    try:
        for entry in ole.listdir():
            if len(entry) < 2:
                continue
            groups.setdefault(entry[1], []).append(entry)
    except Exception:
        ole.close()
        return parse_rdb_sections(extract_rdb_text(raw_bytes))

    if len(groups) <= 1:
        ole.close()
        return parse_rdb_sections(extract_rdb_text(raw_bytes))

    parsed_groups = []
    for relay_key, entries in groups.items():
        parts = []
        for entry in entries:
            try:
                data = ole.openstream(entry).read()
                try: parts.append(data.decode("utf-8", errors="replace"))
                except: parts.append(data.decode("latin-1", errors="replace"))
            except Exception:
                pass
        parsed_groups.append((relay_key, parse_rdb_sections("\n".join(parts))))
    ole.close()

    def _has_e81(sections):
        return any(sections.get(sec, {}).get("E81") for sec in ("1", "2", "3", "4", "5", "6"))

    freq_capable = [sections for _, sections in parsed_groups if _has_e81(sections)]
    if freq_capable:
        # Prefer the first frequency-capable relay wholesale; only pull in
        # section names that relay doesn't have from the others, so a
        # second relay's same-named section can never clobber the first's.
        merged = {}
        for sections in freq_capable:
            for k, v in sections.items():
                merged.setdefault(k, v)
        return merged

    # No relay in the file has frequency protection — best-effort fallback
    # to the largest relay group's own sections.
    parsed_groups.sort(key=lambda kv: sum(len(v) for v in kv[1].values()), reverse=True)
    return parsed_groups[0][1] if parsed_groups else {}

_RELAY_SHEET_KEYWORDS = ["sel-411l"]

# Full ordered list of SEL-411L settings to cross-verify
# Relay settings check removed — these constants are no longer used
SEL411L_SETTINGS_TO_CHECK = []
_AUTOMATION_KEYS = set()
_RE_ASSIGN = re.compile(r'(?!)')  # never matches — unused

def _read_relay_settings_tab(wb):
    """Find and read the relay settings sheet from workbook."""
    ordered = sorted(
        wb.sheetnames,
        key=lambda s: (
            0 if "421-5" in s.lower() else
            1 if "421-3" in s.lower() else
            2 if "421"   in s.lower() else
            3 if "411l"  in s.lower() else
            4 if "311"   in s.lower() else
            5
        )
    )
    for sname in ordered:
        sl = sname.lower()
        if not any(k in sl for k in _RELAY_SHEET_KEYWORDS):
            continue
        ws = wb[sname]
        d = {}
        # Cap row scan: relay settings never exceed ~800 rows.
        # Stop after 20 consecutive blank rows — settings block is dense.
        _cap        = min(ws.max_row or 800, 800)
        _blank_run  = 0
        for row in ws.iter_rows(min_row=1, max_row=_cap,
                                 min_col=1, max_col=3, values_only=True):
            name = row[0]
            if not name or not isinstance(name, str):
                _blank_run += 1
                if _blank_run >= 20:
                    break
                continue
            _blank_run = 0
            key = name.strip().upper()
            if not key: continue
            val = row[2]
            if isinstance(val, str) and val.startswith("#"): val = None
            d[key] = val
        if "CTRW" in d:
            return d, sname
    return {}, None


def _parse_rdb_automation_settings(rdb_sections):
    """Extract automation settings from PROTSEL lines (L1-L6) and TMB1B from O1."""
    found = {}
    for sec in ["L1","L2","L3","L4","L5","L6"]:
        if sec not in rdb_sections:
            continue
        for _k, val in rdb_sections[sec].items():
            if not _k.startswith("PROTSEL"):
                continue
            line = (val or "").strip()
            m = _RE_ASSIGN.match(line)
            if not m:
                continue
            var_name = m.group(1).upper()
            var_val  = m.group(2).strip()
            if var_name in _AUTOMATION_KEYS and var_name not in found:
                found[var_name] = var_val
        if len(found) >= len(_AUTOMATION_KEYS):
            break

    o1 = rdb_sections.get("O1", {})
    tmb1b_raw = o1.get("TMB1B")
    if tmb1b_raw is not None:
        val = tmb1b_raw.split("#")[0].strip()
        found["TMB1B"] = val if val else tmb1b_raw
    return found


def _relay_vals_equal(calc_raw, rdb_str):
    if calc_raw is None or rdb_str is None:
        return None
    c_str = str(calc_raw).strip()
    r_str = str(rdb_str).strip()
    if not c_str or not r_str:
        return None
    if c_str.startswith("#"):
        return None
    try:
        cv = float(c_str)
        rv = float(r_str)
        return abs(cv - rv) < 0.011
    except ValueError:
        pass
    return c_str.upper() == r_str.upper()

def parse_calc_sheet(file_bytes):
    """
    Parse the Calc Sheet Excel workbook and extract relay-relevant values.

    Reads the 'Data Entry' tab for nominal kV, conductor rating, CTR and
    PTR primary values, and the PRC criteria cell.  Also opens the relay
    settings tab (SEL-411L / SEL-421) to extract CTRW, CTRX, PTRY, PTRZ
    ratios for cross-verification against the RDB file.

    Parameters
    ----------
    file_bytes : bytes
        Raw bytes of the .xlsx workbook.

    Returns
    -------
    dict
        Keys: nominal_kv, calc_ctrw, calc_ctrx, calc_ptry, calc_ptrz,
              ctr_w_primary, ctr_x_primary, ptry, conductor_rating_from_fr,
              prc_criteria_raw, relay_sheet_used, line_name, relay_settings_raw,
              r1_12_check (dict — see check_r1_12_limitation_to_protect_line()).
    """
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    result = {
        "nominal_kv": None, "calc_ctrw": None, "calc_ctrx": None,
        "calc_ptry": None, "calc_ptrz": None, "ctr_w_primary": None,
        "ctr_x_primary": None, "ptry": None, "conductor_rating_from_fr": None,
        "prc_criteria_raw": None, "relay_sheet_used": None,
        "line_name": None, "relay_settings_raw": {},
        "r1_12_check": None,
    }

    de_name = next((s for s in wb.sheetnames if "data entry" in s.lower()), None)
    if de_name:
        ws = wb[de_name]
        def col_e(r): return ws.cell(row=r, column=5).value
        result["nominal_kv"]               = _safe_float(col_e(16))
        result["conductor_rating_from_fr"] = _safe_int(col_e(19))
        result["ctr_w_primary"]            = _safe_int(col_e(22))
        result["ctr_x_primary"]            = _safe_int(col_e(23))
        result["ptry"]                     = _safe_int(col_e(24))
        prc_raw = col_e(34)
        if prc_raw: result["prc_criteria_raw"] = str(prc_raw).strip()

        # PRC-023-3 R1.12 ("Limitation to Protect Line") — Data Entry row 358.
        # "Yes" when any condition is entered on that row, else "No".
        try:
            result["r1_12_check"] = check_r1_12_limitation_to_protect_line(ws)
        except Exception:
            result["r1_12_check"] = None

        for r in range(1, 20):
            for c in range(1, 6):
                v = ws.cell(row=r, column=c).value
                if v and isinstance(v, str) and _RE_LINE_NAME.search(v):
                    result["line_name"] = v.strip()
                    break
            if result["line_name"]:
                break

        need_fallback = (
            result["ctr_w_primary"] is None or
            result["ctr_x_primary"] is None or
            result["ptry"] is None or
            result["conductor_rating_from_fr"] is None
        )
        if need_fallback:
            _de_cap = min(ws.max_row or 300, 300)
            for row in ws.iter_rows(min_row=1, max_row=_de_cap, values_only=True):
                label = " ".join(str(c).lower() for c in row if c)
                nums  = [c for c in row if isinstance(c, (int, float)) and c > 0]
                if not nums: continue
                if result["ctr_w_primary"] is None and ("w winding ct" in label or "ctr  sel 311" in label):
                    result["ctr_w_primary"] = int(max(nums))
                if result["ctr_x_primary"] is None and "x winding ct" in label:
                    result["ctr_x_primary"] = int(max(nums))
                if result["ptry"] is None and "ptr:" in label:
                    result["ptry"] = int(nums[0])
                if result["conductor_rating_from_fr"] is None and "conductor rating from fr" in label:
                    result["conductor_rating_from_fr"] = int(nums[0])
                if all(result[k] is not None for k in (
                    "ctr_w_primary", "ctr_x_primary", "ptry", "conductor_rating_from_fr"
                )):
                    break

    # ── Read relay settings from the SAME open workbook (no second open) ──────
    relay_d, rsheet = _read_relay_settings_tab(wb)
    wb.close()

    result["relay_sheet_used"]   = rsheet
    result["relay_settings_raw"] = relay_d
    if relay_d:
        result["calc_ctrw"] = _safe_int(relay_d.get("CTRW"))
        result["calc_ctrx"] = _safe_int(relay_d.get("CTRX"))
        result["calc_ptry"] = _safe_int(relay_d.get("PTRY"))
        result["calc_ptrz"] = _safe_int(relay_d.get("PTRZ"))

    if result["calc_ctrw"] is None and result["ctr_w_primary"] is not None:
        result["calc_ctrw"] = result["ctr_w_primary"] // 5
    if result["calc_ctrx"] is None and result["ctr_x_primary"] is not None:
        result["calc_ctrx"] = result["ctr_x_primary"] // 5
    if result["calc_ptry"] is None:
        result["calc_ptry"] = result["ptry"]

    return result

def parse_fr_workbook(file_bytes):
    """
    Parse the Facility Rating (FR) workbook to extract the conductor ampere rating.

    Scans Section-2 for 'through' segments and Section-3 for per-location
    conductor AMP values.  The rating returned is the minimum of all
    through-segment conductor maximums (i.e. the limiting conductor).

    Parameters
    ----------
    file_bytes : bytes
        Raw bytes of the FR .xlsx workbook.

    Returns
    -------
    dict
        Keys: fr_rating (int|None), through_segments (list[str]),
              conductor_detail (dict[str, list[int]]).
    """
    wb  = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    ws  = wb.active

    through_segs = set()
    loc_amps     = {}

    in_s2 = False
    in_s3 = False
    last_seg     = None
    current_loc  = None

    for row in ws.iter_rows(values_only=True):
        r0, r1 = row[0], row[1] if len(row) > 1 else None

        if isinstance(r0, int):
            if r0 == 2 and r1 and "limiting" in str(r1).lower():
                in_s2, in_s3 = True, False
                continue
            if r0 == 3 and r1 and "component" in str(r1).lower():
                in_s2, in_s3 = False, True
                continue
            if r0 == 4:
                break

        if in_s2:
            if r0 in ('A','B','C','D','E','F','G','H','I','J','K','Z') and r1:
                last_seg = str(r1).strip()
            for ci in (0, 1):
                if ci < len(row) and row[ci] and str(row[ci]).strip().upper() == "THROUGH":
                    if last_seg: through_segs.add(last_seg)
            continue

        if in_s3:
            loc_val = r1
            if loc_val and isinstance(loc_val, str) and loc_val.strip():
                s = loc_val.strip()
                if s.lower() not in {'station or line', 'line panel', 'model information'}:
                    current_loc = s
            device = str(row[2]).strip() if len(row) > 2 and row[2] else ""
            amp    = row[5] if len(row) > 5 else None
            if "conductor" in device.lower() and isinstance(amp, (int, float)) and amp > 0:
                loc = current_loc or "_unknown_"
                loc_amps.setdefault(loc, []).append(int(amp))

    wb.close()

    def _norm(s): return _RE_NORM.sub('-', str(s).lower().strip())
    through_n = {_norm(t) for t in through_segs}
    through_c = {loc: amps for loc, amps in loc_amps.items() if _norm(loc) in through_n} or loc_amps
    per_max   = {loc: max(amps) for loc, amps in through_c.items()}
    fr_rating = min(per_max.values()) if per_max else None

    return {"fr_rating": fr_rating, "through_segments": sorted(through_segs), "conductor_detail": through_c}

def parse_safe_load(content):
    """
    Extract the Safe Load ampere value from SLC file text.

    Tries each pattern in ``_SAFE_LOAD_PATTERNS`` in order and returns
    the first integer match, or None if nothing is found.
    """
    for pat in _SAFE_LOAD_PATTERNS:
        m = pat.search(content)
        if m: return int(m.group(1))
    return None


def parse_cof_dvf(content):
    """
    Extract the Current Overload Factor and Depressed Voltage Factor that
    the SLC tool itself printed to its trace-log output, if present.

    Returns {"cof": float|None, "dvf": float|None} — either key is None
    if that line wasn't found in *content*.
    """
    cof_m = _RE_COF.search(content) if content else None
    dvf_m = _RE_DVF.search(content) if content else None
    return {
        "cof": float(cof_m.group(1)) if cof_m else None,
        "dvf": float(dvf_m.group(1)) if dvf_m else None,
    }


def determine_cof_dvf(nominal_kv, prc_info, r1_12_status):
    """
    Determine the Current Overload Factor (COF) and Depressed Voltage
    Factor (DVF) to use in the SOTF / ZLE / Zn amp-limit formulas, per
    the real SLC tool's "FeederOHL" (transmission-line) branch — see
    SLC_Code.txt around the "Related to Relay Line Limit Calculation
    Workbook" comment:

        VLine > 200 kV:
            R1.12 == "Yes"  ->  COF = 1.0, DVF = 0.85   (R1.12 relief)
            R1.12 == "No"   ->  COF = 1.5, DVF = 0.85
        VLine <= 200 kV:
            PRC-023 applicable, R1.12 == "No"   ->  COF = 1.5, DVF = 0.85
            PRC-023 applicable, R1.12 == "Yes"  ->  COF = 1.0, DVF = 0.85
            PRC-023 NOT applicable              ->  COF = 1.1, DVF = 0.95

    Parameters
    ----------
    nominal_kv : float | None
        System/line voltage in kV (VLine in the SLC tool).
    prc_info : dict
        Output of classify_prc() — only its 'primary' key is used, to
        tell whether PRC-023 (as opposed to PRC-025/026/other/none) is
        the criteria selected for this line on the Calc Sheet.
    r1_12_status : str | None
        "Yes" / "No" from check_r1_12_limitation_to_protect_line()
        (calc_data['r1_12_check']['status']). None/unknown is treated
        as "No" — the conservative branch (no R1.12 relief granted).

    Returns
    -------
    dict: cof (float|None), dvf (float|None), is_prc23 (bool|None),
          vline_band ("> 200 kV" | "<= 200 kV" | None), basis (str).
    """
    if nominal_kv is None:
        return {"cof": None, "dvf": None, "is_prc23": None,
                "vline_band": None,
                "basis": "System kV unavailable — cannot determine COF/DVF"}

    prc23_applicable = (prc_info or {}).get("primary") == "023"
    r1c12 = "YES" if (r1_12_status == "Yes") else "NO"

    if nominal_kv > 200:
        vline_band = "> 200 kV"
        if r1c12 == "YES":
            cof, dvf, is_prc23 = COF10, DVF85, True
            basis = "VLine > 200 kV, PRC-023 R1.12 relief applied"
        else:
            cof, dvf, is_prc23 = COF15, DVF85, True
            basis = "VLine > 200 kV"
    else:
        vline_band = "<= 200 kV"
        if prc23_applicable and r1c12 == "NO":
            cof, dvf, is_prc23 = COF15, DVF85, True
            basis = "VLine <= 200 kV, PRC-023 applicable"
        elif prc23_applicable and r1c12 == "YES":
            cof, dvf, is_prc23 = COF10, DVF85, True
            basis = "VLine <= 200 kV, PRC-023 applicable, R1.12 relief applied"
        else:
            cof, dvf, is_prc23 = COF11, DVF95, False
            basis = "VLine <= 200 kV, PRC-023 not applicable"

    return {"cof": cof, "dvf": dvf, "is_prc23": is_prc23,
            "vline_band": vline_band, "basis": basis}


def compute_sotf_amp_limit(rdb_sections, cof=COF15):
    """
    Compute the SOTF (Switch-On-To-Fault) amp line limit directly from RDB
    relay settings, without needing an SLC file.

    CONFIRMED formula (exact match verified against a real SLC 'Formula
    Safe Load' output — Audubon-Conway L-653, 2960 A):

        SOTF_AMP_LIMIT = (50P3P secondary pickup * CTRW) / COF

    where COF is the Current Overload Factor — NOT a fixed 1.5. Per the
    real SLC tool (SLC_Code.txt, FeederOHL branch), COF varies with the
    line's voltage class, PRC-023 applicability, and PRC-023 R1.12
    ("Limitation to Protect Line") relief — see determine_cof_dvf().
    Callers should compute COF via determine_cof_dvf() and pass it in;
    the default of 1.5 here is only a fallback for standalone use.

    NOTE: this covers ONLY the SOTF element of the Safe Load calc. The
    SLC tool's ZLE (Load Encroachment) and Zn (most-limiting distance
    zone) amp limits use a different, unconfirmed formula and are NOT
    computed here — see manual_zle_amp / manual_zn_amp in _run_checks.

    Parameters
    ----------
    rdb_sections : dict
    cof : float
        Current Overload Factor to use — see determine_cof_dvf().

    Returns the SOTF amp limit (float, unrounded) or None if 50P3P/CTRW
    aren't present in any of the S1-S6 relay groups.
    """
    for gname in ["S1", "S2", "S3", "S4", "S5", "S6", "G1"]:
        s = rdb_sections.get(gname)
        if not s:
            continue
        p50p3 = _safe_float(s.get("50P3P"))
        ctrw  = _safe_float(s.get("CTRW"))
        if p50p3 is not None and ctrw is not None:
            return (p50p3 * ctrw) / cof
    return None


_ZONE_REACH_KEYS = [("Z1MP", None), ("Z2MP", None), ("Z3MP", "DIR3"),
                    ("Z4MP", "DIR4"), ("Z5MP", "DIR5")]


def _most_limiting_forward_zone(s):
    """
    Find the forward-looking distance zone with the LARGEST secondary-ohm
    reach in relay settings group *s* — i.e. the zone that would be reached
    into by the smallest amount of load current (the "most limiting" zone
    for loadability purposes, matching the real SLC tool's "Most Limiting
    Zone Element" selection).

    Zones set to a reverse direction (DIR3/DIR4/DIR5 = 'R') are excluded,
    since a reverse-looking zone can't be encroached upon by forward load
    current. Z1/Z2 have no DIR setting on the SEL-411L (always forward) so
    are always included when present and numeric.

    Returns (zone_label, reach_ohms_secondary) for the largest qualifying
    reach found, or (None, None) if no usable zone reach is present.
    """
    best_label, best_val = None, None
    for mp_key, dir_key in _ZONE_REACH_KEYS:
        val = _safe_float(s.get(mp_key))
        if val is None:
            continue
        if dir_key:
            d = (s.get(dir_key) or "").strip().upper()
            if d.startswith("R"):
                continue
        if best_val is None or val > best_val:
            best_label, best_val = f"ZONE {mp_key[1]}", val
    return best_label, best_val


def _derive_nominal_kv(rdb_sections, calc_data):
    """
    System nominal line-to-line voltage in kV, needed by
    compute_zle_amp_limit() and compute_zn_amp_limit().

    Prefers the Calc Sheet's Data Entry value (calc_data['nominal_kv']);
    when no Calc Sheet is available, falls back to the RDB's own rated
    CT/PT circuit voltage — 87VTWL / 87VTXL / 87VTCC, the differential
    element's rated line-to-line voltage, which is set to the project's
    system kV (confirmed: 87VTWL=230.00 matches the real SLC tool's
    VLine=230 input on Audubon-Conway L-388). This lets ZLE/Zn auto-
    calculate from the RDB alone, with no Calc Sheet or manual entry
    required.
    """
    nom_kv = calc_data.get("nominal_kv") if calc_data else None
    if nom_kv:
        return nom_kv
    if not rdb_sections:
        return None
    for gname in ["S1", "S2", "S3", "S4", "S5", "S6", "G1"]:
        s = rdb_sections.get(gname)
        if not s:
            continue
        for key in ("87VTWL", "87VTXL", "87VTCC"):
            v = _safe_float(s.get(key))
            if v:
                return v
    return None


def compute_zle_amp_limit(rdb_sections, nominal_kv, cof=COF15, dvf=None):
    """
    Compute the ZLE (Load Encroachment) amp line limit directly from RDB
    relay settings, without needing an SLC file.

    CONFIRMED formula (exact match, to floating-point precision, verified
    against real SLC 'Formula Safe Load' output — Audubon-Conway L-388,
    both P1 and P2: ZLE AMP LINE LIMIT = 2763.08390762722 A):

        ZLE_AMP_LIMIT = (Vphase * DVF) / (ZLF_primary * COF)

    where:
        Vphase      = nominal_kv * 1000 / sqrt(3)
        ZLF_primary = ZLF (RDB Load Encroachment reach, secondary ohms)
                      * (PTRY / CTRW)
        DVF, COF    = Depressed Voltage Factor / Current Overload Factor.
                      NOT fixed values — per the real SLC tool
                      (SLC_Code.txt, FeederOHL branch) these vary with
                      line voltage class, PRC-023 applicability, and
                      PRC-023 R1.12 relief. Callers should compute them
                      via determine_cof_dvf() and pass them in.

    Parameters
    ----------
    rdb_sections : dict
    nominal_kv : float
    cof : float
        Current Overload Factor — see determine_cof_dvf().
    dvf : float | None
        Depressed Voltage Factor — see determine_cof_dvf(). If None
        (standalone use with no PRC/R1.12 context available), falls
        back to the legacy nominal_kv-only convention: 0.85 for
        nominal_kv >= 100, else 0.913.

    Returns the ZLE amp limit (float, unrounded) or None if ZLF, CTRW, or
    PTRY aren't present in any S1-S6/G1 relay group, or nominal_kv is
    unknown.
    """
    if not rdb_sections or not nominal_kv:
        return None
    if dvf is None:
        dvf = 0.913 if nominal_kv < 100 else 0.85
    for gname in ["S1", "S2", "S3", "S4", "S5", "S6", "G1"]:
        s = rdb_sections.get(gname)
        if not s:
            continue
        zlf  = _safe_float(s.get("ZLF"))
        ctrw = _safe_float(s.get("CTRW"))
        ptry = _safe_float(s.get("PTRY")) or _safe_float(s.get("PTR"))
        if zlf is not None and ctrw and ptry:
            zlf_primary = zlf * (ptry / ctrw)
            vphase = nominal_kv * 1000 / math.sqrt(3)
            return (vphase * dvf) / (zlf_primary * cof)
    return None


def compute_zn_amp_limit(rdb_sections, nominal_kv, cof=COF15, dvf=None):
    """
    Compute the Zn (most-limiting forward distance zone) amp line limit
    directly from RDB relay settings, without needing an SLC file.

    CONFIRMED formula (exact match, to floating-point precision, verified
    against real SLC 'Formula Safe Load' output — Audubon-Conway L-388,
    both P1 and P2: Zn AMP LINE LIMIT = 2563.63007407425 A, Zone 4
    most-limiting):

        Zn_AMP_LIMIT = (Vphase * DVF) / (COF * Zn_primary * cos(Z1ANG - 30°))

    where:
        Zn_primary = largest forward-zone reach, secondary ohms (see
                     _most_limiting_forward_zone) * (PTRY / CTRW)
        Z1ANG      = relay mho characteristic angle (MTA), degrees
        30°        = fixed NERC PRC-023 loadability power-factor-angle
                     assumption — NOT the relay's own PLAF load-
                     encroachment angle setting (verified against real
                     SLC output; PLAF=40° on this relay had no effect on
                     the actual Zn calc, only the 30° distance is used)
        DVF, COF   = same as in compute_zle_amp_limit() — vary with line
                     voltage class / PRC-023 / R1.12; see
                     determine_cof_dvf().

    Parameters
    ----------
    rdb_sections : dict
    nominal_kv : float
    cof : float
        Current Overload Factor — see determine_cof_dvf().
    dvf : float | None
        Depressed Voltage Factor — see determine_cof_dvf(). If None,
        falls back to the legacy nominal_kv-only convention (same as
        compute_zle_amp_limit()).

    Returns (amp_limit, zone_label) — e.g. (2563.63, "ZONE 4") — or
    (None, None) if the needed settings aren't present, nominal_kv is
    unknown, or the angle term is degenerate (cos = 0).
    """
    if not rdb_sections or not nominal_kv:
        return None, None
    if dvf is None:
        dvf = 0.913 if nominal_kv < 100 else 0.85
    for gname in ["S1", "S2", "S3", "S4", "S5", "S6", "G1"]:
        s = rdb_sections.get(gname)
        if not s:
            continue
        ctrw  = _safe_float(s.get("CTRW"))
        ptry  = _safe_float(s.get("PTRY")) or _safe_float(s.get("PTR"))
        z1ang = _safe_float(s.get("Z1ANG"))
        zone_label, zn_sec = _most_limiting_forward_zone(s)
        if ctrw and ptry and z1ang is not None and zn_sec is not None:
            zn_primary = zn_sec * (ptry / ctrw)
            cos_term = math.cos(math.radians(z1ang - 30))
            if not cos_term:
                return None, None
            vphase = nominal_kv * 1000 / math.sqrt(3)
            amp = (vphase * dvf) / (cof * zn_primary * cos_term)
            return amp, zone_label
    return None, None


def _eload_enabled(rdb_sections):
    """
    Whether the relay's Load Encroachment supervision (ELOAD) is enabled.
    Read from the same S1-S6/G1 relay group search order used elsewhere.
    Returns True/False, or None if no group has an ELOAD setting at all.
    """
    if not rdb_sections:
        return None
    for gname in ["S1", "S2", "S3", "S4", "S5", "S6", "G1"]:
        s = rdb_sections.get(gname)
        if not s:
            continue
        v = s.get("ELOAD")
        if v is not None:
            return str(v).strip().upper() == "Y"
    return None


def combine_distance_element_limit(zn_amp, zle_amp, eload_on):
    """
    Combine the Zn (zone-reach) and ZLE (Load Encroachment) amp limits into
    the single "Distance Element" amp limit the way the real SLC tool does
    — CONFIRMED against real output (Audubon-Conway L-388: Zn=2563.63 A,
    ZLE=2763.08 A, ELOAD=Y → "Distance element AMP limit determined by: LE",
    value 2763.08 A, i.e. the LARGER of the two, not the smaller).

    This is intentionally the MAX, not the MIN: when Load Encroachment is
    enabled it supervises (blocks) the distance zones from operating on
    load current, so the zones can't misoperate on load below whichever of
    Zn / ZLE is larger — that larger value is what actually governs safe
    loading for the distance-protection element as a whole. When ELOAD is
    explicitly disabled, that supervision doesn't exist, so only the raw
    zone reach (Zn) applies and ZLE is excluded from the comparison.

    Returns (value, label) where label is "ZLE" or "Zn" (whichever governs),
    or (None, None) if neither input is available.
    """
    if eload_on is False:
        zle_amp = None
    if zn_amp is not None and zle_amp is not None:
        return (zle_amp, "ZLE") if zle_amp >= zn_amp else (zn_amp, "Zn")
    if zle_amp is not None:
        return zle_amp, "ZLE"
    if zn_amp is not None:
        return zn_amp, "Zn"
    return None, None


def _round_safe_load(x):
    """
    Round a raw amp-limit value down to the Safe Load convention observed
    in real SLC output: strictly less than the limiting value, e.g. an
    exact 2960 A limit rounds DOWN to 2959 A (never equal to the trip
    threshold). Equivalent to ceil(x) - 1 for both integer and
    non-integer inputs.
    """
    return math.ceil(x) - 1


def classify_prc(raw):
    """
    Classify a raw PRC criteria string into a structured dict.

    Returns a dict with keys: selected, label, not_applicable, primary.
    'primary' is one of '023', '025', '026', 'other', or 'none'.
    """
    if not raw:
        return {"selected": None, "label": "Not set", "not_applicable": list(ALL_PRC), "primary": "none"}
    r = raw.upper()
    if "023" in r:
        return {"selected": "PRC-023", "label": "PRC-023", "not_applicable": ["PRC-025", "PRC-026"], "primary": "023"}
    if "026" in r:
        return {"selected": "PRC-026", "label": "PRC-026", "not_applicable": ["PRC-023", "PRC-025"], "primary": "026"}
    if "025" in r:
        return {"selected": "PRC-025", "label": "PRC-025", "not_applicable": ["PRC-023", "PRC-026"], "primary": "025"}
    return {"selected": raw.strip(), "label": raw.replace("_", " ").strip(),
            "not_applicable": [p for p in ALL_PRC if p not in r], "primary": "other"}


# ─── PRC-023-3 "Select An Alternative Requirement" table (Data Entry tab) ────
# Row 347 is the header; each requirement (R1.2, R1.3.1, ... R1.13) occupies
# one row below it, label in column AA, data spread across columns AB:AK
# (# of 2nd Lines / # of Tap lines / # of breakers / # of Autos /
# # of Distribution XFMRs, per the row-347 header). A requirement is
# considered selected/populated for the line under review when ANY of its
# data columns is filled in — confirmed against real Calc Sheets, where an
# unused requirement row (e.g. R1.11, R1.12, R1.13) is left completely blank
# while the row actually governing the line (e.g. R1.3.1) has values in
# every applicable column.
_R1_LABEL_COL   = 27          # AA
_R1_DATA_COLS   = range(28, 38)   # AB .. AK (label column AA excluded)

_PRC023_R1_ROWS = {
    "R1.2":   348, "R1.3.1": 349, "R1.3.2": 350, "R1.4":   351,
    "R1.5":   352, "R1.7":   353, "R1.8":   354, "R1.9":   355,
    "R1.10":  356, "R1.11":  357, "R1.12":  358, "R1.13":  359,
}


def _row_has_conditions(ws, row, cols=_R1_DATA_COLS):
    """
    True if any cell in *row* across *cols* on worksheet *ws* holds a
    non-blank value (numbers, text, or 0 all count; None / "" do not).
    """
    values = {}
    for c in cols:
        v = ws.cell(row=row, column=c).value
        if v is not None and str(v).strip() != "":
            values[openpyxl.utils.get_column_letter(c)] = v
    return bool(values), values


def check_prc023_r1_requirement(ws, key):
    """
    Check a single PRC-023-3 alternative-requirement row (e.g. "R1.12") on
    the Data Entry tab and report whether it has any conditions entered.

    Parameters
    ----------
    ws : openpyxl worksheet
        The 'Data Entry' worksheet (data_only=True).
    key : str
        One of the keys in _PRC023_R1_ROWS, e.g. "R1.12".

    Returns
    -------
    dict: row, label (raw AA text), key, has_conditions (bool),
          status ("Yes"/"No"), values (populated cell letter -> value).
    """
    row = _PRC023_R1_ROWS.get(key)
    if row is None:
        return {"row": None, "label": None, "key": key,
                "has_conditions": False, "status": "No", "values": {}}
    label = ws.cell(row=row, column=_R1_LABEL_COL).value
    has_conditions, values = _row_has_conditions(ws, row)
    return {
        "row": row,
        "label": str(label).strip() if label else key,
        "key": key,
        "has_conditions": has_conditions,
        "status": "Yes" if has_conditions else "No",
        "values": values,
    }


def check_r1_12_limitation_to_protect_line(ws):
    """
    Convenience wrapper for R1.12 ("Limitation to Protect Line"), Data Entry
    row 358. Returns "Yes" if any condition is entered on that row, else "No".
    """
    return check_prc023_r1_requirement(ws, "R1.12")


def nums_equal(a, b):
    """Return True when *a* and *b* are numerically equal within 0.01 tolerance."""
    try: return abs(float(a) - float(b)) < 0.01
    except: return False

def _extract_port(name):
    """Extract SEL port designator (P1-P5, PF) from a filename or string."""
    m = _RE_PORT.search(name or '')
    return m.group(1).upper() if m else None


def _run_checks(calc_data, rdb_sections, fr_result, slc_content,
                calc_name=None, rdb_name=None, fr_name=None, slc_name=None,
                manual_zle_amp=None, manual_zn_amp=None, manual_fr_rating=None):
    results, all_errors = {}, []

    mismatches = {"ctr_ptr": [], "conductor": [], "prc": []}
    results["file_mismatches"] = mismatches

    if calc_data or rdb_sections:
        try:
            s1 = next((rdb_sections[n] for n in ["S1","S2","S3","S4","S5","S6"] if n in rdb_sections), {})
            rdb_ctrw = _safe_int(s1.get("CTRW"))
            rdb_ctrx = _safe_int(s1.get("CTRX"))
            rdb_ptry = _safe_int(s1.get("PTRY")) or _safe_int(s1.get("PTR"))
            rdb_ptrz = _safe_int(s1.get("PTRZ"))
            c_ctrw = calc_data.get("calc_ctrw") if calc_data else None
            c_ctrx = calc_data.get("calc_ctrx") if calc_data else None
            c_ptry = calc_data.get("calc_ptry") if calc_data else None
            c_ptrz = calc_data.get("calc_ptrz") if calc_data else None
            e22 = calc_data.get("ctr_w_primary") if calc_data else None
            e23 = calc_data.get("ctr_x_primary") if calc_data else None
            nom_kv      = calc_data.get("nominal_kv")      if calc_data else None
            relay_sheet = calc_data.get("relay_sheet_used") if calc_data else None
            kv_str      = f"{int(nom_kv)} kV" if nom_kv else "the system"
            ctrw_ok = nums_equal(rdb_ctrw, c_ctrw) if (rdb_ctrw is not None and c_ctrw is not None) else None
            ctrx_ok = nums_equal(rdb_ctrx, c_ctrx) if (rdb_ctrx is not None and c_ctrx is not None) else None
            ptry_ok = nums_equal(rdb_ptry, c_ptry)  if (rdb_ptry is not None and c_ptry is not None) else None
            ptrz_ok = nums_equal(rdb_ptrz, c_ptrz)  if (rdb_ptrz is not None and c_ptrz is not None) else None
            ctrw_x5    = (rdb_ctrw * 5) if rdb_ctrw is not None else None
            ctrx_x5    = (rdb_ctrx * 5) if rdb_ctrx is not None else None
            ctrw_x5_ok = nums_equal(ctrw_x5, e22) if (ctrw_x5 is not None and e22 is not None) else None
            ctrx_x5_ok = nums_equal(ctrx_x5, e23) if (ctrx_x5 is not None and e23 is not None) else None
            ptry_warn = (f"Expected PTR for {kv_str} system is {c_ptry}") if ptry_ok is False and c_ptry is not None else None
            ptrz_warn = (f"Expected PTRZ for {kv_str} system is {c_ptrz}") if ptrz_ok is False and c_ptrz is not None else None
            passed = all(v is True or v is None for v in [ctrw_ok, ctrx_ok, ptry_ok, ptrz_ok, ctrw_x5_ok, ctrx_x5_ok])
            has_name_mismatch_ctr = bool(mismatches["ctr_ptr"])
            results["ctr_ptr_check"] = {
                "pass": passed and not has_name_mismatch_ctr, "relay_sheet": relay_sheet,
                "rdb_ctrw": rdb_ctrw, "calc_ctrw": c_ctrw, "ctrw_ok": ctrw_ok,
                "rdb_ctrx": rdb_ctrx, "calc_ctrx": c_ctrx, "ctrx_ok": ctrx_ok,
                "rdb_ptry": rdb_ptry, "calc_ptry": c_ptry, "ptry_ok": ptry_ok, "ptry_warning": ptry_warn,
                "rdb_ptrz": rdb_ptrz, "calc_ptrz": c_ptrz, "ptrz_ok": ptrz_ok, "ptrz_warning": ptrz_warn,
                "ctrw_x5": ctrw_x5, "e22": e22, "ctrw_x5_ok": ctrw_x5_ok,
                "ctrx_x5": ctrx_x5, "e23": e23, "ctrx_x5_ok": ctrx_x5_ok,
                "name_mismatches": mismatches["ctr_ptr"],
            }
            if ctrw_ok    is False: all_errors.append(f"CTRW mismatch — RDB={rdb_ctrw}, Calc ({relay_sheet})={c_ctrw}")
            if ctrx_ok    is False: all_errors.append(f"CTRX mismatch — RDB={rdb_ctrx}, Calc ({relay_sheet})={c_ctrx}")
            if ptry_ok    is False: all_errors.append(f"PTRY mismatch — RDB={rdb_ptry}, Calc={c_ptry}. {ptry_warn or ''}")
            if ptrz_ok    is False: all_errors.append(f"PTRZ mismatch — RDB={rdb_ptrz}, Calc ({relay_sheet})={c_ptrz}")
            if ctrw_x5_ok is False: all_errors.append(f"CTRW×5 mismatch — RDB CTRW×5={ctrw_x5}, Data Entry E22={e22}")
            if ctrx_x5_ok is False: all_errors.append(f"CTRX×5 mismatch — RDB CTRX×5={ctrx_x5}, Data Entry E23={e23}")
            for mm in mismatches["ctr_ptr"]: all_errors.append(mm)
        except Exception as e:
            results["ctr_ptr_check"] = {"pass": False, "error": str(e)}
            all_errors.append(f"CTR/PTR check failed: {e}")
    else:
        results["ctr_ptr_check"] = {"pass": None, "message": "Calc Sheet and RDB file not available"}

    if rdb_sections:
        port_details, port_errors, ports_in_use = [], [], []
        for pname in ["P1","P2","P3","P4","P5","PF"]:
            if pname not in rdb_sections: continue
            s       = rdb_sections[pname]
            eport   = s.get("EPORT",   "N").strip().upper()
            proto   = s.get("PROTO",   "").strip().upper()
            timeout = s.get("TIMEOUT", "").strip()
            speed   = s.get("SPEED",   "").strip()
            entry   = {"port": pname, "eport": eport, "proto": proto,
                       "timeout": timeout, "speed": speed, "checked": eport == "Y", "pass": True}
            ports_in_use.append(pname)
            if eport == "Y":
                if proto == "SEL":
                    bad = timeout.upper() in ("0", "OFF", "")
                    entry["pass"] = not bad
                    if bad:
                        msg = (f"Port {pname}: EPORT=Y, PROTO=SEL but TIMEOUT='{timeout}'"
                               f" (must not be 0 or OFF when PROTO=SEL)")
                        port_errors.append(msg); all_errors.append(msg)
                else:
                    entry["pass"] = True
            port_details.append(entry)
        results["port_check"] = {
            "pass": (len(port_errors) == 0) if port_details else None,
            "files": port_details, "errors": port_errors,
            "ports_in_use": ("Ports in use: " + ", ".join(ports_in_use)) if ports_in_use else "No active ports found in RDB",
        }
    else:
        results["port_check"] = {"pass": None, "message": "RDB file not uploaded"}

    conductor_rating_to_use = None
    # Temp-settings jobs have no issued FR Workbook, so the FR conductor
    # rating can instead be typed in manually on screen. Only used as a
    # fallback when an actual FR Workbook wasn't uploaded/parsed.
    fr_is_manual = False
    effective_fr_rating = None
    if fr_result is not None:
        effective_fr_rating = fr_result.get("fr_rating")
    elif manual_fr_rating is not None:
        effective_fr_rating = manual_fr_rating
        fr_is_manual = True

    if (fr_result is not None or fr_is_manual) and calc_data:
        try:
            fr_rating   = effective_fr_rating
            calc_rating = calc_data.get("conductor_rating_from_fr")
            match = fr_rating is not None and calc_rating is not None and fr_rating == calc_rating
            conductor_rating_to_use = calc_rating if match else (calc_rating or fr_rating)
            has_name_mismatch_cond = bool(mismatches["conductor"])
            results["conductor_check"] = {
                "pass": match and not has_name_mismatch_cond,
                "fr_rating": fr_rating, "calc_rating": calc_rating,
                "fr_source": "manual" if fr_is_manual else "workbook",
                "name_mismatches": mismatches["conductor"],
            }
            if not match:
                src_label = "Manual FR Rating" if fr_is_manual else "FR Workbook"
                all_errors.append(f"Conductor rating mismatch — {src_label}={fr_rating} A, Calc Sheet={calc_rating} A")
            for mm in mismatches["conductor"]: all_errors.append(mm)
        except Exception as e:
            results["conductor_check"] = {"pass": False, "error": str(e)}
            all_errors.append(f"Conductor rating check failed: {e}")
    elif fr_result is None and not fr_is_manual:
        results["conductor_check"] = {"pass": None, "message": "FR Workbook not uploaded"}
        if calc_data:
            conductor_rating_to_use = calc_data.get("conductor_rating_from_fr")
    else:
        results["conductor_check"] = {"pass": None, "message": "Calc Sheet not available"}

    if slc_content is not None:
        try:
            safe_load = parse_safe_load(slc_content)
            cr        = conductor_rating_to_use
            prc_raw   = calc_data.get("prc_criteria_raw") if calc_data else None
            prc_info  = classify_prc(prc_raw or "")
            primary   = prc_info.get("primary", "none")
            r1_12     = calc_data.get("r1_12_check") if calc_data else None
            has_name_mismatch_prc = bool(mismatches["prc"])

            # COF/DVF actually used by the SLC tool run, read straight from
            # its trace-log output where possible; fall back to the
            # expected value from determine_cof_dvf() (same FeederOHL
            # branch logic) so there's always something to compare, and
            # flag a mismatch if the SLC output disagrees with what the
            # Calc Sheet's PRC/R1.12 selection implies it should be.
            nominal_kv_slc = _derive_nominal_kv(rdb_sections, calc_data)
            expected_cof_dvf = determine_cof_dvf(nominal_kv_slc, prc_info,
                                                  (r1_12 or {}).get("status"))
            parsed_cof_dvf = parse_cof_dvf(slc_content)
            cof_dvf = {
                "cof": parsed_cof_dvf["cof"] if parsed_cof_dvf["cof"] is not None else expected_cof_dvf["cof"],
                "dvf": parsed_cof_dvf["dvf"] if parsed_cof_dvf["dvf"] is not None else expected_cof_dvf["dvf"],
                "source": "SLC output" if (parsed_cof_dvf["cof"] is not None or parsed_cof_dvf["dvf"] is not None) else "expected (computed)",
                "expected": expected_cof_dvf,
                "vline_band": expected_cof_dvf["vline_band"],
                "basis": expected_cof_dvf["basis"],
            }
            cof_dvf_mismatch = (
                parsed_cof_dvf["cof"] is not None and expected_cof_dvf["cof"] is not None and
                not nums_equal(parsed_cof_dvf["cof"], expected_cof_dvf["cof"])
            ) or (
                parsed_cof_dvf["dvf"] is not None and expected_cof_dvf["dvf"] is not None and
                not nums_equal(parsed_cof_dvf["dvf"], expected_cof_dvf["dvf"])
            )

            if safe_load is None:
                results["safe_load_check"] = {
                    "pass": False, "message": "Could not parse Safe Load value from SLC file",
                    "prc_label": prc_info["label"], "prc_info": prc_info, "r1_12_check": r1_12,
                    "name_mismatches": mismatches["prc"], "cof_dvf": cof_dvf,
                }
                all_errors.append("Safe Load value not found in SLC file.")
            elif cr is None:
                results["safe_load_check"] = {
                    "pass": None, "safe_load": safe_load,
                    "message": "Conductor rating unavailable for comparison",
                    "prc_label": prc_info["label"], "prc_info": prc_info, "r1_12_check": r1_12,
                    "name_mismatches": mismatches["prc"], "cof_dvf": cof_dvf,
                }
            else:
                passed    = safe_load >= cr and not has_name_mismatch_prc
                load_line = f"Safe Load ({safe_load} A) {'≥' if safe_load >= cr else '<'} Conductor Rating ({cr} A)"
                results["safe_load_check"] = {
                    "pass": passed, "safe_load": safe_load, "conductor_rating": cr,
                    "prc_label": prc_info["label"], "prc_not_applicable": prc_info["not_applicable"],
                    "prc_info": prc_info, "load_line": load_line, "r1_12_check": r1_12,
                    "name_mismatches": mismatches["prc"], "cof_dvf": cof_dvf,
                }
                if safe_load < cr:
                    all_errors.append(f"Safe Load ({safe_load} A) < Conductor Rating ({cr} A)")
                if cof_dvf_mismatch:
                    all_errors.append(
                        f"COF/DVF mismatch — SLC output used COF={parsed_cof_dvf['cof']}, "
                        f"DVF={parsed_cof_dvf['dvf']}, but PRC/R1.12 selection on the Calc "
                        f"Sheet implies COF={expected_cof_dvf['cof']}, DVF={expected_cof_dvf['dvf']} "
                        f"({expected_cof_dvf['basis']})."
                    )
                for mm in mismatches["prc"]: all_errors.append(mm)
        except Exception as e:
            results["safe_load_check"] = {"pass": False, "error": str(e)}
            all_errors.append(f"Safe Load check failed: {e}")
    else:
        # No SLC file — auto-compute SOTF, ZLE, and Zn directly from the RDB
        # (confirmed formulas — see compute_sotf_amp_limit(), 
        # compute_zle_amp_limit(), compute_zn_amp_limit()). No manual entry
        # is required; manual_zle_amp / manual_zn_amp (if the user still
        # supplies them) simply OVERRIDE the auto-calculated value for that
        # element, e.g. to match an already-run SLC tool exactly.
        try:
            nominal_kv = _derive_nominal_kv(rdb_sections, calc_data)

            # PRC / R1.12 classification is needed BEFORE the amp-limit
            # formulas run, since COF and DVF both depend on it (see
            # determine_cof_dvf() — mirrors the SLC tool's own FeederOHL
            # branch, which decides COF/DVF from VLine, PRC-023
            # applicability, and R1.12 status before computing SOTF/ZLE/Zn).
            cr = conductor_rating_to_use
            prc_raw  = calc_data.get("prc_criteria_raw") if calc_data else None
            prc_info = classify_prc(prc_raw or "")
            r1_12    = calc_data.get("r1_12_check") if calc_data else None
            r1_12_status = (r1_12 or {}).get("status")
            has_name_mismatch_prc = bool(mismatches["prc"])

            cof_dvf = determine_cof_dvf(nominal_kv, prc_info, r1_12_status)
            cof = cof_dvf["cof"] if cof_dvf["cof"] is not None else COF15
            dvf = cof_dvf["dvf"]   # None is fine — compute_zle/zn_amp_limit fall back

            auto_sotf = compute_sotf_amp_limit(rdb_sections, cof=cof) if rdb_sections else None
            auto_zle  = compute_zle_amp_limit(rdb_sections, nominal_kv, cof=cof, dvf=dvf) if rdb_sections else None
            auto_zn, zn_zone = compute_zn_amp_limit(rdb_sections, nominal_kv, cof=cof, dvf=dvf) if rdb_sections else (None, None)
            eload_on  = _eload_enabled(rdb_sections) if rdb_sections else None

            limits = {}
            if auto_sotf is not None:
                limits["SOTF"] = auto_sotf
            if manual_zle_amp is not None:
                limits["ZLE"] = manual_zle_amp
            elif auto_zle is not None:
                limits["ZLE"] = auto_zle
            if manual_zn_amp is not None:
                limits["Zn"] = manual_zn_amp
            elif auto_zn is not None:
                limits["Zn"] = auto_zn

            if not limits:
                missing_kv = " (system kV unavailable — upload a Calc Sheet, or the RDB's 87VTWL/87VTXL/87VTCC was not found)" if not nominal_kv else ""
                results["safe_load_check"] = {
                    "pass": None,
                    "message": ("SLC file not uploaded, and SOTF/ZLE/Zn could not be "
                                "auto-calculated from the RDB" + missing_kv + "."),
                    "prc_label": prc_info["label"], "prc_info": prc_info, "r1_12_check": r1_12,
                    "cof_dvf": cof_dvf,
                }
            else:
                # Combine Zn + ZLE into the single "Distance Element" limit
                # the way the real SLC tool does (MAX of the two, gated by
                # ELOAD — see combine_distance_element_limit()), THEN take
                # the min against SOTF. This is NOT a plain min() across all
                # three — that would under-state Safe Load whenever Load
                # Encroachment supervision is protecting the zone reach.
                dist_val, dist_label = combine_distance_element_limit(
                    limits.get("Zn"), limits.get("ZLE"), eload_on)
                sotf_val = limits.get("SOTF")
                if dist_val is not None and sotf_val is not None:
                    if sotf_val <= dist_val:
                        raw_min, limiting_name = sotf_val, "SOTF"
                    else:
                        raw_min, limiting_name = dist_val, dist_label
                elif dist_val is not None:
                    raw_min, limiting_name = dist_val, dist_label
                else:
                    raw_min, limiting_name = sotf_val, "SOTF"
                safe_load      = _round_safe_load(raw_min)
                complete       = "ZLE" in limits and "Zn" in limits and "SOTF" in limits
                note = (None if complete else
                        "Partial calculation — " +
                        ", ".join(n for n in ["SOTF", "ZLE", "Zn"] if n not in limits) +
                        " could not be auto-calculated from the RDB (and wasn't manually "
                        "overridden). Safe Load may be lower once included.")
                limiting_label = (f"{limiting_name} ({zn_zone})"
                                  if limiting_name == "Zn" and zn_zone else limiting_name)
                if cr is None:
                    results["safe_load_check"] = {
                        "pass": None, "safe_load": safe_load, "auto_calculated": True,
                        "limiting_element": limiting_name, "limiting_zone": zn_zone,
                        "limits_used": limits, "note": note,
                        "message": "Conductor rating unavailable for comparison",
                        "prc_label": prc_info["label"], "prc_info": prc_info, "r1_12_check": r1_12,
                        "cof_dvf": cof_dvf,
                    }
                else:
                    passed    = (safe_load >= cr) and not has_name_mismatch_prc and complete
                    load_line = f"Safe Load ({safe_load} A, auto-calc — {limiting_label} limiting) {'≥' if safe_load >= cr else '<'} Conductor Rating ({cr} A)"
                    results["safe_load_check"] = {
                        "pass": passed, "safe_load": safe_load, "conductor_rating": cr,
                        "auto_calculated": True, "limiting_element": limiting_name,
                        "limiting_zone": zn_zone,
                        "limits_used": limits, "note": note,
                        "prc_label": prc_info["label"], "prc_not_applicable": prc_info["not_applicable"],
                        "prc_info": prc_info, "load_line": load_line, "r1_12_check": r1_12,
                        "name_mismatches": mismatches["prc"],
                        "cof_dvf": cof_dvf,
                    }
                    if safe_load < cr:
                        all_errors.append(f"Safe Load ({safe_load} A, auto-calc) < Conductor Rating ({cr} A)")
                    if not complete:
                        all_errors.append(note)
        except Exception as e:
            results["safe_load_check"] = {"pass": False, "error": str(e)}
            all_errors.append(f"Safe Load auto-calculation failed: {e}")

    # Relay settings check removed — no longer required.
    results["relay_settings_check"] = {"pass": None, "message": "Check removed"}

    return results, all_errors, (len(all_errors) == 0)


# ═══════════════════════════════════════════════════════════════════════════════
# FEEDER CHECKLIST — New Feature
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_kv_from_filename(filename):
    """
    Extract system voltage in kV from RDB filename.
    Handles: 12.47 KV, 12.47KV, 69KV, 115 KV, 230KV, 4.16KV, 13.8KV
    Returns float or None.
    """
    # Decimal kV first (e.g. 12.47KV, 4.16 KV, 13.8KV)
    m = re.search(r'(\d+\.\d+)\s*KV', filename, re.IGNORECASE)
    if m:
        try: return float(m.group(1))
        except: pass
    # Integer kV (e.g. 69KV, 115 KV, 230KV)
    m = re.search(r'\b(\d{2,3})\s*KV\b', filename, re.IGNORECASE)
    if m:
        try: return float(m.group(1))
        except: pass
    return None


_PRIORITY_SECTIONS = ["S1","S2","S3","S4","S5","S6","G1","G2","O1","L1","L2","L3","L4","L5","L6"]
_PRIORITY_SECTIONS_SET = set(_PRIORITY_SECTIONS)

def _build_section_order(rdb_sections):
    """Build the ordered section list once per RDB, reusable across all setting lookups."""
    return _PRIORITY_SECTIONS + [k for k in rdb_sections if k not in _PRIORITY_SECTIONS_SET]


# ── UFLS-specific section order ──────────────────────────────────────────────
# Real SEL RDB files store setting-group data under bare numbered sections
# ("1".."6" = Setting Groups 1-6, matching [CLASSES] "1,\"Group 1\"" etc.),
# SELogic/output-mapping data under "L1".."L6", and a few global keys under
# "G" — NOT under the checklist-style "S1..S6/G1/G2/O1" names in
# _PRIORITY_SECTIONS. The UFLS checklist always verifies Setting Group 1, so
# section "1" (settings) and "L1" (logic: TR/OUT102/SET6) must be searched
# first, ahead of Groups 2-6 and everything else. This holds regardless of a
# particular RDB's internal key ordering, so it applies uniformly across
# every uploaded asset/relay file.
_UFLS_PRIORITY_SECTIONS = ["1", "L1", "G", "2", "3", "4", "5", "6",
                            "L2", "L3", "L4", "L5", "L6"]
_UFLS_PRIORITY_SECTIONS_SET = set(_UFLS_PRIORITY_SECTIONS)


def _build_ufls_section_order(rdb_sections):
    """Group-1-first section order for UFLS lookups. Build once per RDB and
    reuse across every UFLS setting lookup for that file."""
    return _UFLS_PRIORITY_SECTIONS + [k for k in rdb_sections if k not in _UFLS_PRIORITY_SECTIONS_SET]

def _get_rdb_setting(rdb_sections, *keys, _section_order=None):
    """
    Search ALL sections in the RDB for a list of possible setting keys.
    Returns the first non-empty value found, or empty string.
    Prioritises sections: S1-S6, then G1-G2, then all remaining sections.
    Pass _section_order (from _build_section_order) to avoid rebuilding it on every call.
    """
    all_sections = _section_order if _section_order is not None else _build_section_order(rdb_sections)

    for sec in all_sections:
        s = rdb_sections.get(sec, {})
        for key in keys:
            val = s.get(key.upper(), "")
            if val and str(val).strip() and str(val).strip() not in ("", "N", "0"):
                return str(val).strip()
    return ""


def _get_rdb_setting_exact(rdb_sections, *keys, _section_order=None):
    """
    Like _get_rdb_setting but returns ANY non-empty value including 'N', '0', etc.
    Used for boolean/enum settings like E81 where 'N' is a valid expected value.
    Pass _section_order (from _build_section_order) to avoid rebuilding it on every call.
    """
    all_sections = _section_order if _section_order is not None else _build_section_order(rdb_sections)

    for sec in all_sections:
        s = rdb_sections.get(sec, {})
        for key in keys:
            val = s.get(key.upper(), None)
            if val is not None and str(val).strip() != "":
                return str(val).strip()
    return ""



_KNOWN_RDB_EXTENSIONS = ('.rdb', '.txt', '.dat', '.zip', '.rar', '.7z', '.xlsm', '.xlsx')


def _strip_known_extension(filename):
    """
    Strip a file extension ONLY if it's a recognized one. Never use
    os.path.splitext() directly on these filenames — most uploaded RDBs
    have no real extension on disk (confirmed from the folder listings:
    "Dawson Creek 13.8kV Fdr 631F (06-11-2025) -", no ".rdb" at all), and
    splitext() treats the LAST dot anywhere in the string as the
    extension separator. On a name like "Dawson Creek 13.8kV Fdr 631F",
    that means splitext() chops it down to "Dawson Creek 13" and silently
    throws away "8kV Fdr 631F" — losing the feeder token entirely. Same
    failure on "Harrelson 13.8kV 328F SEL-351 2.17.25" (truncates to
    everything before the final ".25") and "Sharp ... 3.20.26" (same).
    """
    base = os.path.basename(filename)
    root, ext = os.path.splitext(base)
    if ext.lower() in _KNOWN_RDB_EXTENSIONS:
        return root
    return base


def _extract_station_name_from_filename(rdb_filename):
    """
    Extract the substation/station name from an RDB filename.
    Filenames: "Mannsdale 12.47KV FDR MD04 (SEL351S-7)(02-17-2025).rdb"
    Returns UPPER-CASE station name string or "".
    """
    base = _strip_known_extension(rdb_filename)
    # Remove parenthesised blocks
    base = re.sub(r"\([^)]*\)", " ", base)
    # Remove voltage tokens: 12.47KV, 230-13.8 KV, etc.
    # (?<![A-Za-z0-9]) / (?![A-Za-z0-9]) instead of \b: \b treats "_" as a
    # word char, so it never matches at an underscore and silently fails to
    # strip noise out of underscore-delimited filenames like
    # "McAlmont_UF_SEL-351A_AsFound(11.14)".
    base = re.sub(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?\s*KV(?![A-Za-z0-9])", " ", base, flags=re.IGNORECASE)
    # Remove MDxx identifiers
    base = re.sub(r"(?<![A-Za-z0-9])MD\s*\d{2,3}(?![A-Za-z0-9])", " ", base, flags=re.IGNORECASE)
    # Remove relay/feeder/as-found noise words
    noise = r"(?<![A-Za-z0-9])(?:FDR|FEEDER|MAIN|BUS\s*TIE|TIE|T[12]|SEL\S*|SVN\S*|RDB|UFLS|UF|PANEL|PNL|BKR|BREAKER|PRI)(?![A-Za-z0-9])|(?<![A-Za-z0-9])A[SL][\s_.]*(?:FOUND|LEFT|SETTINGS?)?(?![A-Za-z0-9])"
    base = re.sub(noise, " ", base, flags=re.IGNORECASE)
    # Collapse whitespace / dashes / underscores / periods
    base = re.sub(r"[\s_.-]+", " ", base).strip(" -_.")
    return base.upper() if base else ""


def _make_group_key(station_name, feeder_id):
    """
    Build a composite group key from station name + feeder ID.
    e.g. "MANNSDALE" + "MD04" -> "MANNSDALE_MD04"
    Ensures files from different stations with same MDxx don't merge.
    """
    parts = []
    if station_name:
        parts.append(re.sub(r"\s+", "_", station_name.strip().upper()))
    if feeder_id:
        parts.append(feeder_id.upper())
    return "_".join(parts) if parts else "UNKNOWN"


_RE_FEEDER_MD  = re.compile(r'\b(MD\s*\d{2,3})\b', re.IGNORECASE)
_RE_FEEDER_GID = re.compile(r'\b(\d?G\d{2})\b',    re.IGNORECASE)

def _extract_feeder_name_from_rdb(rdb_sections, rdb_filename=""):
    """
    Extract feeder name (e.g. MD04, MD22, MD24) from RDB sections or filename.
    Works for Feeder (FDR), Bus Tie (TIE/BUS TIE), and Main (MAIN/T1/T2) RDBs.

    Returns the MDxx (or similar) identifier as a string, e.g. "MD04", "MD22", "MD20".
    Returns None if not determinable.
    """
    _re_md  = _RE_FEEDER_MD
    _re_gid = _RE_FEEDER_GID

    id_keys = ["EOID", "IID", "ID", "SID", "UNITID", "FEEDER", "FID",
               "STNNAM", "LINEID", "NAME", "RSTNAM", "INAM"]

    # ── Step 1: scan every section for known ID keys ─────────────────────────
    for sec in rdb_sections.keys():
        s = rdb_sections.get(sec, {})
        for key in id_keys:
            val = s.get(key.upper(), "")
            if not val:
                continue
            val_str = str(val).strip()
            m = _re_md.search(val_str)
            if m:
                return m.group(1).upper().replace(" ", "")
            m = _re_gid.search(val_str)
            if m:
                return m.group(1).upper()

    # ── Step 2: MDxx anywhere in filename ────────────────────────────────────
    # Covers: FDR MD04, Main MD22, T2 Main MD24, Bus Tie MD20, BUS TIE MD20
    m = _re_md.search(rdb_filename)
    if m:
        return m.group(1).upper().replace(" ", "")

    # ── Step 3: FDR <token> in filename ──────────────────────────────────────
    m = re.search(r'\bFDR\s+(\S+)', rdb_filename, re.IGNORECASE)
    if m:
        token = m.group(1).rstrip('()')
        if _re_md.match(token):  return token.upper().replace(" ", "")
        if _re_gid.match(token): return token.upper()
        return ("FDR" + token).upper()

    return None


def _classify_rdb_group(folder_name):
    """
    Classify a folder/group name as 'feeder', 'tie', or 'main'.
    Used to divide groups in the UI into Feeder / Tie / Main sections.
    """
    name_l = folder_name.lower()
    # Bus Tie / Tie — check before main to avoid "main tie" confusion
    if re.search(r'\btie\b|\bbus\s*tie\b', name_l):
        return 'tie'
    # Main — e.g. T1 Main, T2 Main, Main
    if re.search(r'\bmain\b|\bt[12]\s*main\b', name_l):
        return 'main'
    # Feeder — FDR / Feeder
    if re.search(r'\bfdr\b|\bfeeder\b', name_l):
        return 'feeder'
    # Default to feeder
    return 'feeder'


def _parse_recommendations_excel(file_bytes):
    """
    Dynamically parse the Recommendations Excel file across ALL sheets.

    The Recommendations sheet has a multi-level header like:
        Row 3: "General" | ... | "Freq."
        Row 5: "Feeder"  | ... | "Trip Freq" (or just "Freq")
    so we scan every row looking for 'feeder' + a 'freq' column in the SAME row.

    The "Freq" column in the screenshot (column Z / rightmost) can be headed
    "Trip\\nFreq", "Freq", "Freq.", etc.

    Returns dict: { FEEDER_UPPER: freq_value }
    where freq_value is float | "N/A" | None
    """
    result = {}

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    except Exception:
        return result

    for sheet_name in wb.sheetnames:
        try:
            ws = wb[sheet_name]
            # Materialize once — needed for multi-pass header search
            all_rows = list(ws.iter_rows(values_only=True))
        except Exception:
            continue

        feeder_col      = None
        freq_col        = None
        header_row_idx  = None

        # ── Single-pass: find header row that has BOTH 'feeder' AND 'freq' ──────
        for row_idx, row in enumerate(all_rows):
            if not row:
                continue
            row_str = [str(c).strip().lower().replace('\n', ' ')
                       if c is not None else "" for c in row]

            fc = next((ci for ci, cell in enumerate(row_str)
                       if cell == "feeder" or cell.startswith("feed")), None)
            if fc is None:
                continue

            # freq: take the LAST occurrence (rightmost wins)
            qc = None
            for ci, cell in enumerate(row_str):
                if "freq" in cell:
                    qc = ci

            if qc is not None:
                feeder_col = fc
                freq_col   = qc
                header_row_idx = row_idx
                break

            # Found feeder column but no freq in this row — remember it for fallback
            if feeder_col is None:
                feeder_col     = fc
                header_row_idx = row_idx

        if feeder_col is None:
            continue

        # ── Fallback: scan rows up to header for a 'freq' column ─────────────
        if freq_col is None:
            for look_row in all_rows[:header_row_idx + 1]:
                if not look_row: continue
                for ci, c in enumerate(look_row):
                    if c is not None and "freq" in str(c).strip().lower():
                        freq_col = ci   # keep updating → rightmost wins

        if freq_col is None:
            # Last resort: use the last column that had any header text
            for look_row in all_rows[:header_row_idx + 1]:
                if not look_row: continue
                for ci, c in enumerate(look_row):
                    if c is not None and str(c).strip():
                        freq_col = ci
            if freq_col == feeder_col:
                freq_col = None

        if freq_col is None:
            continue

        # ── Read data rows ────────────────────────────────────────────────────
        for row in all_rows[header_row_idx + 1:]:
            if not row:
                continue
            feeder_val = row[feeder_col] if feeder_col < len(row) else None
            freq_val   = row[freq_col]   if freq_col   < len(row) else None

            if feeder_val is None:
                continue
            feeder_str = str(feeder_val).strip()
            if not feeder_str or feeder_str.lower() in ("none", "nan", ""):
                continue

            feeder_key = feeder_str.upper().replace(" ", "")

            if freq_val is None:
                freq_norm = None
            else:
                freq_str = str(freq_val).strip()
                if freq_str.upper() in ("N/A", "NA", "NONE", "NAN", ""):
                    freq_norm = "N/A"
                else:
                    try:
                        freq_norm = float(freq_str)
                    except ValueError:
                        m = re.search(r'(\d+\.?\d*)', freq_str)
                        freq_norm = float(m.group(1)) if m else freq_str

            result[feeder_key] = freq_norm

    wb.close()
    return result


def _is_sel551_relay(rdb_filename):
    """
    Return True if the RDB filename indicates a SEL-551 relay.
    Matches patterns like: SEL551, SEL-551, SEL_551 (case-insensitive).
    """
    return bool(re.search(r'\bSEL[-_]?551\b', rdb_filename, re.IGNORECASE))


def _run_feeder_checks(rdb_sections, rdb_filename, recommendations_data, has_recommendations):
    """
    Run all feeder checks for a single RDB file.
    Searches ALL RDB sections for required settings (not just S1-S6).

    Logic:
      1. If the relay is a SEL-551, skip all frequency/VNOM checks entirely
         (E81, 81D1P, 81D1D, 27P1P settings do not exist in SEL-551 RDBs).
      2. Check E81 (Under Frequency Status).
      3. If E81 is disabled (RDB="N") AND the recommendation is also N/A (or no rec file),
         the three dependent checks — Under Frequency Value (81D1P), UF Time Delay (81D1D),
         and VNOM Value Verification (27P1P) — are skipped entirely.  A single informational
         note is appended to the checks list explaining why they were skipped.
      4. Otherwise all four checks are run normally.

    Returns list of check-result dicts.
    """
    checks = []

    # Build section order once — reused by all _get_rdb_setting* calls below.
    # NOTE: real SEL RDBs store each setting group under a bare numbered
    # section ("1".."6"), not under checklist-style names like "S1"/"G1"/"O1"
    # (those never actually appear as RDB section headers). Using the plain
    # _build_section_order() here left the "which group" choice to whatever
    # order the sections happened to land in the parsed dict — usually Group
    # 1 by luck, but not guaranteed. _build_ufls_section_order() explicitly
    # puts "1" (Group 1 settings) and "L1" (Group 1 logic) first, so E81,
    # 81D1P, 81D1D, PTR, and 27B81P are always read from Group 1.
    _sec_order = _build_ufls_section_order(rdb_sections)

    # ── Determine feeder name ─────────────────────────────────────────────────
    feeder_name = _extract_feeder_name_from_rdb(rdb_sections, rdb_filename)
    line_name   = feeder_name if feeder_name else os.path.basename(rdb_filename)

    # ══════════════════════════════════════════════════════════════════════════
    # SEL-551 RELAY: Skip all frequency & VNOM checks — these settings
    # (E81, 81D1P, 81D1D, 27B81P) are not present in SEL-551 RDBs.
    # ══════════════════════════════════════════════════════════════════════════
    if _is_sel551_relay(rdb_filename):
        checks.append({
            "check_name":     "_SEL551_SKIP_NOTE_",
            "line_name":      line_name,
            "rdb_value":      "N/A",
            "recommendation": "N/A",
            "status":         "skipped",
            "skipped_checks": [
                "Under Frequency Status (E81)",
                "Under Frequency Value (81D1P)",
                "UF Time Delay (81D1D)",
                "VNOM Value Verification (27P1P)",
            ],
            "skip_reason": (
                "SEL-551 relay detected — E81, 81D1P, 81D1D, and 27B81P settings "
                "are not applicable to this relay model and have been excluded from checking."
            ),
            "warning": None,
            "error":   None,
        })
        return checks

    # ══════════════════════════════════════════════════════════════════════════
    # CHECK 14: Under Frequency Status  (E81)
    # ══════════════════════════════════════════════════════════════════════════
    # E81 can be "Y", "N", "1", "0" — must retrieve EXACT value
    e81_rdb = _get_rdb_setting_exact(rdb_sections, "E81", _section_order=_sec_order)

    rec_freq    = None
    rec_display = "—"
    rec_warning = None
    freq_status = "pass"
    freq_error  = None

    if not has_recommendations:
        rec_warning = "No recommendations file provided"
        freq_status = "info"
    elif feeder_name is None:
        rec_warning = "Could not determine feeder name from RDB"
        freq_status = "warning"
    elif feeder_name.upper() not in recommendations_data:
        rec_warning = f"Feeder '{feeder_name}' not found in recommendations"
        freq_status = "warning"
    else:
        rec_freq = recommendations_data[feeder_name.upper()]
        # recommendation column for E81: N/A → Disabled, numeric → Enabled
        if rec_freq == "N/A" or rec_freq is None:
            rec_display = "Disabled (N)"
        else:
            rec_display = "Enabled (1)"

        if rec_freq == "N/A" or rec_freq is None:
            # Recommendations → N/A  ⟹ RDB E81 must be "N"
            if e81_rdb.upper() == "N":
                freq_status = "pass"
            else:
                freq_status = "fail"
                freq_error  = (
                    f"E81 should be 'N' (Recommendations: Disabled/N/A) "
                    f"but RDB has E81='{e81_rdb}'"
                )
        else:
            # Recommendations → numeric  ⟹ RDB E81 must be "1"
            if e81_rdb == "1":
                freq_status = "pass"
            else:
                freq_status = "fail"
                freq_error  = (
                    f"E81 should be '1' (Recommendations: Enabled) "
                    f"but RDB has E81='{e81_rdb}'"
                )

    checks.append({
        "check_name":     "Under Frequency Status (E81)",
        "line_name":      line_name,
        "rdb_value":      e81_rdb if e81_rdb else "—",
        "recommendation": rec_display if has_recommendations else "—",
        "status":         freq_status,
        "warning":        rec_warning,
        "error":          freq_error,
    })

    # ══════════════════════════════════════════════════════════════════════════
    # SKIP GATE: If E81 is disabled (N) AND recommendation is also N/A (or no
    # rec file provided), the three dependent checks are NOT needed.
    # Add a single "skipped" sentinel entry and return early.
    # ══════════════════════════════════════════════════════════════════════════
    e81_is_disabled = e81_rdb.upper() == "N" if e81_rdb else False
    rec_is_na       = (not has_recommendations) or (rec_freq == "N/A") or (rec_freq is None)

    if e81_is_disabled and rec_is_na:
        # All three dependent checks are skipped — add one explanatory entry
        checks.append({
            "check_name":        "_E81_DISABLED_SKIP_NOTE_",
            "line_name":         line_name,
            "rdb_value":         e81_rdb if e81_rdb else "N",
            "recommendation":    "N/A",
            "status":            "skipped",
            "skipped_checks":    [
                "Under Frequency Value (81D1P)",
                "UF Time Delay (81D1D)",
                "VNOM Value Verification (27P1P)",
            ],
            "skip_reason": (
                "E81 (Under Frequency protection) is disabled (N) in this RDB"
                + (" and the Recommendations file also marks it as Not Applicable (N/A)."
                   if has_recommendations else " and no Recommendations file was provided.")
            ),
            "warning": None,
            "error":   None,
        })
        return checks

    # ══════════════════════════════════════════════════════════════════════════
    # CHECK 14b: Under Frequency Value (81D1P)
    # Compare RDB pickup freq vs recommendation value.
    # ══════════════════════════════════════════════════════════════════════════
    uf_val_rdb_raw = _get_rdb_setting_exact(
        rdb_sections, "81D1P", "81P1P", "81PU1", "UFP1",
        _section_order=_sec_order
    )
    uf_val_status      = "pass"
    uf_val_error       = None
    uf_val_rec_display = "—"

    _uf_rec_freq = (
        rec_freq
        if has_recommendations and feeder_name and feeder_name.upper() in recommendations_data
        else None
    )

    if not has_recommendations:
        uf_val_status      = "info"
        uf_val_rec_display = "—"
    elif feeder_name is None:
        uf_val_status = "warning"
        uf_val_error  = "Could not determine feeder name from RDB"
    elif feeder_name.upper() not in recommendations_data:
        uf_val_status = "warning"
        uf_val_error  = f"Feeder '{feeder_name}' not found in recommendations"
    elif _uf_rec_freq == "N/A" or _uf_rec_freq is None:
        uf_val_status      = "pass"
        uf_val_rec_display = "N/A"
    else:
        uf_val_rec_display = str(_uf_rec_freq)
        if not uf_val_rdb_raw:
            uf_val_status = "warning"
            uf_val_error  = "81D1P setting not found in RDB"
        else:
            try:
                rdb_f = float(uf_val_rdb_raw)
                rec_f = float(_uf_rec_freq)
                if abs(rdb_f - rec_f) < 0.011:
                    uf_val_status = "pass"
                else:
                    uf_val_status = "fail"
                    uf_val_error  = (
                        f"81D1P mismatch: RDB={rdb_f} Hz, "
                        f"Recommendations={rec_f} Hz"
                    )
            except ValueError:
                uf_val_status = "warning"
                uf_val_error  = f"Could not parse 81D1P value: '{uf_val_rdb_raw}'"

    checks.append({
        "check_name":     "Under Frequency Value (81D1P)",
        "line_name":      line_name,
        "rdb_value":      uf_val_rdb_raw if uf_val_rdb_raw else "—",
        "recommendation": uf_val_rec_display,
        "status":         uf_val_status,
        "warning":        None,
        "error":          uf_val_error,
    })

    # ══════════════════════════════════════════════════════════════════════════
    # CHECK 15: UF Time Delay  (81D1D ≥ 6 cycles)
    # ══════════════════════════════════════════════════════════════════════════
    uf_delay_raw = _get_rdb_setting_exact(
        rdb_sections, "81D1D", "81T1D", "81TD1", "UFTD1",
        _section_order=_sec_order
    )
    uf_status = "pass"
    uf_error  = None

    if not uf_delay_raw:
        uf_status = "warning"
        uf_error  = "81D1D setting not found in RDB"
    else:
        try:
            uf_val = float(uf_delay_raw)
            if uf_val >= 6:
                uf_status = "pass"
            else:
                uf_status = "fail"
                uf_error  = f"81D1D = {uf_val} cycles — must be ≥ 6 cycles"
        except ValueError:
            uf_status = "warning"
            uf_error  = f"Could not parse 81D1D value: '{uf_delay_raw}'"

    checks.append({
        "check_name":     "UF Time Delay (81D1D)",
        "line_name":      line_name,
        "rdb_value":      uf_delay_raw if uf_delay_raw else "—",
        "recommendation": "≥ 6 cycles",
        "status":         uf_status,
        "warning":        None,
        "error":          uf_error,
    })

    # ══════════════════════════════════════════════════════════════════════════
    # CHECK 16: VNOM Verification  (27B81P = 70% of VNOM)
    # Formula: Step1 = kV*1000/PTR  |  Step2 = Step1/√3  |  Step3 = Step2×70%
    # ══════════════════════════════════════════════════════════════════════════
    kv_voltage = _extract_kv_from_filename(rdb_filename)

    ptr_raw   = _get_rdb_setting(
        rdb_sections, "PTR", "PTRY", "PTRX", "PTRZ", "PT", "VTR", "VPTR",
        _section_order=_sec_order
    )
    vb81p_raw = _get_rdb_setting_exact(
        rdb_sections, "27B81P", "27BP", "V27B81P",
        _section_order=_sec_order
    )

    vnom_status       = "pass"
    vnom_error        = None
    vnom_warning      = None
    vnom_calc_display = "—"
    vnom_rdb_display  = vb81p_raw if vb81p_raw else "—"

    if kv_voltage is None:
        vnom_status  = "warning"
        vnom_warning = "Could not extract kV from RDB filename"
    elif not ptr_raw:
        vnom_status  = "warning"
        vnom_warning = "PTR not found in RDB settings"
    elif not vb81p_raw:
        vnom_status  = "warning"
        vnom_warning = "27B81P not found in RDB settings"
    else:
        try:
            ptr_val = float(str(ptr_raw).strip())
            if ptr_val == 0:
                raise ValueError("PTR is zero")
            step1 = (kv_voltage * 1000.0) / ptr_val
            step2 = step1 / math.sqrt(3)
            step3 = round(step2 * 0.70, 2)
            vnom_calc_display = str(step3)

            b81p_val = float(vb81p_raw)
            if abs(step3 - b81p_val) < 0.5:
                vnom_status = "pass"
            else:
                vnom_status = "fail"
                vnom_error  = (
                    f"27B81P mismatch: RDB={b81p_val}, Expected={step3} "
                    f"(kV={kv_voltage}, PTR={ptr_val}, "
                    f"Step1={round(step1,2)}(kV × 1000 / PTR), "
                    f"Step2={round(step2,2)}(Step1 / √3), "
                    f"Step3={step3}(Step2 × 70%))"
                )
        except Exception as ex:
            vnom_status  = "warning"
            vnom_warning = f"VNOM calculation error: {ex}"

    checks.append({
        "check_name":     "VNOM Value Verification (27P1P)",
        "line_name":      line_name,
        "rdb_value":      vnom_rdb_display,
        "recommendation": vnom_calc_display,
        "status":         vnom_status,
        "warning":        vnom_warning,
        "error":          vnom_error,
    })

    return checks



# ─── VERSION / UPDATE ENDPOINTS ──────────────────────────────────────────────
# These endpoints are called by the UI on startup to show the version badge
# and notify the user when a newer release is available on GitHub.

# ═══════════════════════════════════════════════════════════════════════════════
# LINE PROTECTION CALCULATION SHEET — Helper Functions & Route
# Serves POST /upload  (Excel generation from form + CSV)
# ═══════════════════════════════════════════════════════════════════════════════

_LP_HERE     = os.path.dirname(os.path.abspath(__file__))
_LP_TEMPLATE = os.path.join(_LP_HERE, "Line_Protection_Calculation_Sheet_Template.xlsm")

def _col_to_num(col: str) -> int:
    n = 0
    for c in col.upper():
        n = n * 26 + (ord(c) - 64)
    return n

def _num_to_col(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s

def _parse_addr(addr: str):
    m = re.match(r"([A-Za-z]+)(\d+)", addr.strip())
    return _col_to_num(m.group(1)), int(m.group(2))

def _build_merge_map(xml_str: str) -> dict:
    """Return {non_top_left_addr: top_left_addr} for all merged regions."""
    merge_map = {}
    for m in re.finditer(r'<mergeCell ref="([A-Z]+\d+):([A-Z]+\d+)"', xml_str):
        sc, sr = _parse_addr(m.group(1))
        ec, er = _parse_addr(m.group(2))
        tl = m.group(1)
        for r in range(sr, er + 1):
            for c in range(sc, ec + 1):
                addr = f"{_num_to_col(c)}{r}"
                if addr != tl:
                    merge_map[addr] = tl
    return merge_map


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def _write_cell_xml(xml_str: str, addr: str, raw) -> str:
    """
    Patch a single cell value directly into worksheet XML.
    Uses inline strings (t="inlineStr") for text to avoid touching sharedStrings.
    Preserves all other cell attributes (style, etc.) unchanged.
    """
    if raw is None or str(raw).strip() == "":
        return xml_str

    # Resolve merged cells: write to top-left
    merge_map = _build_merge_map(xml_str)
    addr = merge_map.get(addr, addr)

    # Try numeric first
    raw_str = str(raw).replace("\u00a0", " ").strip()
    try:
        num = float(raw_str)
        value_xml = f"<v>{int(num) if num == int(num) else num}</v>"
        t_attr = ""
    except (ValueError, TypeError):
        value_xml = f"<is><t>{_xml_escape(raw_str)}</t></is>"
        t_attr = ' t="inlineStr"'

    addr_re = re.escape(addr)

    # Case 1: self-closing empty cell  <c r="ADDR" s="NNN"/>
    m = re.search(rf'(<c r="{addr_re}"([^/]*?)/>)', xml_str)
    if m:
        attrs = re.sub(r'\s*t="[^"]*"', "", m.group(2))
        repl = f'<c r="{addr}"{attrs}{t_attr}>{value_xml}</c>'
        return xml_str[:m.start()] + repl + xml_str[m.end():]

    # Case 2: cell with existing content  <c r="ADDR" ...>...</c>
    m = re.search(rf'(<c r="{addr_re}"([^>]*)>)(.*?)(</c>)', xml_str, re.DOTALL)
    if m:
        attrs = re.sub(r'\s*t="[^"]*"', "", m.group(2))
        inner = m.group(3)
        if "<f>" in inner and t_attr == "":
            # Keep formula; just replace/add cached value
            new_inner = re.sub(r"<v>[^<]*</v>", "", inner).rstrip() + value_xml
            repl = f"<c r=\"{addr}\"{attrs}>{new_inner}</c>"
        else:
            repl = f'<c r="{addr}"{attrs}{t_attr}>{value_xml}</c>'
        return xml_str[:m.start()] + repl + xml_str[m.end():]

    app.logger.warning("Cell %s not found in sheet XML — skipped", addr)
    return xml_str


def _patch_sheet(xml_bytes: bytes, writes: dict) -> bytes:
    """Apply a dict of {addr: value} writes to a worksheet XML byte string."""
    xml = xml_bytes.decode("utf-8")
    for addr, val in writes.items():
        if val is not None and str(val).strip():
            xml = _write_cell_xml(xml, addr, val)
    return xml.encode("utf-8")


def _enable_full_calc(workbook_bytes: bytes) -> bytes:
    """Patch fullCalcOnLoad="1" into xl/workbook.xml <calcPr> element."""
    xml = workbook_bytes.decode("utf-8")
    if 'fullCalcOnLoad=' in xml:
        xml = re.sub(r'fullCalcOnLoad="[^"]*"', 'fullCalcOnLoad="1"', xml)
    else:
        xml = xml.replace("<calcPr ", '<calcPr fullCalcOnLoad="1" ')
    return xml.encode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _csv_handler(rows, r, c):
    if r < 0 or r >= len(rows): return ""
    row = rows[r]
    if c < 0 or c >= len(row): return ""
    v = row[c]
    return "" if v is None else v.replace("\u00a0", " ").strip()


def _first_number(s):
    if not s: return ""
    t = s.replace("\u00a0", " ").replace(",", "").strip()
    up = t.upper()
    if any(k in up for k in ("INFINITE", "INFINITY", "∞")) or re.search(r"\bINF\b", up):
        return "0"
    m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", t)
    return m.group() if m else ""


# ─────────────────────────────────────────────────────────────────────────────
# CSV PARSERS
# ─────────────────────────────────────────────────────────────────────────────

def _find_section_start(rows, section_name):
    upper = section_name.upper()
    for i, row in enumerate(rows):
        if upper in ",".join(row).upper(): return i
    return -1


def _parse_infeed_data(rows, infeed_start, impedance_start):
    result = {}
    if infeed_start == -1: return result
    end = impedance_start if impedance_start != -1 else len(rows)
    bus = 1; i = infeed_start
    while i < end and bus <= 12:
        if "WHEN APPLYING BUS FAULT AT:" in ",".join(rows[i]).upper():
            result[bus] = (_csv_handler(rows, i+1, 2) or "0",
                           _csv_handler(rows, i+2, 2) or "0")
            bus += 1; i += 3; continue
        i += 1
    return result


def _parse_impedance_data(rows, imp_start):
    data = {"first": None, "second": []}
    if imp_start == -1: return data
    i = imp_start
    while i < len(rows):
        line = ",".join(rows[i]).strip()
        if "FIRST LINE IMPEDENCE AT BUS:" in line.upper():
            data["first"] = {
                "r1":    _first_number(_csv_handler(rows, i+1, 2)),
                "x1":    _first_number(_csv_handler(rows, i+1, 3)),
                "r0":    _first_number(_csv_handler(rows, i+2, 2)),
                "x0":    _first_number(_csv_handler(rows, i+2, 3)),
                "miles": _first_number(_csv_handler(rows, i+3, 2)),
            }
            i += 4; continue
        if "SECOND LINE IMPEDENCES FOR LINE:" in line.upper():
            # Extract station name from the header cell (column 1), e.g.:
            #   "Second Line Impedences for line: 2746-GT24"       → "GT24"
            #   "Second Line Impedences for line: 4434-RAGSDALE 1" → "RAGSDALE 1"
            #   "Second Line Impedences for line: 4821-SOUTH CANTON" → "SOUTH CANTON"
            # Split on first '-' after the colon so hyphenated names (e.g. CANTON-22) stay intact.
            raw_cell  = _csv_handler(rows, i, 1)
            line_name = ""
            if ":" in raw_cell:
                after_colon = raw_cell.split(":", 1)[1].strip()   # e.g. "2746-GT24"
                if "-" in after_colon:
                    line_name = after_colon.split("-", 1)[1].strip()  # e.g. "GT24"
            data["second"].append({
                "name":  line_name,
                "r1":    _first_number(_csv_handler(rows, i+1, 2)),
                "x1":    _first_number(_csv_handler(rows, i+1, 3)),
                "r0":    _first_number(_csv_handler(rows, i+2, 2)),
                "x0":    _first_number(_csv_handler(rows, i+2, 3)),
                "miles": _first_number(_csv_handler(rows, i+3, 2)),
            })
            i += 4; continue
        i += 1
    return data


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/upload", methods=["POST"])
def handle_upload():
    # 1. Parse form data
    raw_json = request.form.get("formData")
    if not raw_json:
        return jsonify({"error": "formData is required"}), 400
    try:
        fd = json.loads(raw_json)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Invalid JSON: {e}"}), 400
    app.logger.info("formData parsed (%d keys)", len(fd))

    # 2. Parse CSV
    csv_file = request.files.get("csvFile")
    if not csv_file:
        return jsonify({"error": "csvFile is required"}), 400
    text = csv_file.stream.read().decode("utf-8-sig", errors="replace")
    csv_rows = list(csv.reader(io.StringIO(text)))
    if not csv_rows:
        return jsonify({"error": "CSV file is empty"}), 400
    app.logger.info("CSV rows = %d", len(csv_rows))

    inf_start = _find_section_start(csv_rows, "INFEED TAB")
    imp_start = _find_section_start(csv_rows, "APA IMPEDANCES TAB")
    app.logger.info("INFEED TAB @ %s | APA IMPEDANCES TAB @ %s", inf_start, imp_start)

    infeed_map = _parse_infeed_data(csv_rows, inf_start, imp_start)
    imp_data   = _parse_impedance_data(csv_rows, imp_start)
    app.logger.info("Infeed=%d | second lines=%d", len(infeed_map), len(imp_data["second"]))

    # 3. Load template
    if not os.path.exists(_LP_TEMPLATE):
        return jsonify({"error": f"Template not found: {_LP_TEMPLATE}"}), 500

    with open(_LP_TEMPLATE, "rb") as f:
        template_bytes = f.read()

    # 4. Build per-sheet write dicts
    # ── Data Entry (sheet1) ──────────────────────────────────────────────────
    de_writes = {
        "G3":   fd.get("relayLocation",               ""),
        "K3":   fd.get("lineNumber",                   ""),
        "M3":   fd.get("remoteLocation",               ""),
        "E16":  fd.get("noninalSystemVoltage",           ""),
        "E18":  fd.get("breakerRating",                ""),
        "E19":  fd.get("conductorRating",              ""),
        "E22":  fd.get("ctrW",                         ""),
        "E23":  fd.get("ctrX",                         ""),
        "E24":  fd.get("ptry",                         ""),
        "E26":  fd.get("secondlines",                  ""),
        "E27":  fd.get("numberOfTaps",                 ""),
        "E28":  fd.get("autoXfmrAtRemote",             ""),
        "E29":  fd.get("numberOfBreakers",             ""),
        "E30":  fd.get("noOfDistributionTransformers", ""),
        "E34":  fd.get("relayLoadbility",              ""),
        "E270": fd.get("syncReference",                ""),
        "E271": fd.get("syncSource",                   ""),
        "E273": fd.get("hotLineInd",                   ""),
        "E274": fd.get("vazPtRatio",                   ""),
        "E275": fd.get("vbzPtRatio",                   ""),
        "E276": fd.get("vczPtRatio",                   ""),
        "E282": fd.get("remoteCTR",                    ""),
        "E285": fd.get("remoteBFPU",                   ""),
        "E286": fd.get("remoteBFGU",                   ""),
    }

    # ── Fault Analysis (sheet5) ──────────────────────────────────────────────
    def cv(r, c): return _csv_handler(csv_rows, r, c)

    fa_writes = {
        "G17": cv(0,2),  "G18": cv(1,2),  "G19": cv(2,2),
        "G22": cv(6,2),  "G23": cv(7,2),  "G25": cv(8,2),
        "G36": cv(9,2),  "K36": cv(9,4),
        "G37": cv(10,2), "K37": cv(10,4),
        "G38": cv(12,2), "I38": cv(12,4), "K38": cv(12,6),
        "K39": cv(13,2),
        # Differential relay
        "S35": cv(15,2), "U35": cv(15,4), "W35": cv(15,6), "X35": cv(15,8),
        "S36": cv(17,2), "U36": cv(17,4), "W36": cv(17,6), "X36": cv(17,8),
        "S37": cv(18,2), "U37": cv(18,4), "W37": cv(18,6), "X37": cv(18,8),
        "S38": cv(19,2), "U38": cv(19,4), "W38": cv(19,6), "X38": cv(19,8),
        # X/R n-0
        "E44": cv(21,2), "E45": cv(22,2),
        "E47": cv(23,2),  "G47": cv(23,4),  "I47": cv(23,6),
        "K47": cv(23,8),  "M47": cv(23,10), "O47": cv(23,12),
        # X/R n-1
        "E70": cv(24,2), "E71": cv(25,2),
        "E73": cv(26,2),  "G73": cv(26,4),  "I73": cv(26,6),
        "K73": cv(26,8),  "M73": cv(26,10), "O73": cv(26,12),
    }

    # ── Infeed (sheet6) ──────────────────────────────────────────────────────
    inf_writes = {}
    for bus in range(1, 13):
        er = 14 + bus
        mag, ang = infeed_map.get(bus, ("0", "0"))
        inf_writes[f"R{er}"] = mag
        inf_writes[f"T{er}"] = ang

    # ── Aspen Impedances (sheet4) ────────────────────────────────────────────
    asp_writes = {}
    fl = imp_data["first"]
    if fl:
        asp_writes.update({"E6": fl["r1"], "F6": fl["x1"],
                           "G6": fl["r0"], "H6": fl["x0"], "I6": fl["miles"]})
    # Data rows (impedance values): 15, 23, 31, 39, 47, 55, 63, 71
    # Label rows (line name in col D): 20, 28, 36, 44, 52, 60, 68, 76
    _data_rows  = [15, 23, 31, 39, 47, 55, 63, 71]
    _label_rows = [20, 28, 36, 44, 52, 60, 68, 76]
    for idx, line in enumerate(imp_data["second"][:8]):
        er  = _data_rows[idx]
        lr  = _label_rows[idx]
        asp_writes.update({
            f"D{lr}": line.get("name", ""),           # station label in col D, e.g. "GT24"
            f"E{er}": line["r1"], f"F{er}": line["x1"],
            f"G{er}": line["r0"], f"H{er}": line["x0"], f"I{er}": line["miles"],
        })

    # 5. Build final ZIP: patch only the 4 target sheets; everything else verbatim
    # Sheet name → ZIP path mapping (from workbook.xml sheet order):
    # sheet1=1)DataEntry  sheet4=3)Aspen  sheet5=4)FaultAnalysis  sheet6=5)Infeed
    PATCHES = {
        "xl/worksheets/sheet1.xml": de_writes,
        "xl/worksheets/sheet5.xml": fa_writes,
        "xl/worksheets/sheet6.xml": inf_writes,
        "xl/worksheets/sheet4.xml": asp_writes,
    }

    out_buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(template_bytes), "r") as tpl_zf, \
         zipfile.ZipFile(out_buf, "w", allowZip64=True) as out_zf:

        for info in tpl_zf.infolist():
            name = info.filename
            raw  = tpl_zf.read(name)

            if name in PATCHES:
                patched = _patch_sheet(raw, PATCHES[name])
                app.logger.info("Patched %s (%d writes)", name, len([v for v in PATCHES[name].values() if v]))
                out_zf.writestr(zipfile.ZipInfo(name), patched,
                                compress_type=zipfile.ZIP_DEFLATED)

            elif name == "xl/workbook.xml":
                out_zf.writestr(zipfile.ZipInfo(name), _enable_full_calc(raw),
                                compress_type=info.compress_type)

            else:
                # Everything else verbatim — preserves compression type
                out_zf.writestr(zipfile.ZipInfo(name), raw,
                                compress_type=info.compress_type)

    app.logger.info("=== DONE — sending file ===")
    out_buf.seek(0)
    response = send_file(
        out_buf,
        as_attachment=True,
        download_name="Updated_Line_Protection_Calculation_Sheet.xlsm",
        mimetype="application/vnd.ms-excel.sheet.macroEnabled.12",
    )

    # Prevent Windows from stamping a Zone.Identifier (Mark-of-the-Web) ADS
    # on the downloaded file, which causes Excel to block macros.
    # Serving from localhost already puts the file in Zone 1 (local intranet),
    # but these headers ensure the browser passes bytes directly without
    # writing an intermediate temp tagged with Zone 3 (internet).
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Explicit same-origin scope keeps Windows WebClient in Zone 1
    response.headers["Content-Security-Policy"] = "default-src 'self'"

    return response


@app.route("/lp/health")
def lp_health():
    return jsonify({"status": "ok", "template_found": os.path.exists(_LP_TEMPLATE)})


# ═══════════════════════════════════════════════════════════════════════════════
# PGRI (Relay Compliance Checker) Routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/version")
def version():
    return jsonify({"version": APP_VERSION})

@app.route("/api/check_update")
def check_update():
    import urllib.request, json as _json
    GITHUB_LATEST = "https://api.github.com/repos/phanisaisadhanala/Compliance_Checker/releases/latest"
    try:
        req = urllib.request.Request(GITHUB_LATEST, headers={"User-Agent": "PhasorGrid"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read())
        latest = data.get("tag_name", "").lstrip("v")
        download_url = next(
            (a["browser_download_url"] for a in data.get("assets", [])
             if a["name"].endswith(".exe")), None
        )
        update_available = latest and latest != APP_VERSION
        return jsonify({
            "current": APP_VERSION, "latest": latest,
            "update_available": update_available, "download_url": download_url,
        })
    except Exception as e:
        return jsonify({"current": APP_VERSION, "update_available": False, "error": str(e)})


# ─── SINGLE CHECK ─────────────────────────────────────────────────────────────
@app.route("/api/check", methods=["POST"])
def check():
    results_top, all_errors = {}, []
    calc_file = request.files.get("calc_sheet")
    rdb_file  = request.files.get("rdb_file")
    fr_file   = request.files.get("fr_workbook")
    slc_file  = request.files.get("sel_file")
    if not calc_file and not any([rdb_file, fr_file, slc_file]):
        return jsonify({"success": False, "errors": ["Please upload the Calc Sheet and at least one supporting file."], "results": {}, "_no_table": True})
    if not calc_file:
        return jsonify({"success": False, "errors": ["Please upload the Calc Sheet first."], "results": {}, "_no_table": True})
    if not any([rdb_file, fr_file, slc_file]):
        return jsonify({"success": False, "errors": ["Please upload at least one supporting file (RDB, FR Workbook, or SLC)."], "results": {}, "_no_table": True})
    calc_data = None
    try:
        calc_data = parse_calc_sheet(calc_file.read())
        results_top["calc_sheet"] = {"parsed": True, "values": calc_data}
    except Exception as e:
        results_top["calc_sheet"] = {"parsed": False, "error": str(e)}
        all_errors.append(f"Failed to parse Calc Sheet: {e}")
    rdb_sections = {}
    if rdb_file:
        try:
            raw = rdb_file.read()
            rdb_sections = parse_rdb_sections_by_relay(raw)
            results_top["rdb_parsed"] = {"ok": True, "sections": list(rdb_sections.keys())}
        except Exception as e:
            results_top["rdb_parsed"] = {"ok": False, "error": str(e)}
            all_errors.append(f"Failed to parse RDB file: {e}")
    else:
        results_top["rdb_parsed"] = {"ok": False, "message": "RDB file not uploaded"}
    fr_result = None
    if fr_file:
        try: fr_result = parse_fr_workbook(fr_file.read())
        except Exception as e: all_errors.append(f"Failed to parse FR Workbook: {e}")
    slc_content = None
    if slc_file:
        try: slc_content = slc_file.read().decode("utf-8", errors="ignore")
        except Exception as e: all_errors.append(f"Failed to read SLC file: {e}")

    # Manual ZLE / Zn Amp Limit entry — only used when no SLC file was
    # uploaded (see the on-screen fields that appear in that case).
    manual_zle_amp = _safe_float(request.form.get("manual_zle_amp"))
    manual_zn_amp  = _safe_float(request.form.get("manual_zn_amp"))

    check_results, check_errors, success = _run_checks(
        calc_data, rdb_sections, fr_result, slc_content,
        calc_name=calc_file.filename if calc_file else None,
        rdb_name=rdb_file.filename   if rdb_file  else None,
        fr_name=fr_file.filename     if fr_file   else None,
        slc_name=slc_file.filename   if slc_file  else None,
        manual_zle_amp=manual_zle_amp,
        manual_zn_amp=manual_zn_amp,
    )
    all_errors.extend(check_errors)
    return jsonify({"success": len(all_errors) == 0, "errors": all_errors,
                    "results": {**results_top, **check_results}, "_no_table": False})


# ─── MULTI CHECK (legacy) ─────────────────────────────────────────────────────
@app.route("/api/check_multi", methods=["POST"])
def check_multi():
    return jsonify({
        "success": False,
        "summary": {"total_groups": 0, "total_rdb": 0, "passed": 0, "failed": 0},
        "groups": [],
        "errors": [
            "The legacy multi-file endpoint is disabled. "
            "Grouping MUST be based on root folder structure only. "
            "Please use the Group RDB Check mode (folder drop) which uses /api/batch_group_check."
        ]
    }), 400


# ─── GROUP RDB CHECK ──────────────────────────────────────────────────────────
@app.route("/api/group_check", methods=["POST"])
def group_check():
    calc_file  = request.files.get("calc_sheet")
    rdb_files  = request.files.getlist("rdb_files[]") or request.files.getlist("rdb_files")
    fr_file    = request.files.get("fr_workbook")
    slc_files  = request.files.getlist("sel_files[]") or request.files.getlist("sel_files")
    group_name = request.form.get("group_name", "Group Check")

    errors = []
    if not calc_file:
        return jsonify({"success": False, "errors": ["Calc Sheet is required for Group Check."],
                        "summary": {}, "groups": []}), 400
    if not rdb_files:
        return jsonify({"success": False, "errors": ["At least one RDB file is required."],
                        "summary": {}, "groups": []}), 400

    calc_bytes = calc_file.read()
    calc_data  = None
    try:
        calc_data = parse_calc_sheet(calc_bytes)
    except Exception as e:
        errors.append(f"Failed to parse Calc Sheet: {e}")

    fr_result = None
    if fr_file:
        try: fr_result = parse_fr_workbook(fr_file.read())
        except Exception as e: errors.append(f"Failed to parse FR Workbook: {e}")

    slc_by_port  = {}
    slc_default  = None
    slc_default_name = None
    for sf in slc_files:
        try: content = sf.read().decode("utf-8", errors="ignore")
        except: continue
        port = _extract_port(sf.filename)
        if port:
            if port not in slc_by_port:
                slc_by_port[port] = (content, sf.filename)
        else:
            if slc_default is None:
                slc_default = content
                slc_default_name = sf.filename

    def pick_slc(port):
        if port and port in slc_by_port: return slc_by_port[port]
        if slc_default is not None:      return slc_default, slc_default_name
        if slc_by_port:                  return next(iter(slc_by_port.values()))
        return None, None

    rdb_items = []
    for rdb_f in rdb_files:
        fname = rdb_f.filename or "unknown.rdb"
        try:    raw = rdb_f.read()
        except: raw = b""
        rdb_items.append((fname, raw))

    def process_rdb(fname, raw):
        port  = _extract_port(fname)
        ferrs = list(errors)
        rdb_sections = {}
        try:
            rdb_sections = parse_rdb_sections_by_relay(raw)
        except Exception as e:
            ferrs.append(f"Failed to parse RDB '{fname}': {e}")

        slc_content, slc_name_used = pick_slc(port)
        check_res, check_errs, _ = _run_checks(
            calc_data, rdb_sections, fr_result, slc_content,
            calc_name=calc_file.filename if calc_file else None,
            rdb_name=fname,
            fr_name=fr_file.filename if fr_file else None,
            slc_name=slc_name_used,
        )
        ferrs.extend(check_errs)
        ok = len(ferrs) == 0
        return {
            "file_name": fname, "port": port,
            "success": ok, "errors": ferrs, "results": check_res,
        }

    group_results = []
    futures = {_FILE_EXECUTOR.submit(process_rdb, fname, raw): fname for fname, raw in rdb_items}
    fname_to_result = {}
    for future in as_completed(futures):
        try:   fname_to_result[futures[future]] = future.result()
        except Exception as e:
            fname_to_result[futures[future]] = {
                "file_name": futures[future], "port": None,
                "success": False, "errors": [str(e)], "results": {},
            }
    for fname, _ in rdb_items:
        group_results.append(fname_to_result[fname])

    total_rdb = len(group_results)
    passed    = sum(1 for r in group_results if r["success"])
    failed    = total_rdb - passed

    all_ok = failed == 0
    return jsonify({
        "success": all_ok,
        "summary": {
            "total_groups": 1, "total_rdb": total_rdb,
            "passed": passed, "failed": failed,
        },
        "groups": [{
            "group_name": group_name,
            "group_key": "group_0",
            "summary": {"total_rdb": total_rdb, "passed": passed, "failed": failed},
            "files": {
                "calc": calc_file.filename if calc_file else None,
                "rdb": [fname for fname, _ in rdb_items],
                "fr":  [fr_file.filename] if fr_file else [],
                "sel": [s.filename for s in slc_files],
            },
            "results": group_results,
        }],
    })


# ─── BATCH GROUP CHECK ────────────────────────────────────────────────────────
_LINE_CHECK_LABELS = {
    "ctr_ptr_check":        "CTR/PTR Check",
    "port_check":           "Port settings Check",
    "conductor_check":      "Conductor Rating Check",
    "safe_load_check":      "Safe Load Check",
    "relay_settings_check": "Relay Settings Check",
}


def _flatten_line_checks(check_res):
    """Convert the dict returned by _run_checks() (keyed by check type) into
    the flat [{check_name, status, message}, …] list the frontend report
    modal (index.html — crm-table rows) expects under r.checks."""
    checks = []
    for key, label in _LINE_CHECK_LABELS.items():
        d = check_res.get(key) or {}
        if not isinstance(d, dict):
            checks.append({"check_name": label, "status": "na", "message": str(d)})
            continue
        p = d.get("pass")
        status = "pass" if p is True else ("fail" if p is False else "na")
        msg = d.get("message") or d.get("error") or ""

        if not msg:
            if key == "ctr_ptr_check":
                parts = []
                for pair, fld in (("CTRW", "ctrw"), ("CTRX", "ctrx"),
                                   ("PTRY", "ptry"), ("PTRZ", "ptrz")):
                    if d.get(f"{fld}_ok") is False:
                        parts.append(f"{pair} mismatch (RDB={d.get('rdb_'+fld)}, Calc={d.get('calc_'+fld)})")
                if d.get("ctrw_x5_ok") is False:
                    parts.append(f"CTRW×5 mismatch (RDB={d.get('ctrw_x5')}, Data Entry E22={d.get('e22')})")
                if d.get("ctrx_x5_ok") is False:
                    parts.append(f"CTRX×5 mismatch (RDB={d.get('ctrx_x5')}, Data Entry E23={d.get('e23')})")
                parts.extend(d.get("name_mismatches") or [])
                msg = "; ".join(parts) if parts else ("All CTR/PTR values match" if status == "pass" else "")
            elif key == "port_check":
                errs = d.get("errors") or []
                msg = "; ".join(errs) if errs else (d.get("ports_in_use") or "")
            elif key == "conductor_check":
                if status == "pass":
                    msg = f"FR Rating ({d.get('fr_rating')} A) matches Calc Rating ({d.get('calc_rating')} A)"
                else:
                    msg = "; ".join(d.get("name_mismatches") or [])
            elif key == "safe_load_check":
                msg = d.get("load_line") or ""

        checks.append({"check_name": label, "status": status, "message": msg})
    return checks


def _process_line_group(idx, group_name, calc_bytes, calc_fname, fr_bytes, fr_fname,
                         rdb_items, slc_items, manual_zle_amp=None, manual_zn_amp=None,
                         manual_fr_rating=None):
    """Run the full Line Checklist pipeline (Calc + FR + RDB(s) + SLC(s)) for
    ONE already-formed group and return the same per-group result shape the
    frontend's renderGroupResults() expects. Shared by /api/batch_group_check
    (groups formed client-side, by folder) and /api/smart_group_check (groups
    formed server-side, from file content) — the grouping method upstream
    doesn't matter to this function, only that a group's files have already
    been decided."""
    group_errors = []

    # ── Parse calc + FR in parallel (both are independent Excel opens) ────
    calc_future = _FILE_EXECUTOR.submit(parse_calc_sheet, calc_bytes) if calc_bytes else None
    fr_future   = _FILE_EXECUTOR.submit(parse_fr_workbook, fr_bytes)  if fr_bytes  else None

    calc_data = None
    if calc_future:
        try:
            calc_data = calc_future.result()
        except Exception as e:
            group_errors.append(f"Failed to parse Calc Sheet: {e}")
    else:
        group_errors.append("Calc Sheet missing for this group.")

    fr_result = None
    if fr_future:
        try:
            fr_result = fr_future.result()
        except Exception as e:
            group_errors.append(f"Failed to parse FR Workbook: {e}")

    slc_by_port  = {}
    slc_default  = None
    slc_default_name = None
    for sf_fname, content in slc_items:
        port = _extract_port(sf_fname)
        if port:
            if port not in slc_by_port:
                slc_by_port[port] = (content, sf_fname)
        else:
            if slc_default is None:
                slc_default = content
                slc_default_name = sf_fname

    def pick_slc(port):
        if port and port in slc_by_port: return slc_by_port[port]
        if slc_default is not None:      return slc_default, slc_default_name
        if slc_by_port:                  return next(iter(slc_by_port.values()))
        return None, None

    if not rdb_items:
        return {
            "group_name": group_name, "group_key": f"group_{idx}",
            "summary": {"total_rdb": 0, "passed": 0, "failed": 0},
            "files": {
                "calc": calc_fname, "rdb": [], "fr": [fr_fname] if fr_fname else [],
                "sel": [sf for sf, _ in slc_items],
            },
            "results": [],
            "warning": "No .rdb files provided for this group.",
            "_g_total": 0, "_g_passed": 0, "_g_failed": 0,
        }

    def process_rdb(fname, raw):
        port  = _extract_port(fname)
        ferrs = list(group_errors)
        rdb_sections = {}
        try:
            rdb_sections = parse_rdb_sections_by_relay(raw)
        except Exception as e:
            ferrs.append(f"Failed to parse RDB '{fname}': {e}")

        slc_content, slc_name_used = pick_slc(port)
        check_res, check_errs, _ = _run_checks(
            calc_data, rdb_sections, fr_result, slc_content,
            calc_name=calc_fname, rdb_name=fname,
            fr_name=fr_fname, slc_name=slc_name_used,
            manual_zle_amp=manual_zle_amp, manual_zn_amp=manual_zn_amp,
            manual_fr_rating=manual_fr_rating,
        )
        ferrs.extend(check_errs)
        ok = len(ferrs) == 0
        return {
            "file_name": fname, "rdb_file": fname, "port": port,
            "success": ok, "errors": ferrs, "results": check_res,
            "checks": _flatten_line_checks(check_res),
        }

    rdb_futures = {_FILE_EXECUTOR.submit(process_rdb, fname, raw): fname
                   for fname, raw in rdb_items}
    fname_to_result = {}
    for future in as_completed(rdb_futures):
        fn = rdb_futures[future]
        try:   fname_to_result[fn] = future.result()
        except Exception as e:
            fname_to_result[fn] = {
                "file_name": fn, "rdb_file": fn, "port": None,
                "success": False, "errors": [str(e)], "results": {}, "checks": [],
            }

    group_results = [fname_to_result[fname] for fname, _ in rdb_items]
    g_total  = len(group_results)
    g_passed = sum(1 for r in group_results if r["success"])
    g_failed = g_total - g_passed

    return {
        "group_name": group_name, "group_key": f"group_{idx}",
        "summary": {"total_rdb": g_total, "passed": g_passed, "failed": g_failed},
        "files": {
            "calc": calc_fname,
            "rdb":  [fname for fname, _ in rdb_items],
            "fr":   [fr_fname] if fr_fname else [],
            "sel":  [sf for sf, _ in slc_items],
        },
        "results": group_results,
        "_g_total": g_total, "_g_passed": g_passed, "_g_failed": g_failed,
    }


@app.route("/api/batch_group_check", methods=["POST"])
def batch_group_check():
    try:
        group_count = int(request.form.get("group_count", 0))
    except (ValueError, TypeError):
        return jsonify({"success": False, "errors": ["Invalid group_count."],
                        "summary": {}, "groups": []}), 400

    if group_count == 0:
        return jsonify({"success": False, "errors": ["No groups received."],
                        "summary": {}, "groups": []}), 400

    group_specs = []
    for idx in range(group_count):
        group_name  = request.form.get(f"group_name_{idx}", f"Group {idx+1}")
        calc_file   = request.files.get(f"calc_sheet_{idx}")
        fr_file     = request.files.get(f"fr_workbook_{idx}")
        rdb_files   = (request.files.getlist(f"rdb_files_{idx}[]") or
                       request.files.getlist(f"rdb_files_{idx}"))
        slc_files   = (request.files.getlist(f"sel_files_{idx}[]") or
                       request.files.getlist(f"sel_files_{idx}"))

        calc_bytes  = calc_file.read()  if calc_file else None
        calc_fname  = calc_file.filename if calc_file else None
        fr_bytes    = fr_file.read()    if fr_file   else None
        fr_fname    = fr_file.filename  if fr_file   else None

        rdb_items = [(f.filename or f"unknown_{i}.rdb", f.read())
                     for i, f in enumerate(rdb_files)]

        slc_items = []
        for sf in slc_files:
            try:    content = sf.read().decode("utf-8", errors="ignore")
            except: content = ""
            slc_items.append((sf.filename, content))

        # Manual ZLE / Zn Amp Limit — only shown/used on screen when this
        # group has no SLC file attached (see frontend "No SLC" fields).
        manual_zle_amp = _safe_float(request.form.get(f"manual_zle_amp_{idx}"))
        manual_zn_amp  = _safe_float(request.form.get(f"manual_zn_amp_{idx}"))
        # Manual FR Rating — used for temp-settings jobs that have no issued
        # FR Workbook. Only actually applied by _run_checks when no FR
        # Workbook file was uploaded for this group (see fr_is_manual there).
        manual_fr_rating = _safe_float(request.form.get(f"manual_fr_rating_{idx}"))

        group_specs.append({
            "idx":        idx,
            "group_name": group_name,
            "calc_bytes": calc_bytes,
            "calc_fname": calc_fname,
            "fr_bytes":   fr_bytes,
            "fr_fname":   fr_fname,
            "rdb_items":  rdb_items,
            "slc_items":  slc_items,
            "manual_zle_amp": manual_zle_amp,
            "manual_zn_amp":  manual_zn_amp,
            "manual_fr_rating": manual_fr_rating,
        })

    idx_to_future = {
        spec["idx"]: _GROUP_EXECUTOR.submit(
            _process_line_group, spec["idx"], spec["group_name"],
            spec["calc_bytes"], spec["calc_fname"], spec["fr_bytes"], spec["fr_fname"],
            spec["rdb_items"], spec["slc_items"],
            spec["manual_zle_amp"], spec["manual_zn_amp"],
            spec["manual_fr_rating"],
        )
        for spec in group_specs
    }
    idx_to_out = {}
    for idx, future in idx_to_future.items():
        try:   idx_to_out[idx] = future.result()
        except Exception as e:
            idx_to_out[idx] = {
                "group_name": f"Group {idx+1}", "group_key": f"group_{idx}",
                "summary": {"total_rdb": 0, "passed": 0, "failed": 0},
                "files": {}, "results": [],
                "error": str(e),
                "_g_total": 0, "_g_passed": 0, "_g_failed": 0,
            }

    all_groups_out = []
    total_rdb_all = passed_all = failed_all = 0
    for idx in range(group_count):
        out = idx_to_out[idx]
        total_rdb_all += out.pop("_g_total",  0)
        passed_all    += out.pop("_g_passed", 0)
        failed_all    += out.pop("_g_failed", 0)
        all_groups_out.append(out)

    return jsonify({
        "success": failed_all == 0,
        "summary": {
            "total_groups": len(all_groups_out),
            "total_rdb":    total_rdb_all,
            "passed":       passed_all,
            "failed":       failed_all,
        },
        "groups": all_groups_out,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# CONTENT-BASED GROUPING ENGINE  — /api/smart_group_check
#
# The batch_group_check flow above requires files to already be sorted into
# groups by FOLDER STRUCTURE before upload (see the disabled /api/check_multi
# note: "Grouping MUST be based on root folder structure only"). That was a
# deliberate constraint at the time, but it means a Calc/RDB/FR/SLC set that
# isn't filed under one cleanly-named job folder never gets grouped correctly
# — the naming convention IS the grouping logic.
#
# This endpoint replaces that: it reads every uploaded file's own CONTENT
# first, extracts an identifying "line key" from it, and groups files whose
# keys match — regardless of what folder or filename they arrived with.
#
#   Calc sheet / FR workbook  -> Facility Rating tab: "FALLS - HAMRICK 161 kV
#                                 (641)" gives station pair + voltage + line #.
#   RDB                       -> RID setting in the bare Group-1 section
#                                 (same format: "Falls,161,Hamrick 161kV,").
#   SLC                       -> plain safe-load text, carries NO line-name
#                                 content — this is the deliberate exception.
#                                 SLC files are attached to an already-formed
#                                 group using filename/path proximity only,
#                                 same as the old engine, AFTER Calc/FR/RDB
#                                 have been grouped by content.
# ═══════════════════════════════════════════════════════════════════════════════

_CONTENT_KEY_STOPWORDS = {
    "THE","AND","FDR","FEEDER","LINE","TIE","MAIN","BUS","SUB","SUBSTATION",
    "STATION","GRID","KV","RELAY","RELAYING","PROTECTION","SETTINGS","SETTING",
    "WORKBOOK","SUMMARY","CALC","CALCS","CALCULATION","RATING","RATINGS",
    "FACILITY","SAFE","LOAD","FILE","DATA","ENTRY","SEL","GE","ABB","PRC",
    "REV","REVISION","AS","FOUND","LEFT","ISSUED","FOR","OF","NEW","OLD",
}

_RE_FACILITY_STRING = re.compile(
    r'([A-Za-z][A-Za-z0-9\'\.]*(?:\s+[A-Za-z0-9\'\.]+)*)\s*-\s*'
    r'([A-Za-z][A-Za-z0-9\'\.]*(?:\s+[A-Za-z0-9\'\.]+)*?)\s*'
    r'(\d{2,3}(?:\.\d+)?)\s*kV(?:\s*\((\d{3,4})\))?',
    re.IGNORECASE,
)
_RE_RID_STRING = re.compile(
    # RID is written into the RDB free-form by whoever settings-engineered
    # the file, so both layouts show up in real files: "Falls,161,Hamrick"
    # (bare voltage) AND "Falls, 161kV, Hamrick" (kV glued onto the number,
    # e.g. the actual RID field in these Falls-Hamrick RDBs). The old
    # pattern required a bare \d{2,3} directly before the second comma, so
    # it silently failed to match the "161kV" form and RDB content-keys
    # came back None on real files. "k?V?" makes the unit optional either
    # way without a second regex.
    r'([A-Za-z][A-Za-z\']*)\s*,\s*(\d{2,3})\s*k?V?\s*,\s*([A-Za-z][A-Za-z\']*)',
    re.IGNORECASE,
)
_RE_BARE_LINE_NUM = re.compile(r'\((\d{3,4})\)')


def _content_key_tokens(*words):
    """Uppercase, de-noise, and de-duplicate a set of station-name words into
    the token set used for content-key overlap matching."""
    out = set()
    for w in words:
        if not w:
            continue
        for tok in re.split(r'[^A-Za-z0-9]+', str(w)):
            tok = tok.strip().upper()
            if len(tok) < 3 or tok in _CONTENT_KEY_STOPWORDS or tok.isdigit():
                continue
            out.add(tok)
    return out


def _parse_facility_string(s):
    """'FALLS - HAMRICK 161 kV (641)' -> {stations:{'FALLS','HAMRICK'},
    voltage:'161', line_number:'641'}. Returns None if the string doesn't
    match the expected station-pair pattern."""
    if not s:
        return None
    m = _RE_FACILITY_STRING.search(str(s))
    if not m:
        return None
    st1, st2, volt, line_no = m.groups()
    return {
        "stations": _content_key_tokens(st1, st2),
        "voltage": volt,
        "line_number": line_no,
    }


def _parse_rid_string(s):
    """'Falls,161,Hamrick 161kV,' -> same shape as _parse_facility_string.
    The RID field is a comma-delimited "Relay Identifier" written directly
    into the RDB by the settings engineer — reading it means matching
    doesn't depend on the RDB filename following any convention at all."""
    if not s:
        return None
    m = _RE_RID_STRING.search(str(s))
    if not m:
        return None
    st1, volt, st2 = m.groups()
    return {
        "stations": _content_key_tokens(st1, st2),
        "voltage": volt,
        "line_number": None,
    }


def _extract_fr_facility_info(wb):
    """Scan every sheet of an FR/Calc workbook for the 'Facility Rating
    Workbook' summary block (Facility / Voltage / Grid Assigned / Line
    Number columns) and return {stations, voltage, line_number} from it.

    Falls back to a bare "A-B ###kV" facility-string match anywhere in the
    first 40 rows of any sheet if no sheet has that table — real Calc
    Sheet workbooks (as opposed to the separate FR workbook) usually do
    NOT carry the Facility Rating Workbook tab at all; the only in-file
    trace of the station pair is a label like "TOTAL- Falls-Hamrick
    161kV" on the Aspen Impedances tab. Without this fallback, Calc
    Sheet workbooks always came back with key=None, so they could never
    actually be matched to their FR/RDB files by content — the fallback
    is what makes "group by content" work at all for a Calc Sheet.
    Returns None only when neither the table nor a bare facility string
    turns up anywhere in the workbook.

    NOTE: on a read_only workbook, ws.cell(row=r, column=c) is NOT random
    access — each call reseeks the underlying XML stream from the top of
    the sheet, so hundreds of individual .cell() calls (40 rows x 15 cols,
    across every sheet) turned a single large Calc workbook (30+ sheets)
    into a 40-50+ second scan, and that's what "reading files and grouping
    by content" was actually stuck on with a real folder of files. Reading
    each sheet's rows ONCE via iter_rows(values_only=True) — which is what
    read_only mode is built for — does the same lookup in well under a
    second per workbook.
    """
    fallback = None

    def _cell(row_vals, col):
        return row_vals[col - 1] if col and 1 <= col <= len(row_vals) else None

    for sname in wb.sheetnames:
        try:
            ws = wb[sname]
        except Exception:
            continue
        cap = min(ws.max_row or 40, 40)
        try:
            rows = list(ws.iter_rows(min_row=1, max_row=cap, max_col=15, values_only=True))
        except Exception:
            continue

        header_row_idx = None
        col_map = {}
        for i, row_vals in enumerate(rows):
            labels = {str(v).strip().lower(): c for c, v in enumerate(row_vals, start=1) if isinstance(v, str)}
            if "facility" in labels and any("line number" in k for k in labels):
                header_row_idx = i
                col_map = {
                    "facility":    labels.get("facility"),
                    "voltage":     next((c for k, c in labels.items() if k.startswith("voltage")), None),
                    "line_number": next((c for k, c in labels.items() if "line number" in k), None),
                }
                break
            if fallback is None:
                for v in row_vals:
                    if isinstance(v, str):
                        parsed_fb = _parse_facility_string(v)
                        if parsed_fb:
                            fallback = parsed_fb
                            break

        if header_row_idx is None:
            continue

        # Data is usually the very next non-blank row.
        for i in range(header_row_idx + 1, min(header_row_idx + 5, len(rows))):
            row_vals = rows[i]
            fac_val = _cell(row_vals, col_map.get("facility"))
            if not fac_val:
                continue
            parsed = _parse_facility_string(fac_val)
            if not parsed:
                # Facility cell present but didn't match "A - B ### kV"
                # pattern — still usable if a separate Line Number column
                # gives us the discriminator and the raw text gives tokens.
                parsed = {"stations": _content_key_tokens(fac_val), "voltage": None, "line_number": None}
            ln_val = _cell(row_vals, col_map.get("line_number"))
            if ln_val not in (None, "", "N/A"):
                parsed["line_number"] = str(ln_val).strip()
            if not parsed["line_number"]:
                m = _RE_BARE_LINE_NUM.search(str(fac_val))
                if m:
                    parsed["line_number"] = m.group(1)
            if not parsed.get("voltage"):
                v_val = _cell(row_vals, col_map.get("voltage"))
                if v_val not in (None, ""):
                    parsed["voltage"] = str(v_val).strip()
            return parsed
    return fallback


def _get_rdb_relay_id(rdb_sections, _section_order=None):
    """RID lives in the bare Group-1 section alongside TID — a human-
    readable "Station,Voltage,Station kV," string written directly into
    the RDB, independent of the filename."""
    so = _section_order if _section_order is not None else _build_section_order(rdb_sections)
    rid = _get_rdb_setting_exact(rdb_sections, "RID", _section_order=so)
    return rid.strip() if rid else None


def _content_key_for_rdb(raw_bytes):
    try:
        rdb_sections = parse_rdb_sections_by_relay(raw_bytes)
    except Exception:
        return None, {}
    rid = _get_rdb_relay_id(rdb_sections)
    key = _parse_rid_string(rid) if rid else None
    return key, rdb_sections


def _keys_match(key_a, key_b):
    """True if two content keys belong to the same line. Line number is the
    hard discriminator when both sides have one (mirrors the old filename
    engine's L-NNN rule); otherwise fall back to station-token comparison.

    This used to accept a match on 50% token overlap, e.g. two DIFFERENT
    two-terminal lines like {AUDUBON, COMPANY} and {AUDUBON, CONWAY} share
    exactly one token ("AUDUBON" — a substation name reused across many
    unrelated lines) which is >= 50% of a 2-element set, so every
    "Audubon-something" line in a batch silently merged into one group.
    Requiring the SMALLER station set to be fully contained in the larger
    one means both endpoints of a two-terminal pair have to agree (a
    shared hub name alone is no longer enough), while still tolerating a
    3rd noise token picked up on one side. A single-token set is even more
    ambiguous (that lone word is very often just the shared hub name), so
    it's only trusted against another single-token set with an EXACT
    match — never as a subset of a larger, more specific set.
    """
    if not key_a or not key_b:
        return False
    ln_a, ln_b = key_a.get("line_number"), key_b.get("line_number")
    if ln_a and ln_b:
        return ln_a == ln_b
    sa, sb = key_a.get("stations") or set(), key_b.get("stations") or set()
    if not sa or not sb:
        return False
    smaller, larger = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if len(smaller) < 2:
        return sa == sb
    return smaller <= larger


def _group_label_for_key(key, fallback):
    st = sorted(key.get("stations") or [])
    if len(st) >= 2:
        label = f"{st[0].title()} - {st[1].title()}"
    elif st:
        label = st[0].title()
    else:
        label = fallback
    if key.get("line_number"):
        label += f" ({key['line_number']})"
    return label


def _slc_fallback_tokens(filename, path_hint):
    """SLC files carry no line-identifying content (the deliberate exception
    noted above) — fall back to whatever the old filename/path engine used:
    normalized tokens from the filename and its folder path."""
    base = _strip_known_extension(filename)
    text = f"{path_hint or ''} {base}"
    return _content_key_tokens(*re.split(r'[\\/]+', text))


@app.route("/api/smart_group_check", methods=["POST"])
def smart_group_check():
    """
    Accepts ALL files from a folder upload, unsorted — no per-group form
    fields required. Each file's role (calc/FR/RDB/SLC) and group are
    determined from its own content, not its filename or folder path.

    Form fields:
      files[]  — every uploaded file (any mix of calc/.xlsx, FR/.xlsx,
                  .rdb, SLC/.txt), any order.
      paths[]  — OPTIONAL, parallel array of each file's original relative
                  path (webkitRelativePath). Only used as a fallback signal
                  for attaching SLC files (which have no matchable content)
                  and for labeling files that couldn't be content-matched.
    """
    files = request.files.getlist("files[]") or request.files.getlist("files")
    if not files:
        return jsonify({"success": False, "errors": ["No files received."],
                        "summary": {}, "groups": [], "ungrouped": []}), 400
    raw_paths = request.form.getlist("paths[]") or request.form.getlist("paths")
    paths = raw_paths if len(raw_paths) == len(files) else [None] * len(files)

    calc_candidates, fr_candidates, rdb_candidates, slc_candidates, skipped = [], [], [], [], []

    for f, path_hint in zip(files, paths):
        fname = f.filename or "unnamed"
        ext = os.path.splitext(fname)[1].lower()
        try:
            data = f.read()
        except Exception:
            skipped.append({"filename": fname, "reason": "unreadable"})
            continue
        if not data:
            skipped.append({"filename": fname, "reason": "empty"})
            continue

        # RDB detection: by extension OR by the OLE compound-file signature.
        # RDBs frequently arrive with NO extension at all (see
        # _strip_known_extension's docstring — this is an established,
        # documented fact about how these files actually show up on disk),
        # so extension alone silently misses them. Checking the OLE magic
        # bytes catches those regardless of what they're named. This must
        # run BEFORE the text/SLC branch below — without it, an
        # extensionless RDB falls into that branch, gets decoded as
        # "text", and is scanned by regex against binary garbage, which is
        # slow-to-catastrophically-slow depending on content.
        if ext == ".rdb" or data[:8] == b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1":
            rdb_candidates.append({"filename": fname, "bytes": data, "path": path_hint})
            continue

        if ext in (".xlsx", ".xlsm"):
            # Single workbook open, reused for both the calc/FR classification
            # AND the content-key extraction below (previously opened twice
            # per file — tripled with the parse in _process_line_group later
            # — which was the other big contributor to the multi-minute
            # stall on larger workbooks).
            try:
                wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
            except Exception:
                skipped.append({"filename": fname, "reason": "could not open as Excel workbook"})
                continue
            try:
                is_calc = any("data entry" in s.lower() for s in wb.sheetnames)
                key = _extract_fr_facility_info(wb)
            finally:
                wb.close()
            item = {"filename": fname, "bytes": data, "path": path_hint, "key": key}
            (calc_candidates if is_calc else fr_candidates).append(item)
            continue

        if ext in (".txt", ".dat", ""):
            # Plain-text SLC candidate. Guard against binary content before
            # ever decoding+regex-scanning it: SLC files are always small,
            # mostly-printable text — if this doesn't look like that, it's
            # not an SLC (could be an extensionless file of some other
            # binary type), so skip it rather than risk a slow/pathological
            # regex match against garbage.
            sample = data[:4096]
            printable = sum(1 for b in sample if 9 <= b <= 13 or 32 <= b <= 126)
            if len(data) > 2_000_000 or (sample and printable / len(sample) < 0.85):
                skipped.append({"filename": fname, "reason": "not recognized as text (binary content)"})
                continue
            try:
                text = data.decode("utf-8", errors="ignore")
            except Exception:
                text = ""
            if any(p.search(text) for p in _SAFE_LOAD_PATTERNS):
                slc_candidates.append({"filename": fname, "text": text, "path": path_hint})
            else:
                skipped.append({"filename": fname, "reason": "unrecognized file type"})
            continue

        skipped.append({"filename": fname, "reason": "unrecognized file type"})

    # ── Extract content keys (in parallel — each is an independent file open) ──
    # Calc/FR items already carry their "key" from the single-open pass
    # above; only RDB keys still need extracting here.
    def _key_rdb(item):
        key, sections = _content_key_for_rdb(item["bytes"])
        return key

    rdb_futures  = {_FILE_EXECUTOR.submit(_key_rdb, it): it for it in rdb_candidates}
    for fut, it in rdb_futures.items():
        try:    it["key"] = fut.result()
        except Exception: it["key"] = None

    # ── Form groups: each group anchored on one Calc sheet (a Line Checklist
    #    always needs a Calc Sheet), then absorb matching FR/RDB by content
    #    key. Calc files with no usable content key each become their own
    #    single-item group (better to surface a group needing manual fixing
    #    than to silently merge unrelated lines). ──────────────────────────
    groups = []
    for calc in calc_candidates:
        groups.append({
            "key": calc["key"],
            "calc": calc,
            "fr": None,
            "rdb": [],
            "slc": [],
        })

    unmatched_fr, unmatched_rdb = [], []
    for fr in fr_candidates:
        target = next((g for g in groups if g["fr"] is None and _keys_match(g["key"], fr["key"])), None)
        if target:
            target["fr"] = fr
        else:
            unmatched_fr.append(fr)

    for rdb in rdb_candidates:
        target = next((g for g in groups if _keys_match(g["key"], rdb["key"])), None)
        if target:
            target["rdb"].append(rdb)
        else:
            unmatched_rdb.append(rdb)

    # ── SLC: no line-identifying content — attach via filename/path token
    #    overlap against each group's ALREADY-CONTENT-MATCHED station set. ──
    unmatched_slc = []
    for slc in slc_candidates:
        slc_tokens = _slc_fallback_tokens(slc["filename"], slc["path"])
        best, best_score = None, 0
        for g in groups:
            st = (g["key"] or {}).get("stations") or set()
            score = len(st & slc_tokens)
            if score > best_score:
                best, best_score = g, score
        if best and best_score > 0:
            best["slc"].append(slc)
        else:
            unmatched_slc.append(slc)

    # ── Build the group_specs _process_line_group() expects ────────────────
    group_specs = []
    for idx, g in enumerate(groups):
        label = _group_label_for_key(g["key"], g["calc"]["filename"]) if g["key"] else g["calc"]["filename"]
        group_specs.append({
            "idx": idx,
            "group_name": label,
            "calc_bytes": g["calc"]["bytes"],
            "calc_fname": g["calc"]["filename"],
            "fr_bytes":   g["fr"]["bytes"] if g["fr"] else None,
            "fr_fname":   g["fr"]["filename"] if g["fr"] else None,
            "rdb_items":  [(r["filename"], r["bytes"]) for r in g["rdb"]],
            "slc_items":  [(s["filename"], s["text"]) for s in g["slc"]],
        })

    idx_to_future = {
        spec["idx"]: _GROUP_EXECUTOR.submit(
            _process_line_group, spec["idx"], spec["group_name"],
            spec["calc_bytes"], spec["calc_fname"], spec["fr_bytes"], spec["fr_fname"],
            spec["rdb_items"], spec["slc_items"],
        )
        for spec in group_specs
    }
    idx_to_out = {}
    for idx, future in idx_to_future.items():
        try:
            idx_to_out[idx] = future.result()
        except Exception as e:
            idx_to_out[idx] = {
                "group_name": group_specs[idx]["group_name"], "group_key": f"group_{idx}",
                "summary": {"total_rdb": 0, "passed": 0, "failed": 0},
                "files": {}, "results": [], "error": str(e),
                "_g_total": 0, "_g_passed": 0, "_g_failed": 0,
            }

    all_groups_out = []
    total_rdb_all = passed_all = failed_all = 0
    for idx in range(len(group_specs)):
        out = idx_to_out[idx]
        total_rdb_all += out.pop("_g_total",  0)
        passed_all    += out.pop("_g_passed", 0)
        failed_all    += out.pop("_g_failed", 0)
        all_groups_out.append(out)

    ungrouped = (
        [{"filename": it["filename"], "type": "fr",  "reason": "no matching Calc Sheet content key"} for it in unmatched_fr] +
        [{"filename": it["filename"], "type": "rdb", "reason": "no matching Calc Sheet content key"} for it in unmatched_rdb] +
        [{"filename": it["filename"], "type": "slc", "reason": "no station-name overlap with any group"} for it in unmatched_slc] +
        [{"filename": it["filename"], "reason": it["reason"]} for it in skipped]
    )

    return jsonify({
        "success": failed_all == 0 and not ungrouped,
        "summary": {
            "total_groups": len(all_groups_out),
            "total_rdb":    total_rdb_all,
            "passed":       passed_all,
            "failed":       failed_all,
        },
        "groups": all_groups_out,
        "ungrouped": ungrouped,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# FEEDER CHECK ENDPOINT  — /api/feeder_check
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/feeder_check", methods=["POST"])
def feeder_check():
    """
    Accepts:
      rdb_files[]   — one or more RDB files (mandatory)
      rec_file      — Recommendations Excel file (optional)

    Groups RDB files by feeder name (e.g. MD01, MD04) extracted from
    the filename or RDB internal settings.  Each feeder becomes one
    group; multiple RDBs (SEL351, SEL551 …) inside the same feeder
    are processed together.  The recommendations Excel is shared across
    all groups.

    Returns grouped results that mirror the batch_group_check structure.
    """
    rdb_files = request.files.getlist("rdb_files[]") or request.files.getlist("rdb_files")
    rec_file  = request.files.get("rec_file")

    if not rdb_files:
        return jsonify({
            "success": False,
            "errors": ["At least one RDB file is required."],
            "groups": [],
            "summary": {"total_groups": 0, "total_rdb": 0, "passed": 0, "failed": 0},
        }), 400

    # ── Parse recommendations Excel + all RDB files in parallel ─────────────
    rec_future = None
    if rec_file:
        rec_bytes  = rec_file.read()
        rec_future = _FILE_EXECUTOR.submit(_parse_recommendations_excel, rec_bytes)

    def _parse_one_rdb(rdb_f):
        fname = rdb_f.filename or "unknown.rdb"
        try:    raw = rdb_f.read()
        except: raw = b""
        rdb_sections: dict = {}
        rdb_error          = None
        try:
            rdb_sections = parse_rdb_sections_by_relay(raw)
        except Exception as exc:
            rdb_error = f"Failed to parse RDB '{fname}': {exc}"
        feeder_name = _extract_feeder_name_from_rdb(rdb_sections, fname)
        return {"fname": fname, "rdb_sections": rdb_sections,
                "feeder_name": feeder_name, "rdb_error": rdb_error}

    # Submit all RDB parses in parallel
    rdb_parse_futures = [_FILE_EXECUTOR.submit(_parse_one_rdb, rdb_f) for rdb_f in rdb_files]

    # Collect recommendations result
    recommendations_data: dict = {}
    has_recommendations        = False
    rec_parse_error            = None
    if rec_future:
        try:
            recommendations_data = rec_future.result()
            has_recommendations  = True
        except Exception as exc:
            rec_parse_error = f"Failed to parse Recommendations file: {exc}"

    # Collect all parsed RDBs (in original order)
    parsed_rdbs = []
    for fut in rdb_parse_futures:
        try:
            parsed_rdbs.append(fut.result())
        except Exception as exc:
            parsed_rdbs.append({"fname": "unknown.rdb", "rdb_sections": {},
                                 "feeder_name": None, "rdb_error": str(exc)})

    # ── Group by station+feeder (preserve insertion order) ───────────────────
    # Key = "STATION_MDxx" — ensures files from different stations with the
    # same feeder ID are NOT merged together.
    # If feeder name cannot be determined use the bare filename as group key.
    feeder_groups: dict = {}   # ordered: composite_key -> [parsed_rdb, …]
    for item in parsed_rdbs:
        feeder_id    = item["feeder_name"]
        station_name = _extract_station_name_from_filename(item["fname"])
        if feeder_id:
            key = _make_group_key(station_name, feeder_id)
        else:
            key = _strip_known_extension(item["fname"]).upper()
        item["station_name"] = station_name
        item["group_key_used"] = key
        feeder_groups.setdefault(key, []).append(item)

    # ── Process each feeder group — fully parallel ───────────────────────────
    all_groups_out = []
    overall_errors = []
    if rec_parse_error:
        overall_errors.append(rec_parse_error)

    total_rdb_all = passed_all = failed_all = skipped_all = 0

    def _process_feeder_item(item, feeder_key, rec_data, has_rec):
        """Process a single RDB item inside a feeder group. Thread-safe."""
        fname        = item["fname"]
        rdb_sections = item["rdb_sections"]
        rdb_error    = item["rdb_error"]

        if rdb_error:
            return {"file_name": fname, "success": False, "error": rdb_error, "checks": []}

        try:
            checks = _run_feeder_checks(rdb_sections, fname, rec_data, has_rec)
            statuses     = [c["status"] for c in checks]
            file_success = all(s in ("pass", "info", "skipped") for s in statuses)
            is_skipped   = all(s == "skipped" for s in statuses) and bool(statuses)
            return {"file_name": fname, "success": file_success, "is_skipped": is_skipped, "error": None, "checks": checks}
        except Exception as exc:
            return {"file_name": fname, "success": False, "error": f"Error running checks on '{fname}': {exc}", "checks": []}

    def _process_feeder_group(feeder_key, items, rec_data, has_rec):
        """Process all RDB items in one feeder group in parallel."""
        futures_map = {
            _FILE_EXECUTOR.submit(_process_feeder_item, item, feeder_key, rec_data, has_rec): i
            for i, item in enumerate(items)
        }
        idx_to_result = {}
        for future in as_completed(futures_map):
            i = futures_map[future]
            try:
                idx_to_result[i] = future.result()
            except Exception as exc:
                idx_to_result[i] = {
                    "file_name": items[i]["fname"], "success": False,
                    "error": str(exc), "checks": [],
                }
        group_results = [idx_to_result[i] for i in range(len(items))]

        g_total   = len(group_results)
        g_skipped = sum(1 for r in group_results if r.get("is_skipped"))
        g_passed  = sum(1 for r in group_results if r["success"] and not r.get("is_skipped"))
        g_failed  = g_total - g_passed - g_skipped

        _first_item   = items[0] if items else {}
        _feeder_id    = _first_item.get("feeder_name") or feeder_key
        _station_name = _first_item.get("station_name", "")
        _all_fnames   = " ".join(it["fname"] for it in items)
        _group_type   = _classify_rdb_group(_all_fnames)

        group_errors = []
        for r in group_results:
            if r.get("error"):
                group_errors.append(r["error"])
            elif not r["success"]:
                for fc in r.get("checks", []):
                    if fc["status"] == "fail":
                        group_errors.append(
                            f"[{feeder_key}][{r['file_name']}] {fc['check_name']}: "
                            f"{fc.get('error') or 'Failed'}"
                        )

        return {
            "group_name":   feeder_key,
            "group_key":    re.sub(r'\W+', '_', feeder_key.lower()),
            "feeder_name":  _feeder_id,
            "station_name": _station_name,
            "group_type":   _group_type,
            "summary": {"total_rdb": g_total, "passed": g_passed, "failed": g_failed, "skipped": g_skipped},
            "files": {
                "rdb": [it["fname"] for it in items],
                "rec": rec_file.filename if rec_file else None,
            },
            "results": group_results,
            "_g_total":   g_total,
            "_g_passed":  g_passed,
            "_g_failed":  g_failed,
            "_g_skipped": g_skipped,
            "_g_errors":  group_errors,
        }

    group_keys = list(feeder_groups.keys())
    group_futures = {
        _GROUP_EXECUTOR.submit(_process_feeder_group, fk, feeder_groups[fk], recommendations_data, has_recommendations): fk
        for fk in group_keys
    }
    key_to_out = {}
    for future in as_completed(group_futures):
        fk = group_futures[future]
        try:
            key_to_out[fk] = future.result()
        except Exception as exc:
            key_to_out[fk] = {
                "group_name": fk, "group_key": re.sub(r'\W+', '_', fk.lower()),
                "feeder_name": fk, "station_name": "", "group_type": "feeder",
                "summary": {"total_rdb": 0, "passed": 0, "failed": 0},
                "files": {"rdb": [], "rec": rec_file.filename if rec_file else None},
                "results": [], "error": str(exc),
                "_g_total": 0, "_g_passed": 0, "_g_failed": 0, "_g_errors": [str(exc)],
            }

    for fk in group_keys:
        out = key_to_out[fk]
        total_rdb_all += out.pop("_g_total",   0)
        passed_all    += out.pop("_g_passed",  0)
        failed_all    += out.pop("_g_failed",  0)
        skipped_all   += out.pop("_g_skipped", 0)
        overall_errors.extend(out.pop("_g_errors", []))
        all_groups_out.append(out)

    return jsonify({
        "success":                 failed_all == 0,
        "has_recommendations":     has_recommendations,
        "recommendations_feeders": list(recommendations_data.keys()),
        "summary": {
            "total_groups": len(all_groups_out),
            "total_rdb":    total_rdb_all,
            "passed":       passed_all,
            "failed":       failed_all,
            "skipped":      skipped_all,
        },
        "groups":  all_groups_out,
        "errors":  overall_errors,
    })


@app.route("/api/chat", methods=["POST", "OPTIONS"])
def chat():
    """
    PGRI AI Assistant endpoint.

    Accepts JSON: { "messages": [ {"role": "user"|"assistant", "content": "..."}, ... ] }
    Returns JSON:  { "reply": "..." }

    The assistant is a rule-based keyword responder seeded with accurate
    knowledge of how the PGRI tools actually work (verified directly
    against the parsing/validation logic in this file), so it can answer
    domain questions entirely offline with no external API dependency.
    """
    if request.method == "OPTIONS":
        resp = jsonify({})
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp, 200
    data = request.get_json(force=True, silent=True) or {}
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "No messages provided"}), 400

    # ── Keyword-based responses ────────────────────────────────────────────
    last_msg = (messages[-1].get("content") or "").lower()

    def _kw(*words): return any(w in last_msg for w in words)

    if _kw("hello", "hi", "hey", "greet"):
        reply = "Hi! I'm the PGRI AI Assistant. Ask me anything about the Compliance Checker, Line Protection Calculation Sheet tool, relay types, PRC standards, or how to use PGRI."
    elif _kw("prc-023", "prc023", "load encroach"):
        reply = ("**PRC-023** (load encroachment) is checked in PGRI by comparing two values:\n\n"
                 "- **Safe Load (A)** — parsed from the uploaded SLC file\n"
                 "- **Conductor Rating (A)** — pulled from the Calc Sheet's FR-derived rating, or the FR Workbook (Section-2/3) if the Calc Sheet value is missing\n\n"
                 "**Pass condition:** Safe Load ≥ Conductor Rating.\n\n"
                 "Common failure causes:\n"
                 "- Safe Load value not found in the SLC file (unsupported text pattern)\n"
                 "- Conductor Rating missing from both the Calc Sheet and FR Workbook\n"
                 "- Safe Load genuinely below the conductor's ampacity rating")
    elif _kw("prc-025", "prc025"):
        reply = ("**PRC-025** ensures generator load rejection does not cause a relay operation on rated load.\n\n"
                 "This standard applies to lines connected to generation. "
                 "PGRI marks it N/A when PRC-023 or PRC-026 is the selected criterion in the Calc Sheet.")
    elif _kw("prc-026", "prc026", "stability", "swing"):
        reply = ("**PRC-026** requires that the relay does not trip for a stable power swing.\n\n"
                 "PGRI verifies the PRC criterion cell in the Calc Sheet Data Entry tab and marks the "
                 "other two standards (023/025) as Not Applicable.")
    elif _kw("sel-551", "551", "overcurrent"):
        reply = ("**SEL-551** is a feeder overcurrent relay that doesn't have the frequency/voltage settings PGRI's Feeder Checker normally looks for (E81, 81D1P, 81D1D, 27B81P).\n\n"
                 "When PGRI detects a SEL-551 RDB, it **skips all four feeder checks** for that file and marks them "
                 "**Not Applicable**, with a note explaining why.")
    elif _kw("sel-411", "411l"):
        reply = ("**SEL-411L** is a transmission line current differential relay.\n\n"
                 "PGRI looks for a sheet tab containing '411l' in the Calc Sheet workbook for relay settings. "
                 "Key settings verified: CTRW, CTRX, PTRY, PTRZ ratios vs RDB values.")
    elif _kw("sel-421", "421"):
        reply = ("**SEL-421** (variants: -5 and -3) is a distance/phase-comparison relay.\n\n"
                 "PGRI prioritises the '421-5' tab over '421-3' when both exist. "
                 "Settings checked include CTRW, CTRX, PTRY, PTRZ and PRC compliance.")
    elif _kw("rdb", ".rdb", "relay database"):
        reply = ("**RDB files** are SEL relay database exports (INI-style sections).\n\n"
                 "PGRI parses them for:\n"
                 "- CTR/PTR settings (CTRW, CTRX, PTRY, PTRZ)\n"
                 "- Port designation (P1–P5, PF from filename)\n"
                 "- Feeder ID (MDxx pattern) for grouping\n"
                 "- Automation settings from L1–L6 and O1 sections\n\n"
                 "If you see an OLE parse error, install **olefile**: `pip install olefile`")
    elif _kw("ctr", "ptr", "ratio"):
        reply = ("**CTR (Current Transformer Ratio)** and **PTR (Potential Transformer Ratio)** "
                 "are cross-verified between the Calc Sheet and the RDB file.\n\n"
                 "PGRI expects:\n"
                 "- CTRW / CTRX in the relay settings tab of the Calc Sheet\n"
                 "- Matching values in the RDB sections\n"
                 "- Tolerance: ±0.01\n\n"
                 "If they don't match, check that the Calc Sheet relay settings tab is for the correct relay model.")
    elif _kw("fr workbook", "facility rating", "fr file", ".fr"):
        reply = ("The **FR Workbook** (Facility Rating) provides the conductor AMP limit.\n\n"
                 "PGRI reads:\n"
                 "- **Section-2**: identifies 'THROUGH' segments\n"
                 "- **Section-3**: conductor AMP values per location\n\n"
                 "The FR rating = minimum of max AMP across all through-segment conductors.\n\n"
                 "If the FR rating is None, verify the Section-2/Section-3 headers are intact.")
    elif _kw("slc", "safe load", ".slc"):
        reply = ("The **SLC file** (Safe Load Calculation) contains the minimum calculated amp line limit "
                 "used for the PRC-023 load encroachment check (Safe Load vs Conductor Rating).\n\n"
                 "PGRI searches for patterns like:\n"
                 "- `Safe Load = <value>`\n"
                 "- `Minimum Calculated AMP LINE LIMIT Rounded [A] = <value>`\n"
                 "- `Min Calc... = <value>`\n"
                 "- `SOTF AMP LINE LIMIT [A] = <value>`\n\n"
                 "If parsing fails, check the SLC file format matches one of these patterns.")
    elif _kw("feeder", "feeder check", "feeder checker"):
        reply = ("The **Feeder Checker** runs up to four checks per RDB file:\n\n"
                 "1. **Under Frequency Status (E81)** — compared against the Recommendations file (Enabled vs Disabled)\n"
                 "2. **Under Frequency Value (81D1P)** — RDB pickup value vs recommended value (±0.011 Hz tolerance)\n"
                 "3. **UF Time Delay (81D1D)** — must be ≥ 6 cycles\n"
                 "4. **VNOM Value Verification (27B81P)** — calculated as (kV×1000/PTR)/√3 × 70%, compared to the RDB value (±0.5 tolerance)\n\n"
                 "If E81 is disabled in the RDB and the Recommendations file also marks it N/A (or no Recommendations file is uploaded), "
                 "checks 2–4 are skipped automatically with an explanatory note.\n\n"
                 "RDB files are grouped by Station + Feeder ID (MDxx). "
                 "A Recommendations file is optional but needed to evaluate most checks beyond E81.\n\n"
                 "SEL-551 relays → all four checks = **Not Applicable** (these settings don't exist in SEL-551 RDBs).")
    elif _kw("line protection", "lp parent", "calc sheet template"):
        reply = ("The **Line Protection Calculation Sheet** tool generates completed Line Protection Calc Sheets.\n\n"
                 "It uses the template file `Line_Protection_Calculation_Sheet_Template.xlsm` "
                 "and populates settings from uploaded relay data.\n\n"
                 "Outputs: filled .xlsm Calc Sheet + Aspen Impedances tab with station name labels in column B.\n\n"
                 "If the template is not found at startup, check it is in the same directory as app.py.")
    elif _kw("electron", "desktop", "packag", "build"):
        reply = ("**Electron Build Notes for PGRI:**\n\n"
                 "- Keep the flat `src/` structure — no `frontend/` or `backend/` subfolders\n"
                 "- `package.json` extraResources must reference paths relative to `src/`\n"
                 "- `main.js` path references must match the flat layout\n"
                 "- Flask starts on port **5051** — ensure no other process occupies it\n"
                 "- preload.js exposes only the APIs needed by the renderer")
    elif _kw("upload", "file", "how to use", "get started", "start"):
        reply = ("**Getting started with PGRI Compliance Checker:**\n\n"
                 "1. Choose a checklist type: **Line Checklist** (single relay group: Calc Sheet + RDB + FR + SLC) "
                 "or **Feeder/Tie/Main Checklist** (bulk-check all feeders in a substation)\n"
                 "2. Click **Upload Folder's Here** and select a folder containing your files "
                 "(works for a single line or multiple lines at once)\n"
                 "3. Click **▶ Run Checks**\n"
                 "4. Review results — download a Combined PDF report\n\n"
                 "Hover any ⓘ icon in the results table for an explanation of that specific check.")
    elif _kw("error", "fail", "issue", "problem", "wrong"):
        reply = ("**Common PGRI errors and fixes:**\n\n"
                 "- *OLE parse error on RDB* → install `olefile`: `pip install olefile`\n"
                 "- *CTR/PTR mismatch* → verify the relay settings tab name contains the relay model string\n"
                 "- *FR rating is None* → check Section-2/Section-3 headers in the FR workbook\n"
                 "- *Safe Load not found* → ensure the SLC file uses a supported pattern (e.g. `Safe Load = <value>`)\n"
                 "- *Deadlock / timeout on batch jobs* → check thread pool sizes; _GROUP_EXECUTOR and _FILE_EXECUTOR are separate pools")
    elif _kw("version", "ver "):
        reply = "PGRI is currently at version **1.0.0** (as set in `APP_VERSION` in app.py). Bump this on every release."
    elif _kw("port", "5051"):
        reply = "PGRI Flask backend runs on **port 5051** (`http://127.0.0.1:5051`). Make sure no other service is using this port."
    else:
        reply = ("I'm the PGRI AI Assistant. I can help with:\n\n"
                 "- **Relay types**: SEL-411L, SEL-421-5/3, SEL-311C/L, SEL-551\n"
                 "- **PRC standards**: PRC-023, PRC-025, PRC-026\n"
                 "- **File requirements**: RDB, Calc Sheet, FR Workbook, SLC\n"
                 "- **Check failures**: CTR/PTR mismatches, load encroachment, feeder checks\n"
                 "- **Tools**: Compliance Checker (Line / Feeder checklists), Line Protection Calculation Sheet generator\n\n"
                 "Ask me anything specific!")

    return jsonify({"reply": reply})


@app.route("/health")
def health():
    """
    Liveness / readiness probe.

    Returns HTTP 200 with ``{"status": "ok", "version": "<APP_VERSION>"}``.
    Used by load-balancers and the desktop auto-updater to confirm the
    backend process is running and responding correctly.
    """
    import platform, sys
    return jsonify({
        "status":  "ok",
        "version": APP_VERSION,
        "python":  sys.version.split()[0],
        "platform": platform.system(),
    })


if __name__ == "__main__":
    # ── Startup banner ────────────────────────────────────────────────────
    import sys
    bar = "═" * 58
    print(f"\n{bar}")
    print(f"  PhasorGrid Relay Intelligence  |  Combined Suite")
    print(f"  Version    : {APP_VERSION}")
    print(f"  Python     : {sys.version.split()[0]}")
    print(f"  Listen     : http://127.0.0.1:5051")
    print(f"  OLE lib    : {'olefile available ✓' if _OLEFILE_AVAILABLE else 'olefile NOT installed'}")
    import os as _os
    _tmpl = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'Line_Protection_Calculation_Sheet_Template.xlsm')
    print(f"  LP Template: {'found ✓' if _os.path.exists(_tmpl) else 'NOT FOUND ✗'}")
    print(f"{bar}\n")
    # ── Launch Flask (production-safe: no reloader, no debug) ─────────────
    app.run(debug=False, port=5051, host="127.0.0.1", use_reloader=False)