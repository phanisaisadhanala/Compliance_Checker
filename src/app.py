from flask import Flask, request, jsonify
from flask_cors import CORS
import openpyxl
import re
import io

app = Flask(__name__)
CORS(app)

# ─── VERSION (bump this on every release) ───────────────────────────────────
APP_VERSION = "1.3.0"

ALL_PRC = ["PRC-023", "PRC-025", "PRC-026"]

def _safe_int(val):
    if val is None: return None
    try: return int(float(str(val).strip()))
    except: return None

def _safe_float(val):
    if val is None: return None
    try: return float(str(val).strip())
    except: return None

def extract_rdb_text(raw_bytes):
    try:
        import olefile, io as _io
        parts = []
        ole = olefile.OleFileIO(_io.BytesIO(raw_bytes))
        for entry in ole.listdir():
            try:
                data = ole.openstream(entry).read()
                try: parts.append(data.decode("utf-8", errors="replace"))
                except: parts.append(data.decode("latin-1", errors="replace"))
            except: pass
        ole.close()
        return "\n".join(parts)
    except: pass
    try: return raw_bytes.decode("utf-8", errors="replace")
    except: return raw_bytes.decode("latin-1", errors="replace")

def parse_rdb_sections(content):
    sections, current, settings = {}, None, {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line: continue
        sec = re.match(r'^\[([A-Za-z0-9_]+)\]', line)
        if sec:
            if current is not None: sections[current] = settings
            current, settings = sec.group(1).upper(), {}
            continue
        kv = re.match(r'^([A-Za-z0-9_]+),\s*"([^"]*)"', line)
        if kv and current is not None:
            settings[kv.group(1).upper()] = kv.group(2).strip()
    if current is not None: sections[current] = settings
    return sections

_RELAY_SHEET_KEYWORDS = ["sel-411l"]

def _read_relay_settings_tab(wb):
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
        for row in ws.iter_rows(min_row=1, max_row=ws.max_row,
                                 min_col=1, max_col=3, values_only=True):
            name = row[0]
            val  = row[2]
            if not name or not isinstance(name, str): continue
            key = name.strip().upper()
            if not key: continue
            if isinstance(val, str) and val.startswith("#"): val = None
            d[key] = val
        if "CTRW" in d:
            return d, sname
    return {}, None

def parse_calc_sheet(file_bytes):
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    result = {
        "nominal_kv": None, "calc_ctrw": None, "calc_ctrx": None,
        "calc_ptry": None, "calc_ptrz": None, "ctr_w_primary": None,
        "ctr_x_primary": None, "ptry": None, "conductor_rating_from_fr": None,
        "prc_criteria_raw": None, "relay_sheet_used": None,
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
        if None in (result["ctr_w_primary"], result["ctr_x_primary"],
                    result["ptry"], result["conductor_rating_from_fr"]):
            for row in ws.iter_rows(values_only=True):
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
    relay_d, rsheet = _read_relay_settings_tab(wb)
    result["relay_sheet_used"] = rsheet
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
    wb.close()
    return result

def parse_fr_workbook(file_bytes):
    wb   = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws   = wb.active
    rows = list(ws.iter_rows(values_only=True))
    through_segs, in_s2, last_seg = set(), False, None
    for row in rows:
        if isinstance(row[0], int) and row[0] == 2 and row[1] and "limiting" in str(row[1]).lower():
            in_s2 = True; continue
        if isinstance(row[0], int) and row[0] == 3: in_s2 = False; break
        if not in_s2: continue
        if row[0] in ('A','B','C','D','E','F','G','H','I','J','K','Z') and row[1]:
            last_seg = str(row[1]).strip()
        for ci in (0, 1):
            if ci < len(row) and row[ci] and str(row[ci]).strip().upper() == "THROUGH":
                if last_seg: through_segs.add(last_seg)
    in_s3, current_loc, loc_amps = False, None, {}
    for row in rows:
        if isinstance(row[0], int) and row[0] == 3 and row[1] and "component" in str(row[1]).lower():
            in_s3 = True; continue
        if isinstance(row[0], int) and row[0] == 4: break
        if not in_s3: continue
        loc_val = row[1] if len(row) > 1 else None
        if loc_val and isinstance(loc_val, str) and loc_val.strip():
            s = loc_val.strip()
            if s.lower() not in {'station or line', 'line panel', 'model information'}:
                current_loc = s
        device = str(row[2]).strip() if len(row) > 2 and row[2] else ""
        amp    = row[5] if len(row) > 5 else None
        if "conductor" in device.lower() and isinstance(amp, (int, float)) and amp > 0:
            loc = current_loc or "_unknown_"
            loc_amps.setdefault(loc, []).append(int(amp))
    def _norm(s): return re.sub(r'\s*-\s*', '-', str(s).lower().strip())
    through_n = {_norm(t) for t in through_segs}
    through_c = {loc: amps for loc, amps in loc_amps.items() if _norm(loc) in through_n} or loc_amps
    per_max   = {loc: max(amps) for loc, amps in through_c.items()}
    fr_rating = min(per_max.values()) if per_max else None
    wb.close()
    return {"fr_rating": fr_rating, "through_segments": sorted(through_segs), "conductor_detail": through_c}

def parse_safe_load(content):
    patterns = [
        r'Safe\s*Load\s*=\s*(\d+)',
        r'SAFE\s*LOAD\s*=\s*(\d+)',
        r'Minimum\s+Calculated\s+AMP\s+LINE\s+LIMIT\s+Rounded\s*\[A\]\s*=\s*(\d+)',
        r'Min(?:imum)?\s+Calc[^\n]*=\s*(\d+)',
        r'SOTF\s+AMP\s+LINE\s+LIMIT\s*\[A\]\s*=\s*(\d+)',
    ]
    for pat in patterns:
        m = re.search(pat, content, re.IGNORECASE)
        if m: return int(m.group(1))
    return None

def classify_prc(raw):
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

def nums_equal(a, b):
    try: return abs(float(a) - float(b)) < 0.01
    except: return False

def _run_checks(calc_data, rdb_sections, fr_result, sel_content):
    results, all_errors = {}, []

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
            ptrz_warn = ("") if ptrz_ok is False and c_ptrz is not None else None
            passed = all(v is True or v is None for v in [ctrw_ok, ctrx_ok, ptry_ok, ptrz_ok, ctrw_x5_ok, ctrx_x5_ok])
            results["ctr_ptr_check"] = {
                "pass": passed, "relay_sheet": relay_sheet,
                "rdb_ctrw": rdb_ctrw, "calc_ctrw": c_ctrw, "ctrw_ok": ctrw_ok,
                "rdb_ctrx": rdb_ctrx, "calc_ctrx": c_ctrx, "ctrx_ok": ctrx_ok,
                "rdb_ptry": rdb_ptry, "calc_ptry": c_ptry, "ptry_ok": ptry_ok, "ptry_warning": ptry_warn,
                "rdb_ptrz": rdb_ptrz, "calc_ptrz": c_ptrz, "ptrz_ok": ptrz_ok, "ptrz_warning": ptrz_warn,
                "ctrw_x5": ctrw_x5, "e22": e22, "ctrw_x5_ok": ctrw_x5_ok,
                "ctrx_x5": ctrx_x5, "e23": e23, "ctrx_x5_ok": ctrx_x5_ok,
            }
            if ctrw_ok    is False: all_errors.append(f"CTRW mismatch — RDB={rdb_ctrw}, Calc ({relay_sheet})={c_ctrw}")
            if ctrx_ok    is False: all_errors.append(f"CTRX mismatch — RDB={rdb_ctrx}, Calc ({relay_sheet})={c_ctrx}")
            if ptry_ok    is False: all_errors.append(f"PTRY mismatch — RDB={rdb_ptry}, Calc={c_ptry}. {ptry_warn or ''}")
            if ptrz_ok    is False: all_errors.append(f"PTRZ mismatch — RDB={rdb_ptrz}, Calc ({relay_sheet})={c_ptrz}")
            if ctrw_x5_ok is False: all_errors.append(f"CTRW×5 mismatch — RDB CTRW×5={ctrw_x5}, Data Entry E22={e22}")
            if ctrx_x5_ok is False: all_errors.append(f"CTRX×5 mismatch — RDB CTRX×5={ctrx_x5}, Data Entry E23={e23}")
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
    if fr_result and calc_data:
        try:
            fr_rating   = fr_result["fr_rating"]
            calc_rating = calc_data.get("conductor_rating_from_fr")
            match = fr_rating is not None and calc_rating is not None and fr_rating == calc_rating
            conductor_rating_to_use = calc_rating if match else (calc_rating or fr_rating)
            results["conductor_check"] = {"pass": match, "fr_rating": fr_rating, "calc_rating": calc_rating}
            if not match:
                all_errors.append(f"Conductor rating mismatch — FR Workbook={fr_rating} A, Calc Sheet={calc_rating} A")
        except Exception as e:
            results["conductor_check"] = {"pass": False, "error": str(e)}
            all_errors.append(f"Conductor rating check failed: {e}")
    elif fr_result is None:
        results["conductor_check"] = {"pass": None, "message": "FR Workbook not uploaded"}
        if calc_data: conductor_rating_to_use = calc_data.get("conductor_rating_from_fr")
    else:
        results["conductor_check"] = {"pass": None, "message": "Calc Sheet not available"}

    if sel_content is not None:
        try:
            safe_load = parse_safe_load(sel_content)
            cr        = conductor_rating_to_use
            prc_raw   = calc_data.get("prc_criteria_raw") if calc_data else None
            prc_info  = classify_prc(prc_raw or "")
            primary   = prc_info.get("primary", "none")
            if safe_load is None:
                results["safe_load_check"] = {
                    "pass": False, "message": "Could not parse Safe Load value from SEL file",
                    "prc_label": prc_info["label"], "prc_info": prc_info,
                }
                all_errors.append("Safe Load value not found in SEL file.")
            elif cr is None:
                results["safe_load_check"] = {
                    "pass": None, "safe_load": safe_load,
                    "message": "Conductor rating unavailable for comparison",
                    "prc_label": prc_info["label"], "prc_info": prc_info,
                }
            else:
                passed    = safe_load >= cr
                load_line = f"Safe Load ({safe_load} A) {'≥' if passed else '<'} Conductor Rating ({cr} A)"
                results["safe_load_check"] = {
                    "pass": passed, "safe_load": safe_load, "conductor_rating": cr,
                    "prc_label": prc_info["label"], "prc_not_applicable": prc_info["not_applicable"],
                    "prc_info": prc_info, "load_line": load_line,
                }
                if not passed:
                    all_errors.append(f"Safe Load ({safe_load} A) < Conductor Rating ({cr} A)")
        except Exception as e:
            results["safe_load_check"] = {"pass": False, "error": str(e)}
            all_errors.append(f"Safe Load check failed: {e}")
    else:
        results["safe_load_check"] = {"pass": None, "message": "SEL file not uploaded"}

    return results, all_errors, (len(all_errors) == 0)

# ─── VERSION / UPDATE ENDPOINTS ──────────────────────────────────────────────
@app.route("/api/version")
def version():
    return jsonify({"version": APP_VERSION})

@app.route("/api/check_update")
def check_update():
    """
    Auto-update: compares running version against latest published on GitHub.
    Electron calls this on startup; responds with whether an update is available.
    """
    import urllib.request, json as _json
    GITHUB_LATEST = "https://api.github.com/repos/YOUR_GITHUB_USERNAME/phasorgrid/releases/latest"
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

# ─── EXISTING ENDPOINTS ───────────────────────────────────────────────────────
@app.route("/api/check", methods=["POST"])
def check():
    results_top, all_errors = {}, []
    calc_file = request.files.get("calc_sheet")
    rdb_file  = request.files.get("rdb_file")
    fr_file   = request.files.get("fr_workbook")
    sel_file  = request.files.get("sel_file")
    if not calc_file and not any([rdb_file, fr_file, sel_file]):
        return jsonify({"success": False, "errors": ["Please upload the Calc Sheet and at least one supporting file."], "results": {}, "_no_table": True})
    if not calc_file:
        return jsonify({"success": False, "errors": ["Please upload the Calc Sheet first."], "results": {}, "_no_table": True})
    if not any([rdb_file, fr_file, sel_file]):
        return jsonify({"success": False, "errors": ["Please upload at least one supporting file (RDB, FR Workbook, or SEL)."], "results": {}, "_no_table": True})
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
            text = extract_rdb_text(raw)
            rdb_sections = parse_rdb_sections(text)
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
    sel_content = None
    if sel_file:
        try: sel_content = sel_file.read().decode("utf-8", errors="ignore")
        except Exception as e: all_errors.append(f"Failed to read SEL file: {e}")
    check_results, check_errors, success = _run_checks(calc_data, rdb_sections, fr_result, sel_content)
    all_errors.extend(check_errors)
    return jsonify({"success": len(all_errors) == 0, "errors": all_errors,
                    "results": {**results_top, **check_results}, "_no_table": False})

@app.route("/api/check_multi", methods=["POST"])
def check_multi():
    calc_files = (request.files.getlist("calc_files[]") or request.files.getlist("calc_files"))
    rdb_files  = (request.files.getlist("rdb_files[]")  or request.files.getlist("rdb_files"))
    fr_files   = (request.files.getlist("fr_files[]")   or request.files.getlist("fr_files"))
    sel_files  = (request.files.getlist("sel_files[]")  or request.files.getlist("sel_files"))
    if not calc_files:
        return jsonify({"success": False, "summary": {"total_groups": 0, "total_rdb": 0, "passed": 0, "failed": 0},
                        "groups": [], "errors": ["At least one Calc Sheet is required."]}), 400

    _NOISE_TOKENS = {"calcs","calc","fr","fr5","fr4","fr3","fr2","fr1","svn","sel","settings",
        "dual","txt","xlsx","xlsm","rdb","the","and","or","of","for","to","la",
        "kv","230kv","115kv","138kv","69kv","345kv","500kv","p1","p2","p3","p4","p5","pf","slc"}
    def _strip_ext(name): return re.sub(r'\.[a-zA-Z0-9]{1,5}$', '', name)
    def _extract_line_numbers(name):
        raw = _strip_ext(name).lower(); found = set()
        for m in re.finditer(r'l[-\s]?(\d{2,})', raw): found.add(m.group(1))
        for m in re.finditer(r'\((\d{3,})\)', raw): found.add(m.group(1))
        for m in re.finditer(r'(?<![a-z\d])(\d{3,})(?![a-z\d])', raw): found.add(m.group(1))
        return found
    def _extract_station_words(name):
        n = re.sub(r'[^a-z0-9\s]', ' ', _strip_ext(name).lower())
        tokens = [t for t in n.split() if t not in _NOISE_TOKENS and len(t) >= 3]
        return [t for t in tokens if t.isalpha()][:3]
    def _group_key_from_file(name):
        line_nums = _extract_line_numbers(name); station = _extract_station_words(name)
        raw = _strip_ext(name).lower(); primary_ln = None
        m = re.search(r'l[-\s]?(\d{2,})', raw)
        if m: primary_ln = m.group(1)
        elif line_nums: primary_ln = sorted(line_nums)[0]
        parts = station[:2]
        if primary_ln: parts.append(primary_ln)
        return " ".join(parts) if parts else _strip_ext(name)[:30].lower()
    def _score_match(file_name, group_key):
        f_lines = _extract_line_numbers(file_name); f_station = set(_extract_station_words(file_name))
        g_parts = set(group_key.split()); score = 0.0
        g_nums  = {p for p in g_parts if p.isdigit()}
        if f_lines & g_nums: score += 1.5
        g_alpha = {p for p in g_parts if p.isalpha()}
        if f_station and g_alpha: score += len(f_station & g_alpha) / max(len(f_station | g_alpha), 1)
        return score
    def extract_port_from_name(name):
        m = re.search(r'\b(P[1-5F])\b', name, re.IGNORECASE)
        return m.group(1).upper() if m else None
    def _build_sel_by_port(sel_list):
        by_port = {}; default = None
        for sf in sel_list:
            try: content = sf.read().decode("utf-8", errors="ignore")
            except: continue
            port = extract_port_from_name(sf.filename)
            if port:
                if port not in by_port: by_port[port] = content
            else:
                if default is None: default = content
        if default is not None: by_port["_default"] = default
        return by_port
    def _pick_sel(port, sel_by_port):
        if port and port in sel_by_port: return sel_by_port[port]
        if "_default" in sel_by_port: return sel_by_port["_default"]
        return next(iter(sel_by_port.values()), None)

    groups = {}; order = []
    for f in calc_files:
        key = _group_key_from_file(f.filename)
        if key in groups: key = key + f"_{len(groups)}"
        groups[key] = {"calc": f, "rdb": [], "fr": [], "sel": []}; order.append(key)
    if not groups:
        return jsonify({"success": False, "summary": {}, "groups": [], "errors": ["No calc files."]}), 400
    def _assign(file_obj, slot):
        best_key, best_score = order[0], -1.0
        for k in order:
            s = _score_match(file_obj.filename, k)
            if s > best_score: best_score, best_key = s, k
        groups[best_key][slot].append(file_obj)
    for f in rdb_files: _assign(f, "rdb")
    for f in fr_files:  _assign(f, "fr")
    for f in sel_files:
        scores = {k: _score_match(f.filename, k) for k in order}
        if max(scores.values()) > 0.3: groups[max(scores, key=scores.get)]["sel"].append(f)
        else: [groups[k]["sel"].append(f) for k in order]

    all_groups_out = []; total_rdb_all = passed_all = failed_all = 0
    for gkey in order:
        g = groups[gkey]; calc_file = g["calc"]
        group_name = calc_file.filename if calc_file else gkey
        calc_data, calc_err = None, None
        try: calc_data = parse_calc_sheet(calc_file.read())
        except Exception as e: calc_err = str(e)
        fr_result = None
        for fr_f in g["fr"]:
            try: fr_result = parse_fr_workbook(fr_f.read()); break
            except: pass
        sel_by_port = _build_sel_by_port(g["sel"])
        group_results = []
        for rdb_f in g["rdb"]:
            fname = rdb_f.filename or "unknown.rdb"
            port  = extract_port_from_name(fname); ferrs = []
            if calc_err: ferrs.append(f"Calc Sheet parse error: {calc_err}")
            rdb_sections = {}
            try:
                raw = rdb_f.read(); text = extract_rdb_text(raw); rdb_sections = parse_rdb_sections(text)
            except Exception as e: ferrs.append(f"Failed to parse RDB '{fname}': {e}")
            sel_content = _pick_sel(port, sel_by_port) if sel_by_port else None
            check_res, check_errs, _ = _run_checks(calc_data, rdb_sections, fr_result, sel_content)
            ferrs.extend(check_errs)
            group_results.append({"file_name": fname, "port": port,
                                   "success": len(ferrs) == 0, "errors": ferrs, "results": check_res})
        g_total = len(group_results); g_passed = sum(1 for r in group_results if r["success"]); g_failed = g_total - g_passed
        total_rdb_all += g_total; passed_all += g_passed; failed_all += g_failed
        all_groups_out.append({
            "group_name": group_name, "group_key": gkey,
            "summary": {"total_rdb": g_total, "passed": g_passed, "failed": g_failed},
            "files": {"calc": calc_file.filename if calc_file else None,
                      "rdb": [f.filename for f in g["rdb"]], "fr": [f.filename for f in g["fr"]],
                      "sel": list(sel_by_port.keys())},
            "results": group_results,
        })
    return jsonify({
        "success": failed_all == 0,
        "summary": {"total_groups": len(all_groups_out), "total_rdb": total_rdb_all,
                    "passed": passed_all, "failed": failed_all},
        "groups": all_groups_out,
    })

@app.route("/health")
def health():
    return jsonify({"status": "ok", "version": APP_VERSION})

if __name__ == "__main__":
    app.run(debug=False, port=5051, host="127.0.0.1")
