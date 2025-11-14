# -*- coding: utf-8 -*-
"""
DCM4CHEE MWL — Enostavna lokalna aplikacija (SL)
✔ DICOM JSON (hex tagi) za MWL
✔ Samodejno ustvari pacienta, samodejni Patient ID
✔ Shranjevanje 'Načrtovani AE naprave' (datalist)
✔ Prikaz ČASA v rezultatih (robustno) in DATUMA kot DD.MM.YYYY
✔ BRISANJE MWL po poti /mwlitems/{StudyInstanceUID}/{SPS_ID}
✔ SAMODEJNI Accession Number (ACCYYYYMMDD-####), privzeto vklopljeno
"""

from flask import Flask, request, jsonify, Response, send_from_directory
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime
import os, json, re, io
import pdfplumber

# utišaj opozorila za samopodpisan certifikat (po potrebi)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ---------- Privzeta nastavitev ----------
CFG = {
    # privzeta (lahko spremeniš v poljubno od spodnjih dveh)
    "server_base": "https://192.168.123.220:30007/dcm4chee-arc",
    "aet": "WORKLIST",
    "username": "admin",
    "password": "ksenija",
    "allow_self_signed": True,
}

# Dve pripravljeni konfiguraciji (UI ju ponudi kot izbiro, vrednosti lahko ročno popraviš)
CONFIG_PRESETS = {
    "pacs1": {
        "label": "PACS 1 (192.168.123.220)",
        "server_base": "https://192.168.123.220:30007/dcm4chee-arc",
        "username": "admin",
        "password": "ksenija",
        "allow_self_signed": True,
    },
    "pacs2": {
        "label": "PACS 2 (192.168.1.40)",
        "server_base": "https://192.168.1.40:30007/dcm4chee-arc",
        "username": "admin",
        "password": "tobi78nLJ5",
        "allow_self_signed": True,
    },
}

COUNTER_FILE = "pid_counter.json"
ACC_COUNTER_FILE = "acc_counter.json"   # <— števec za Accession
STATION_FILE = "station_aets.json"

# ---------- Datoteke ----------
def _read_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json_file(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# ---------- Station AE ----------
def load_station_aets():
    data = _read_json_file(STATION_FILE, {"items": []})
    items = data.get("items", [])
    seen = set(); clean = []
    for it in items:
        s = (it or "").strip()
        if s and s not in seen:
            seen.add(s); clean.append(s)
    return clean

def save_station_aets(items):
    uniq = sorted({(it or "").strip() for it in items if (it or "").strip()})
    _write_json_file(STATION_FILE, {"items": uniq})
    return uniq

def add_station_aet(value):
    v = (value or "").strip()
    if not v: return load_station_aets()
    curr = load_station_aets()
    if v not in curr:
        curr.append(v); return save_station_aets(curr)
    return curr

# ---------- Patient ID ----------
def _load_counter():
    return _read_json_file(COUNTER_FILE, {"date": datetime.now().strftime("%Y%m%d"), "n": 0})

def _save_counter(obj):
    _write_json_file(COUNTER_FILE, obj)

def next_patient_id():
    state = _load_counter()
    today = datetime.now().strftime("%Y%m%d")
    if state.get("date") != today:
        state = {"date": today, "n": 0}
    state["n"] += 1
    _save_counter(state)
    return f"PID{today}-{state['n']:04d}"

def generate_unique_patient_id():
    for _ in range(1000):
        pid = next_patient_id()
        r = qido_find_patient_by_id(pid)
        if r.ok:
            try:
                arr = r.json()
                if not (isinstance(arr, list) and len(arr) > 0):
                    return pid
            except Exception:
                return pid
        else:
            return pid
    return f"PID{datetime.now().strftime('%Y%m%d')}-{datetime.now().strftime('%H%M%S')}"

# ---------- Accession Number ----------
def _load_acc_counter():
    return _read_json_file(ACC_COUNTER_FILE, {"date": datetime.now().strftime("%Y%m%d"), "n": 0})

def _save_acc_counter(obj):
    _write_json_file(ACC_COUNTER_FILE, obj)

def next_accession_number():
    """ACCYYYYMMDD-####, reset števca vsak dan."""
    state = _load_acc_counter()
    today = datetime.now().strftime("%Y%m%d")
    if state.get("date") != today:
        state = {"date": today, "n": 0}
    state["n"] += 1
    _save_acc_counter(state)
    return f"ACC{today}-{state['n']:04d}"

# ---------- Pretvorbe datum/čas ----------
DA_RE_1 = re.compile(r"^\s*(\d{2})\.(\d{2})\.(\d{4})\s*$")   # DD.MM.YYYY
DA_RE_2 = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")     # YYYY-MM-DD
DA_RE_3 = re.compile(r"^\s*\d{8}\s*$")                       # YYYYMMDD

TM_RE_1 = re.compile(r"^\s*(\d{2}):(\d{2})(?::(\d{2}))?\s*$") # HH:MM[:SS]
TM_RE_2 = re.compile(r"^\s*\d{6}\s*$")                        # HHMMSS
TM_RE_3 = re.compile(r"^\s*\d{4}\s*$")                        # HHMM -> HHMMSS

def to_da(s: str | None) -> str:
    if not s: return ""
    s = s.strip()
    m = DA_RE_1.match(s)
    if m:
        dd, mm, yyyy = m.groups()
        return f"{yyyy}{mm}{dd}"
    m = DA_RE_2.match(s)
    if m:
        yyyy, mm, dd = m.groups()
        return f"{yyyy}{mm}{dd}"
    if DA_RE_3.match(s):
        return s
    try:
        dt = datetime.strptime(s, "%d.%m.%Y")
        return dt.strftime("%Y%m%d")
    except Exception:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
            return dt.strftime("%Y%m%d")
        except Exception:
            return ""

def to_tm(s: str | None) -> str:
    if not s: return ""
    s = s.strip()
    m = TM_RE_1.match(s)
    if m:
        hh, mm, ss = m.groups()
        ss = ss or "00"
        return f"{hh}{mm}{ss}"
    if TM_RE_2.match(s):
        return s
    if TM_RE_3.match(s):
        return s + "00"
    try:
        dt = datetime.strptime(s, "%H:%M:%S")
        return dt.strftime("%H%M%S")
    except Exception:
        try:
            dt = datetime.strptime(s, "%H:%M")
            return dt.strftime("%H%M00")
        except Exception:
            return ""

# ---------- HTTP helperji ----------
def _verify_flag():
    return not CFG.get("allow_self_signed", True)

def arc_get(path: str, headers: dict | None = None):
    url = f"{CFG['server_base'].rstrip('/')}{path}"
    return requests.get(url, headers=headers or {},
                        auth=HTTPBasicAuth(CFG["username"], CFG["password"]),
                        verify=_verify_flag())

def arc_post_dicom(path: str, dicom_json: dict):
    url = f"{CFG['server_base'].rstrip('/')}{path}"
    return requests.post(url, json=dicom_json,
                         headers={"Content-Type":"application/dicom+json","Accept":"application/json"},
                         auth=HTTPBasicAuth(CFG["username"], CFG["password"]),
                         verify=_verify_flag())

def arc_delete(path: str, headers: dict | None = None):
    url = f"{CFG['server_base'].rstrip('/')}{path}"
    return requests.delete(url, headers=headers or {"Accept":"application/json"},
                           auth=HTTPBasicAuth(CFG["username"], CFG["password"]),
                           verify=_verify_flag())

# ---------- Pacient ----------
def qido_find_patient_by_id(patient_id: str):
    path = f"/aets/{CFG['aet']}/rs/patients?PatientID={requests.utils.quote(patient_id)}"
    url = f"{CFG['server_base'].rstrip('/')}{path}"
    return requests.get(url, headers={"Accept": "application/json"},
                        auth=HTTPBasicAuth(CFG["username"], CFG["password"]),
                        verify=_verify_flag())

def create_patient_dicom_json(patient_id: str, patient_name: str, birth_date_da: str | None):
    ds = {
        "00100020": {"vr":"LO","Value":[patient_id]},
        "00100010": {"vr":"PN","Value":[{"Alphabetic":patient_name or "NEZNANO"}]}
    }
    b = to_da(birth_date_da or "")
    if b:
        ds["00100030"] = {"vr":"DA","Value":[b]}
    return ds

def rs_create_patient(patient_id: str, patient_name: str, birth_date_da: str | None):
    path = f"/aets/{CFG['aet']}/rs/patients"
    return arc_post_dicom(path, create_patient_dicom_json(patient_id, patient_name, birth_date_da))

def ensure_patient_exists(patient_id: str, patient_name: str, birth_date_da: str | None):
    r = qido_find_patient_by_id(patient_id)
    if r.ok:
        try:
            arr = r.json()
            if isinstance(arr, list) and len(arr) > 0:
                return True
        except Exception:
            pass
    r2 = rs_create_patient(patient_id, patient_name, birth_date_da)
    return r2.ok

# ---------- DICOM JSON -> preprosto ----------
def _get_str(ds, tag, default=""):
    try:
        v = ds.get(tag, {}); vals = v.get("Value")
        if not vals: return default
        if v.get("vr") == "PN":
            val = vals[0]
            if isinstance(val, dict):
                return val.get("Alphabetic") or val.get("Ideographic") or val.get("Phonetic") or default
            return str(val)
        return str(vals[0])
    except Exception:
        return default

def dicom_mwl_to_simple(ds):
    simple = {
        "patientName": _get_str(ds, "00100010"),
        "patientId":   _get_str(ds, "00100020"),
        "accessionNumber": _get_str(ds, "00080050"),
        "procedureDescription": _get_str(ds, "00321060"),
        "studyInstanceUID": _get_str(ds, "0020000D"),
        "scheduledProcedureStep": []
    }
    sps_seq = ds.get("00400100", {})
    items = sps_seq.get("Value") or []
    out_items = []
    for item in items:
        out_items.append({
            "modality": _get_str(item, "00080060"),
            "scheduledStationAETitle": _get_str(item, "00400001"),
            "scheduledProcedureStepStartDate": _get_str(item, "00400002"),
            "scheduledProcedureStepStartTime": _get_str(item, "00400003"),
            "scheduledProcedureStepID": _get_str(item, "00400009"),
            "scheduledProcedureStepStatus": _get_str(item, "00400020"),
        })
    if out_items:
        simple["scheduledProcedureStep"] = out_items
    return simple

# ---------- Zgradi DICOM MWL ----------
def build_dicom_mwl(form: dict, resolved_patient_id: str) -> dict:
    pn    = (form.get("patientName") or "NEZNANO").strip()
    pid   = resolved_patient_id.strip()
    acc   = (form.get("accession") or "").strip()
    proc  = (form.get("procDesc") or "").strip()
    mod   = (form.get("modality") or "US").strip().upper()
    sdate = to_da(form.get("schedDate") or form.get("schedDate_da") or "")
    stime = to_tm(form.get("schedTime") or form.get("schedTime_tm") or "")
    saet  = (form.get("stationAET") or "").strip()
    spsid = f"SPS_{pid}"

    dicom = {
        "00100010": {"vr": "PN", "Value": [{"Alphabetic": pn}]},
        "00100020": {"vr": "LO", "Value": [pid]},
    }
    if acc:  dicom["00080050"] = {"vr": "SH", "Value": [acc]}
    if proc: dicom["00321060"] = {"vr": "LO", "Value": [proc]}

    sps_item = {
        "00080060": {"vr": "CS", "Value": [mod]},
        "00400009": {"vr": "SH", "Value": [spsid]},
        "00400020": {"vr": "CS", "Value": ["SCHEDULED"]},
    }
    if saet:  sps_item["00400001"] = {"vr": "AE", "Value": [saet]}
    if sdate: sps_item["00400002"] = {"vr": "DA", "Value": [sdate]}
    if stime: sps_item["00400003"] = {"vr": "TM", "Value": [stime]}
    dicom["00400100"] = {"vr": "SQ", "Value": [sps_item]}
    return dicom

# ---------- BRISANJE ----------
def delete_mwl_by_uid_and_sps(study_uid: str, sps_id: str):
    p = f"/aets/{CFG['aet']}/rs/mwlitems/{requests.utils.quote(study_uid)}/{requests.utils.quote(sps_id)}"
    r = arc_delete(p, {"Accept":"application/json"})
    return r

@app.post('/api/remove')
def api_remove():
    """
    Body JSON:
      {"spsid":"SPS_...", "studyuid":"2.25...."}  # idealno
    Če "studyuid" manjka, aplikacija sama prebere /mwlitems in poišče StudyInstanceUID.
    """
    data = request.get_json(silent=True) or {}
    spsid = (data.get("spsid") or "").strip()
    studyuid = (data.get("studyuid") or "").strip()

    if not spsid:
        return jsonify({"ok": False, "napaka": "Manjka 'spsid'."}), 400

    # če nimamo studyuid, ga poiščemo
    if not studyuid:
        r = arc_get(f"/aets/{CFG['aet']}/rs/mwlitems", {"Accept":"application/dicom+json"})
        if not r.ok:
            return Response(r.text, status=r.status_code)
        try:
            arr = r.json()
        except Exception:
            return jsonify({"ok": False, "napaka": "Nepričakovan odgovor PACS.", "odgovor": r.text}), 502

        if isinstance(arr, list):
            for ds in arr:
                sps_seq = (ds or {}).get("00400100", {})
                items = sps_seq.get("Value") or []
                cur_sps = None
                if items:
                    v = items[0]
                    val = (v.get("00400009", {}) or {}).get("Value") or []
                    cur_sps = str(val[0]) if val else ""
                if cur_sps and cur_sps == spsid:
                    val = (ds.get("0020000D", {}) or {}).get("Value") or []
                    studyuid = str(val[0]) if val else ""
                    if studyuid:
                        break

        if not studyuid:
            return jsonify({"ok": False, "napaka": "Ni bilo mogoče najti StudyInstanceUID za podani SPS ID."}), 404

    resp = delete_mwl_by_uid_and_sps(studyuid, spsid)
    try:
        body = resp.json()
    except Exception:
        body = resp.text

    return jsonify({"ok": resp.ok, "status": resp.status_code, "response": body}), (200 if resp.ok else resp.status_code)


# ---------- BRISANJE VSEH MWL ELEMENTOV ----------
@app.post('/api/remove_all')
def api_remove_all():
    """
    Izbriše vse MWL elemente na trenutnem AET.
    """
    # Najprej pridobimo vse MWL elemente
    r = arc_get(f"/aets/{CFG['aet']}/rs/mwlitems", {"Accept": "application/dicom+json"})
    if not r.ok:
        return Response(r.text, status=r.status_code)

    try:
        arr = r.json()
    except Exception:
        return jsonify({"ok": False, "napaka": "Nepričakovan odgovor PACS.", "odgovor": r.text}), 502

    if not isinstance(arr, list):
        return jsonify({"ok": True, "deleted": [], "errors": []})

    deleted = []
    errors = []

    for ds in arr:
        if not isinstance(ds, dict):
            continue
        # StudyInstanceUID
        try:
            val_uid = (ds.get("0020000D", {}) or {}).get("Value") or []
            studyuid = str(val_uid[0]) if val_uid else ""
        except Exception:
            studyuid = ""

        # ScheduledProcedureStepSequence (00400100)
        sps_seq = (ds.get("00400100", {}) or {})
        items = sps_seq.get("Value") or []
        for item in items:
            try:
                val_sps = (item.get("00400009", {}) or {}).get("Value") or []
                spsid = str(val_sps[0]) if val_sps else ""
            except Exception:
                spsid = ""

            if not studyuid or not spsid:
                continue

            resp = delete_mwl_by_uid_and_sps(studyuid, spsid)
            if resp.ok:
                deleted.append({"studyuid": studyuid, "spsid": spsid})
            else:
                errors.append({
                    "studyuid": studyuid,
                    "spsid": spsid,
                    "status": resp.status_code,
                    "body": resp.text
                })

    return jsonify({
        "ok": len(errors) == 0,
        "deleted": deleted,
        "errors": errors
    }), (200 if len(errors) == 0 else 207)

# ---------- API: UI, config, list, create ----------
@app.route('/')
def index():
    return Response(INDEX_HTML, mimetype='text/html')

@app.post('/api/config')
def set_config():
    data = request.json or {}
    for k in CFG.keys():
        if k in data:
            CFG[k] = data[k]
    return jsonify({"ok": True, "cfg": CFG})

@app.get('/api/stations')
def get_stations():
    return jsonify({"items": load_station_aets()})

@app.post('/api/stations')
def add_station():
    data = request.json or {}
    val = (data.get("value") or "").strip()
    items = add_station_aet(val)
    return jsonify({"items": items})

@app.get('/api/list')
def list_mwl():
    r = arc_get(f"/aets/{CFG['aet']}/rs/mwlitems", {"Accept":"application/dicom+json"})
    if not r.ok:
        return Response(r.text, status=r.status_code)
    try:
        arr = r.json()
        if not isinstance(arr, list):
            return jsonify([])
        simple = [dicom_mwl_to_simple(ds) for ds in arr]
        return jsonify(simple)
    except Exception:
        return Response(r.text, status=200, mimetype="application/json")

@app.post('/api/create')
def create_mwl():
    simple = request.json or {}

    surname  = (simple.get("patientSurname") or "").strip()
    given    = (simple.get("patientGiven") or "").strip()
    raw_pn   = (simple.get("patientName") or "").strip()
    if not raw_pn:
        raw_pn = (surname or given) and f"{surname}^{given}" or "NEZNANO"

    auto_pid   = bool(simple.get("autoPID"))
    req_pid    = (simple.get("patientId") or "").strip()

    # --- Accession: avtomatsko, če autoACC=True ali polje prazno ---
    auto_acc   = bool(simple.get("autoACC"))
    req_acc    = (simple.get("accession") or "").strip()
    accession  = next_accession_number() if (auto_acc or not req_acc) else req_acc

    birth_da = simple.get("birthDate_da") or to_da(simple.get("birthDate") or simple.get("birthDate_h") or "")
    sched_da = simple.get("schedDate_da") or to_da(simple.get("schedDate") or simple.get("schedDate_h") or "")
    sched_tm = simple.get("schedTime_tm") or to_tm(simple.get("schedTime") or simple.get("schedTime_h") or "")

    pid = generate_unique_patient_id() if (auto_pid or not req_pid) else req_pid

    station_aet = (simple.get("stationAET") or "").strip()
    if station_aet:
        add_station_aet(station_aet)

    if not ensure_patient_exists(pid, raw_pn, birth_da):
        return jsonify({"ok": False, "napaka": "Pacienta ni bilo mogoče ustvariti", "dodeljenID": pid}), 400

    payload = {
        **simple,
        "patientName": raw_pn,
        "schedDate": sched_da,
        "schedTime": sched_tm,
        "accession": accession,     # <-- uporabimo izračunani accession
    }
    r = arc_post_dicom(f"/aets/{CFG['aet']}/rs/mwlitems", build_dicom_mwl(payload, pid))

    try:
        arch_json = r.json()
    except Exception:
        arch_json = r.text

    return jsonify({
        "ok": r.ok,
        "status": r.status_code,
        "dodeljenID": pid,
        "dodeljenAccession": accession,    # <-- vrnemo v UI
        "odgovorPACS": arch_json
    }), r.status_code


# ---------- PDF Import endpoint ----------
@app.post('/api/import_pdf')
def import_pdf():
    file = request.files.get("file")
    if not file:
        return jsonify({"ok": False, "error": "No file"}), 400
    try:
        pdf = pdfplumber.open(io.BytesIO(file.read()))
        lines = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            for ln in text.split("\n"):
                ln = ln.strip()
                if ln:
                    lines.append(ln)

        # Heuristika: vrstica struktura
        # Št. Termin(DD.MM.YYYY HH:MM) Priimek Ime Telefon Dat. rojstva Opomba...
        out_rows = []
        date_re = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")
        time_re = re.compile(r"^\d{1,2}:\d{2}$")

        for ln in lines:
            parts = ln.split()
            if len(parts) < 7:
                continue
            # preskoči header in neustrezne vrstice
            if parts[0].startswith("Št"):
                continue
            if not date_re.match(parts[1]) or not time_re.match(parts[2]):
                continue

            idx = parts[0]
            exam_date = parts[1]
            exam_time = parts[2]

            # poišči datum rojstva kasneje v vrstici
            birth_idx = None
            for i in range(3, len(parts)):
                if date_re.match(parts[i]):
                    birth_idx = i
                    break
            if birth_idx is None:
                continue

            surname = parts[3]
            given = parts[4] if birth_idx > 4 else ""
            birth = parts[birth_idx]
            desc_tokens = parts[birth_idx+1:]
            desc = " ".join(desc_tokens) if desc_tokens else ""

            row = f"{idx};{surname};{given};{birth};{exam_date};{exam_time};{desc}"
            out_rows.append(row)

        txt = "\n".join(out_rows)
        return jsonify({"ok": True, "text": txt})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get('/logo.png')
def logo_png():
    """Serve logo.png from the same directory as this script."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(base_dir, 'logo.png')

# ---------- HTML (SL) ----------
INDEX_HTML = """
<!doctype html><html lang="sl"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>DCM4CHEE MWL — Lokalni odjemalec</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;margin:0;background:#0b1020;color:#e8edf2}
header{padding:16px 20px;border-bottom:1px solid #223056}
h1{margin:0;font-size:18px}
main{max-width:1100px;margin:0 auto;padding:16px}
.card{background:#131a33;border:1px solid #223056;border-radius:12px;padding:16px;margin:12px 0}
label{display:block;margin:8px 0 4px;color:#97a1b3}
input,button,select,datalist{border-radius:10px;border:1px solid #223056;background:#0e1630;color:#e8edf2;padding:10px;width:100%}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.btn{cursor:pointer;background:#5aa3ff;border:0;color:#041227;font-weight:600}
.btn.alt{background:#2b3561;color:#e8edf2}
.btn.danger{background:#ff5a7a;color:#041227}
table{width:100%;border-collapse:collapse;margin-top:12px}
th,td{border-bottom:1px solid #223056;padding:8px;text-align:left}th{color:#97a1b3}
.muted{color:#97a1b3}.ok{color:#35c56a}.err{color:#ff5a7a}
small{color:#97a1b3}
.inline{display:flex;gap:8px;align-items:center}
.flex{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.badge{display:inline-block;padding:2px 8px;border:1px solid #223056;border-radius:999px;background:#0e1630;color:#97a1b3;font-size:12px}
.hint{font-size:12px;color:#97a1b3}
</style></head><body>
<header style="display:flex;justify-content:space-between;align-items:center;">
  <h1>WORKLIST - Lokalni odjemalec</h1>
  <img src="/logo.png" alt="Logo" style="height:90px;"/>
</header><main>

<section class="card">
<h3>Nastavitve strežnika</h3>

<label>Hitre nastavitve</label>
<select id="cfgPreset" onchange="applyPreset()">
  <option value="">— ročna nastavitev —</option>
  <option value="pacs1">PACS 1 (192.168.1.40)</option>
  <option value="pacs2">PACS 2 (192.168.123.220)</option>
</select>

<div class="row" style="margin-top:10px">
 <div><label>URL arhiva</label><input id="server_base" value="https://192.168.123.220:30007/dcm4chee-arc"/></div>
 <div><label>AET delovne liste</label><input id="aet" value="WORKLIST"/></div>
</div>
<div class="row">
 <div><label>Uporabniško ime</label><input id="username" value="admin"/></div>
 <div><label>Geslo</label><input id="password" type="password" value="admin"/></div>
</div>
<div class="row">
 <div><label>Dovoli samopodpisan certifikat</label>
 <select id="allow_self_signed"><option value="true" selected>Da</option><option value="false">Ne</option></select></div><div></div>
</div>
<div class="flex" style="margin-top:10px">
 <button class="btn" onclick="saveCfg()">Shrani</button>
 <button class="btn alt" onclick="listItems()">Prikaži MWL elemente</button>
 <span class="badge">Stanje: <span id="statusText">Pripravljeno</span></span>
</div></section>

<section class="card">
<h3>Uvoz dnevnega programa</h3>
<p class="hint">Uvozi CSV ali PDF z dnevnim programom</p>
<div class="flex" style="margin-top:10px">
  <button class="btn" onclick="openImportDialog()">Uvozi CSV / PDF</button>
  <button class="btn alt" onclick="writeImportedRows()">Vpiši</button>
</div>
<div id="importInfo" class="hint" style="margin-top:8px">Ni uvoženih podatkov.</div>
<div id="importTable" style="margin-top:10px;overflow:auto"></div>
<input type="file" id="importFile" accept=".csv,text/csv,application/pdf" style="display:none" onchange="onImportFileChange(event)"/>
</section>

<section class="card">
<h3>Ustvari nov MWL element</h3>
<div class="row">
 <div>
  <label>Priimek</label><input id="surname" value="NOVAK"/>
  <label>Ime</label><input id="given" value="MIHA"/>

  <label class="inline">
    <span>ID pacienta</span>
    <span class="inline" style="gap:6px;margin-left:8px">
      <input type="checkbox" id="autoPID" checked/>
      <small>Samodejno generiraj</small>
    </span>
  </label>
  <input id="patientId" placeholder="(pusti prazno za samodejno generiranje)"/>

  <label>Datum rojstva (DD.MM.YYYY)</label><input id="birthDate_h" placeholder="31.01.1980"/>
  <div class="hint">Vnesi npr. 05.11.1979</div>

  <label>Številka napotnice / Accession</label>
  <div class="inline" style="gap:8px">
    <input id="accession" placeholder="(samodejno, če je spodaj označeno)" style="flex:1"/>
    <label class="inline" style="gap:6px">
      <input type="checkbox" id="autoACC" checked/>
      <small>Samodejno generiraj</small>
    </label>
  </div>
 </div>
 <div>
  <label>Modaliteta (npr. US, CT, MR)</label><input id="modality" value="US"/>

  <label>Datum preiskave (DD.MM.YYYY)</label><input id="schedDate_h" value="30.10.2025"/>
  <label>Čas preiskave (HH:MM ali HH:MM:SS)</label><input id="schedTime_h" value="09:00"/>

  <label>Načrtovani AE naprave</label>
  <div class="flex">
    <input id="stationAET" list="stationList" placeholder="npr. US_ROOM1"/>
    <datalist id="stationList"></datalist>
    <button class="btn" style="white-space:nowrap" onclick="saveCurrentAE()">Dodaj v seznam</button>
  </div>
 </div>
</div>
<label>Opis postopka / preiskave</label><input id="procDesc" value="Doppler karotid"/>
<div class="flex" style="margin-top:10px">
 <button class="btn" onclick="createItem()">Ustvari MWL</button>
</div></section>

<section class="card"><h3>Rezultati</h3>
  <div id="out" class="muted">Pripravljeno.</div>
</section>
</main>

<script>
// ---- helpers ----
var CFG_PRESETS = {
  pacs1: {
    server_base: "https://192.168.1.40:30007/dcm4chee-arc",
    username: "admin",
    password: "tobi78nLJ5",
    allow_self_signed: true
  },
  pacs2: {
    server_base: "https://192.168.123.220:30007/dcm4chee-arc",
    username: "admin",
    password: "ksenija",
    allow_self_signed: true
  }
};

function applyPreset(){
  var sel = document.getElementById('cfgPreset');
  if(!sel) return;
  var key = sel.value;
  if(!key) return;
  var p = CFG_PRESETS[key];
  if(!p) return;
  var sb = document.getElementById('server_base');
  var un = document.getElementById('username');
  var pw = document.getElementById('password');
  var as = document.getElementById('allow_self_signed');
  var aet = document.getElementById('aet');
  if(sb) sb.value = p.server_base;
  if(un) un.value = p.username;
  if(pw) pw.value = p.password;
  if(as) as.value = p.allow_self_signed ? 'true' : 'false';
  if(aet) aet.value = 'WORKLIST';
}

function $(id){ return document.getElementById(id); }

function esc(s){
  return String(s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;');
}

function log(msg, cls){
  var out = $('out');
  if(out){
    out.innerHTML = '<div class="'+(cls||'')+'">'+msg+'</div>';
  }
  var st = $('statusText');
  if(st){
    st.textContent = (cls === 'ok') ? 'OK' : 'Pripravljeno';
  }
}

// Datum (YYYYMMDD -> DD.MM.YYYY)
function daToHuman(da){
  var s = String(da||'').trim();
  if (/^\d{8}$/.test(s)){
    return s.slice(6,8)+'.'+s.slice(4,6)+'.'+s.slice(0,4);
  }
  return s || '';
}

// Čas (sprejme HHMM, HHMMSS, HH:MM(:SS), z/ex frakcijami)
function fmtTime(tm){
  if (tm == null) return '';
  var s0 = String(tm).trim();
  if (/^\d{2}:\d{2}(:\d{2})?$/.test(s0)) return s0; // že lepo
  var digits = s0.replace(/\D/g,'');              // npr. "173000", "1730"
  if (digits.length < 4) return s0;               // nimamo vsaj HHMM
  var hh = digits.slice(0,2);
  var mm = digits.slice(2,4);
  var ss = digits.slice(4,6) || '00';
  var H = +hh, M = +mm, S = +ss;
  if (!(H>=0 && H<=23 && M>=0 && M<=59 && S>=0 && S<=59)) return s0;
  return (ss==='00') ? (hh+':'+mm) : (hh+':'+mm+':'+ss);
}

// ---- Uvoz dnevnega programa (CSV) ----
var importedRows = [];

function normalizeText(s){
  if(s == null) return '';
  var map = {
    'Č':'C','Ć':'C','Ž':'Z','Š':'S','Đ':'D',
    'č':'c','ć':'c','ž':'z','š':'s','đ':'d'
  };
  var out = '';
  var str = String(s);
  for (var i=0; i<str.length; i++){
    var ch = str.charAt(i);
    out += map[ch] || ch;
  }
  return out.toUpperCase();
}

function normalizeDateHuman(s){
  s = String(s || '').trim();
  if(!s) return '';
  var m;

  // že v formatu DD.MM.YYYY
  m = s.match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})$/);
  if(m){
    var dd = m[1], mm = m[2], yyyy = m[3];
    if(dd.length === 1) dd = '0' + dd;
    if(mm.length === 1) mm = '0' + mm;
    return dd + '.' + mm + '.' + yyyy;
  }

  // YYYY-MM-DD -> DD.MM.YYYY
  m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if(m){
    var yyyy2 = m[1], mm2 = m[2], dd2 = m[3];
    if(dd2.length === 1) dd2 = '0' + dd2;
    if(mm2.length === 1) mm2 = '0' + mm2;
    return dd2 + '.' + mm2 + '.' + yyyy2;
  }

  // splošen zapis D/M/YYYY ali D-M-YYYY ali D.M.YYYY
  m = s.match(/^(\d{1,2})[\.\/-](\d{1,2})[\.\/-](\d{4})$/);
  if(m){
    var dd3 = m[1], mm3 = m[2], yyyy3 = m[3];
    if(dd3.length === 1) dd3 = '0' + dd3;
    if(mm3.length === 1) mm3 = '0' + mm3;
    return dd3 + '.' + mm3 + '.' + yyyy3;
  }

  // če ne prepoznamo, pustimo original
  return s;
}

function normalizeTimeHuman(s){
  s = String(s || '').trim();
  if(!s) return '';
  var m;

  // že v formatu HH:MM
  if(/^\d{2}:\d{2}$/.test(s)){
    return s;
  }

  // HH:MM(:SS) -> HH:MM
  m = s.match(/^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$/);
  if(m){
    var hh = m[1], mm = m[2];
    if(hh.length === 1) hh = '0' + hh;
    if(mm.length === 1) mm = '0' + mm;
    return hh + ':' + mm;
  }

  // števke brez ločil, npr. "900", "0900", "1230", "13.00"
  var digits = s.replace(/\D/g,'');
  if(digits.length === 3 || digits.length === 4){
    var h, min;
    if(digits.length === 3){
      h = digits.charAt(0);
      min = digits.slice(1);
    }else{
      h = digits.slice(0,2);
      min = digits.slice(2);
    }
    if(h.length === 1) h = '0' + h;
    if(min.length === 1) min = '0' + min;
    var H = parseInt(h,10), M = parseInt(min,10);
    if(H>=0 && H<=23 && M>=0 && M<=59){
      return h + ':' + min;
    }
  }

  // če ne prepoznamo, pustimo original
  return s;
}

function parseImportedCsv(text){
  var lines = text.split(/\\r?\\n/);
  var rows = [];
  for(var i=0; i<lines.length; i++){
    var line = lines[i];
    var trimmed = line.trim();
    if(!trimmed) continue;
    var parts = trimmed.split(';');
    if(parts.length < 7) continue;
    // preskoči header, če je
    if((parts[0] || '').trim().indexOf('#') === 0 &&
       (parts[1] || '').toUpperCase().indexOf('PRIIMEK') !== -1) continue;
    var idx       = (parts[0] || '').trim();
    var surname   = normalizeText(parts[1] || '');
    var given     = normalizeText(parts[2] || '');
    var birthDate = normalizeDateHuman((parts[3] || '').trim());
    var examDate  = normalizeDateHuman((parts[4] || '').trim());
    var examTime  = normalizeTimeHuman((parts[5] || '').trim());
    var desc      = normalizeText(parts[6] || '');
    rows.push({
      idx: idx,
      surname: surname,
      given: given,
      birthDate: birthDate,
      examDate: examDate,
      examTime: examTime,
      desc: desc,
      station: ''
    });
  }
  return rows;
}

function renderImportTable(){
  var info = $('importInfo');
  var container = $('importTable');
  if(!container || !info) return;
  if(!importedRows.length){
    info.textContent = 'Ni uvoženih podatkov.';
    container.innerHTML = '';
    return;
  }
  info.textContent = 'Uvoženih vrstic: ' + importedRows.length + '. Izberi UZ1 ali UZ2 za vrstice, ki jih želiš vpisati.';
  var html = '<table><tr>'
    + '<th>#</th><th>Priimek</th><th>Ime</th><th>Datum rojstva</th>'
    + '<th>Datum preiskave</th><th>Čas preiskave</th><th>Opis preiskave</th><th>AE</th>'
    + '</tr>';
  for(var i=0; i<importedRows.length; i++){
    var r = importedRows[i];
    var ae = r.station || '';
    html += '<tr>'
      + '<td>'+esc(r.idx || (i+1))+'</td>'
      + '<td>'+esc(r.surname)+'</td>'
      + '<td>'+esc(r.given)+'</td>'
      + '<td>'+esc(r.birthDate)+'</td>'
      + '<td>'+esc(r.examDate)+'</td>'
      + '<td>'+esc(r.examTime)+'</td>'
      + '<td>'+esc(r.desc)+'</td>'
      + '<td><select class="import-station" data-idx="'+i+'">'
      + '<option value=""></option>'
      + '<option value="UZ1"'+(ae==='UZ1'?' selected':'')+'>UZ1</option>'
      + '<option value="UZ2"'+(ae==='UZ2'?' selected':'')+'>UZ2</option>'
      + '</select></td>'
      + '</tr>';
  }
  html += '</table>';
  container.innerHTML = html;
  var selects = container.getElementsByClassName('import-station');
  for(var j=0; j<selects.length; j++){
    selects[j].addEventListener('change', function(e){
      var idxStr = e.target.getAttribute('data-idx');
      var idx = parseInt(idxStr, 10);
      if(!isNaN(idx) && importedRows[idx]){
        importedRows[idx].station = e.target.value || '';
      }
    });
  }
}

function openImportDialog(){
  var inp = $('importFile');
  if(!inp){ log('Manjka input za uvoz.', 'err'); return; }
  inp.value = '';
  inp.click();
}

function onImportFileChange(evt){
  var files = (evt && evt.target && evt.target.files) ? evt.target.files : null;
  var file = files && files[0];
  if(!file) return;
  var name = (file.name||'').toLowerCase();

  // PDF -> pošlji na backend in prejmi umetni CSV tekst
  if(name.indexOf('.pdf') >= 0){
    var fd = new FormData();
    fd.append("file", file);
    fetch("/api/import_pdf", {
      method: "POST",
      body: fd
    })
      .then(function(r){ return r.json(); })
      .then(function(j){
        if(!j.ok){
          log("Napaka pri PDF uvozu: " + esc(j.error||'neznano'), "err");
          return;
        }
        importedRows = parseImportedCsv(j.text || '');
        renderImportTable();
        log("PDF uspešno uvožen.", "ok");
      })
      .catch(function(e){
        importedRows = [];
        renderImportTable();
        log("Napaka PDF: " + esc(String(e)), "err");
      });
    return;
  }

  // CSV pot
  var reader = new FileReader();
  reader.onload = function(ev){
    try{
      var text = String(ev.target.result || '');
      importedRows = parseImportedCsv(text);
      renderImportTable();
      log('CSV uspešno uvožen.', 'ok');
    }catch(e){
      importedRows = [];
      renderImportTable();
      log('Napaka pri branju CSV: ' + esc(String(e)), 'err');
    }
  };
  reader.onerror = function(){
    importedRows = [];
    renderImportTable();
    log('Napaka pri branju datoteke.', 'err');
  };
  reader.readAsText(file);
}

function writeImportedRows(){
  if(!importedRows.length){
    log('Ni uvoženih vrstic za vpis.', 'err');
    return;
  }
  var rowsToWrite = [];
  for(var i=0; i<importedRows.length; i++){
    var r = importedRows[i];
    if(r.station === 'UZ1' || r.station === 'UZ2'){
      rowsToWrite.push(r);
    }
  }
  if(!rowsToWrite.length){
    log('Za nobeno vrstico ni izbran AE (UZ1/UZ2).', 'err');
    return;
  }

  var idx = 0;
  function processNext(){
    if(idx >= rowsToWrite.length){
      log('Vnos zaključen za ' + rowsToWrite.length + ' vrstic.', 'ok');
      listItems();
      return;
    }
    var row = rowsToWrite[idx];

    // Simulacija ročnega vnosa v obrazec
    if($('surname'))    $('surname').value    = row.surname || '';
    if($('given'))      $('given').value      = row.given || '';
    if($('birthDate_h'))$('birthDate_h').value= row.birthDate || '';
    if($('schedDate_h'))$('schedDate_h').value= row.examDate || '';
    if($('schedTime_h'))$('schedTime_h').value= row.examTime || '';
    if($('procDesc'))   $('procDesc').value   = row.desc || '';
    if($('stationAET')) $('stationAET').value = row.station || '';

    if($('autoPID'))    $('autoPID').checked  = true;
    if($('autoACC'))    $('autoACC').checked  = true;
    if($('patientId'))  $('patientId').value  = '';
    if($('accession'))  $('accession').value  = '';

    // Ustvari MWL za trenutno vrstico, po zaključku nadaljuj na naslednjo
    if(typeof createItem === 'function'){
      createItem(function(){
        idx++;
        processNext();
      });
    } else {
      log('Funkcija createItem ni definirana.', 'err');
    }
  }

  processNext();
}

// ---- Pretvorbe DA/TM (klient) za pošiljanje ----
function toDA(h){
  if(!h) return '';
  h = String(h).trim();
  var m = h.match(/^(\d{2})\.(\d{2})\.(\d{4})$/);
  if(m){
    var dd = m[1], mm = m[2], yyyy = m[3];
    return yyyy+mm+dd;
  }
  m = h.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if(m){
    var yyyy2 = m[1], mm2 = m[2], dd2 = m[3];
    return yyyy2+mm2+dd2;
  }
  m = h.match(/^\d{8}$/);
  if(m) return h;
  return '';
}

function toTM(h){
  if(!h) return '';
  h = String(h).trim();
  var m = h.match(/^(\d{2}):(\d{2})(?::(\d{2}))?$/);
  if(m){
    var HH = m[1], MM = m[2], SS = m[3] || '00';
    return HH+MM+SS;
  }
  m = h.match(/^\d{6}$/);
  if(m) return h;
  m = h.match(/^\d{4}$/);
  if(m) return h + '00';
  return '';
}

// Inicializacija po nalaganju HTML (skript je na koncu body, zato so elementi že prisotni)
loadStations();
renderImportTable();

// ---- Nastavitve ----
function saveCfg(){
  var data = {
    server_base: $('server_base').value.trim().replace(/\/$/,''),
    aet: $('aet').value.trim(),
    username: $('username').value,
    password: $('password').value,
    allow_self_signed: $('allow_self_signed').value === 'true'
  };
  fetch('/api/config', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(data)
  })
    .then(function(r){ return r.json(); })
    .then(function(){ log('Nastavitve shranjene.','ok'); })
    .catch(function(e){ log('Napaka pri shranjevanju: ' + e, 'err'); });
}

// ---- Station AE ----
function loadStations(){
  fetch('/api/stations')
    .then(function(r){ return r.json(); })
    .then(function(j){
      var dl = $('stationList');
      if(!dl) return;
      dl.innerHTML='';
      var items = j.items || [];
      for(var i=0; i<items.length; i++){
        var opt = document.createElement('option');
        opt.value = items[i];
        dl.appendChild(opt);
      }
    })
    .catch(function(){});
}

function saveCurrentAE(){
  var val = ($('stationAET').value||'').trim();
  if(!val){ log('Najprej vnesi AE.','err'); return; }
  fetch('/api/stations', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({value:val})
  })
    .then(function(r){ return r.json(); })
    .then(function(j){
      var dl = $('stationList');
      if(!dl) return;
      dl.innerHTML='';
      var items = j.items || [];
      for(var i=0; i<items.length; i++){
        var opt = document.createElement('option');
        opt.value = items[i];
        dl.appendChild(opt);
      }
      log('AE shranjen v seznam.','ok');
    })
    .catch(function(e){ log('Napaka pri shranjevanju AE: ' + e, 'err'); });
}

// ---- UI: brisanje (ID-jev ne prikazujemo, jih pa uporabimo v gumbu) ----
function deleteItem(spsid, studyuid){
  if(!spsid){ log('Manjka SPS ID.','err'); return; }
  if(!confirm('Res želite izbrisati MWL?')) return;
  var body = studyuid ? {spsid: spsid, studyuid: studyuid} : {spsid: spsid};
  fetch('/api/remove', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  })
    .then(function(r){
      return r.text().then(function(t){
        return {ok:r.ok, t:t};
      });
    })
    .then(function(res){
      if(!res.ok) throw new Error(res.t);
      log('<span class="ok">Element izbrisan.</span>','ok');
      listItems();
    })
    .catch(function(e){ log('Napaka pri brisanju: ' + esc(String(e)), 'err'); });
}

function deleteAllItems(){
  if(!confirm('Res želite izbrisati VSE MWL elemente?')) return;
  fetch('/api/remove_all', {
    method:'POST'
  })
    .then(function(r){
      return r.text().then(function(t){
        return {ok:r.ok, t:t};
      });
    })
    .then(function(res){
      if(!res.ok){
        log('Napaka pri brisanju vseh: ' + esc(res.t), 'err');
        return;
      }
      var msg = 'Vsi MWL elementi so bili izbrisani.';
      try{
        var j = JSON.parse(res.t);
        if(j && j.errors && j.errors.length){
          msg = 'Brisanje vseh je zaključeno z napakami pri nekaterih elementih.';
        }
      }catch(e){}
      log(msg, 'ok');
      listItems();
    })
    .catch(function(e){
      log('Napaka pri brisanju vseh: ' + esc(String(e)), 'err');
    });
}

// ---- Prikaz MWL (brez prikaza SPS/Study UID) ----
function listItems(){
  var st = $('statusText');
  if(st) st.textContent = 'Pridobivanje...';
  fetch('/api/list')
    .then(function(r){
      return r.text().then(function(txt){
        if(!r.ok) throw new Error(txt);
        return txt;
      });
    })
    .then(function(txt){
      try{
        var j = JSON.parse(txt);
        if(!Array.isArray(j) || !j.length){
          log('<span class="muted">Ni najdenih MWL elementov.</span>', 'ok');
          return;
        }
        var html = '<table><tr><th>Pacient</th><th>ID</th><th>Opis postopka / preiskave</th><th>Datum</th><th>Čas</th><th>Postaja</th><th>Briši</th></tr>';
        for(var i=0; i<j.length; i++){
          var it = j[i];
          var spsArr = it.scheduledProcedureStep || [];
          var s = spsArr[0] || {};
          var dHuman = daToHuman(s.scheduledProcedureStepStartDate||'');
          var tHuman = fmtTime(s.scheduledProcedureStepStartTime||'');
          var sps = s.scheduledProcedureStepID || '';
          var suid = it.studyInstanceUID || '';
          html += '<tr>'
            + '<td>'+esc(String(it.patientName||'').replace(/\\^/g,' '))+'</td>'
            + '<td>'+esc(it.patientId||'')+'</td>'
            + '<td>'+esc(it.procedureDescription||'')+'</td>'
            + '<td>'+esc(dHuman)+'</td>'
            + '<td>'+esc(tHuman)+'</td>'
            + '<td>'+esc(s.scheduledStationAETitle||'')+'</td>'
            + '<td>'+(sps?('<button class="btn danger" onclick="deleteItem(\\''+esc(sps)+'\\', \\''+esc(suid)+'\\')">Briši</button>'):'')+'</td>'
            + '</tr>';
        }
        html += '</table>';
        html += '<div style="margin-top:10px;text-align:right;"><button class="btn danger" onclick="deleteAllItems()">Briši vse</button></div>';
        log(html,'ok');
      }catch(e){
        log('<pre>'+esc(txt)+'</pre>', 'err');
      }
    })
    .catch(function(e){
      log('Napaka pri pridobivanju: ' + esc(String(e)), 'err');
    });
}

function clearPatientForm(){
  if($('surname'))     $('surname').value = '';
  if($('given'))       $('given').value = '';
  if($('birthDate_h')) $('birthDate_h').value = '';
  if($('schedDate_h')) $('schedDate_h').value = '';
  if($('schedTime_h')) $('schedTime_h').value = '';
  if($('procDesc'))    $('procDesc').value = '';
  if($('patientId'))   $('patientId').value = '';
  if($('accession'))   $('accession').value = '';
  // modality in stationAET pustimo, ker sta običajno stalni za serijo vnosov
}
// ---- Ustvarjanje MWL (po uspehu samodejno osveži seznam) ----
function createItem(done){
  var surname = ($('surname').value||'').trim();
  var given   = ($('given').value||'').trim();

  var birth_h = ($('birthDate_h').value||'').trim();
  var sdate_h = ($('schedDate_h').value||'').trim();
  var stime_h = ($('schedTime_h').value||'').trim();

  var stationAETVal = $('stationAET') ? ($('stationAET').value||'') : '';
  var autoPIDVal    = $('autoPID') ? $('autoPID').checked : true;
  var autoACCVal    = $('autoACC') ? $('autoACC').checked : true;
  var accessionVal  = $('accession') ? ($('accession').value||'') : '';
  var patientIdVal  = $('patientId') ? ($('patientId').value||'') : '';
  var procDescVal   = $('procDesc') ? ($('procDesc').value||'') : '';
  var modalityVal   = $('modality') ? ($('modality').value||'US') : 'US';

  var birth_da = toDA(birth_h);
  var sched_da = toDA(sdate_h);
  var sched_tm = toTM(stime_h);

  var body = {
    patientSurname: surname,
    patientGiven:   given,
    patientName:    '',
    patientId:      patientIdVal,
    birthDate_da:   birth_da,
    accession:      accessionVal,
    autoACC:        autoACCVal,
    procDesc:       procDescVal,
    modality:       modalityVal,
    schedDate_da:   sched_da,
    schedTime_tm:   sched_tm,
    stationAET:     stationAETVal,
    autoPID:        autoPIDVal
  };

  fetch('/api/create',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  })
    .then(function(r){
      return r.text().then(function(t){
        return {ok:r.ok, t:t};
      });
    })
    .then(function(res){
      var ok = res.ok;
      var t = res.t;
      var pid = '';
      var acc = '';
      var msg = '';
      try{
        var j = JSON.parse(t);
        pid = j.dodeljenID || '';
        acc = j.dodeljenAccession || '';
        if(j && j.odgovorPACS){
          msg = String(j.odgovorPACS);
        }
      }catch(e){}
      if(!ok){
        // PACS (dcm4chee) je vrnil napako, npr. 500 Internal Server Error
        if(msg && msg.indexOf('Internal Server Error') !== -1){
          log('PACS je vrnil napako 500 (Internal Server Error) pri ustvarjanju MWL. Preveri nastavitve in podatke.', 'err');
        }else{
          log('Napaka pri ustvarjanju MWL (HTTP napaka): ' + esc(t), 'err');
        }
        if(typeof done === 'function'){ done(); }
        return;
      }
      if(pid && $('patientId')){ $('patientId').value = pid; }
      if(acc && $('autoACC') && $('autoACC').checked && $('accession')){
        $('accession').value = acc;
      }
      clearPatientForm();
      listItems();
      log('<span class="ok">MWL uspešno ustvarjen.</span>','ok');
      if(typeof done === 'function'){ done(); }
    })
    .catch(function(e){
      // napaka na ravni povezave/JS, ne PACS
      log('Napaka pri ustvarjanju (povezava ali brskalnik): ' + esc(String(e)), 'err');
      if(typeof done === 'function'){ done(); }
    });
}
</script>
</body></html>
"""

# ---------- Zagon ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print(f"\nAplikacija DCM4CHEE MWL deluje na http://127.0.0.1:{port}")
    print("Odpri ta naslov v brskalniku. Za izhod pritisni Ctrl+C.\n")
    app.run(host="127.0.0.1", port=port, debug=False)