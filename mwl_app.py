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

from flask import Flask, request, jsonify, Response
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime
import os, json, re

# utišaj opozorila za samopodpisan certifikat (po potrebi)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ---------- Privzeta nastavitev ----------
CFG = {
    "server_base": "https://192.168.1.40:30007/dcm4chee-arc",
    "aet": "WORKLIST",
    "username": "admin",
    "password": "admin",
    "allow_self_signed": True,
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
<header><h1>DCM4CHEE MWL — Lokalni odjemalec</h1></header><main>

<section class="card">
<h3>Nastavitve strežnika</h3>
<div class="row">
 <div><label>URL arhiva</label><input id="server_base" value="https://192.168.1.40:30007/dcm4chee-arc"/></div>
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
  $('out').innerHTML = '<div class="'+(cls||'')+'">'+msg+'</div>';
  $('statusText').textContent = (cls==='ok') ? 'OK' : 'Pripravljeno';
}
// Datum (YYYYMMDD -> DD.MM.YYYY)
function daToHuman(da){
  const s = String(da||'').trim();
  if (/^\d{8}$/.test(s)) return s.slice(6,8)+'.'+s.slice(4,6)+'.'+s.slice(0,4);
  return s || '';
}
// Čas (sprejme HHMM, HHMMSS, HH:MM(:SS), z/ex frakcijami)
function fmtTime(tm){
  if (tm == null) return '';
  const s0 = String(tm).trim();
  if (/^\d{2}:\d{2}(:\d{2})?$/.test(s0)) return s0; // že lepo
  const digits = s0.replace(/\D/g,'');              // npr. "173000", "1730"
  if (digits.length < 4) return s0;                  // nimamo vsaj HHMM
  const hh = digits.slice(0,2);
  const mm = digits.slice(2,4);
  const ss = digits.slice(4,6) || '00';
  const H=+hh, M=+mm, S=+ss;
  if (!(H>=0 && H<=23 && M>=0 && M<=59 && S>=0 && S<=59)) return s0;
  return (ss==='00') ? `${hh}:${mm}` : `${hh}:${mm}:${ss}`;
}

// ---- Pretvorbe DA/TM (klient) za pošiljanje ----
function toDA(h){
  if(!h) return '';
  h = String(h).trim();
  let m = h.match(/^(\d{2})\.(\d{2})\.(\d{4})$/);
  if(m){ const [_,dd,mm,yyyy]=m; return `${yyyy}${mm}${dd}`; }
  m = h.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if(m){ const [_,yyyy,mm,dd]=m; return `${yyyy}${mm}${dd}`; }
  m = h.match(/^\d{8}$/); if(m) return h;
  return '';
}
function toTM(h){
  if(!h) return '';
  h = String(h).trim();
  let m = h.match(/^(\d{2}):(\d{2})(?::(\d{2}))?$/);
  if(m){ const [_,HH,MM,SS] = m; return `${HH}${MM}${SS||'00'}`; }
  m = h.match(/^\d{6}$/); if(m) return h;
  m = h.match(/^\d{4}$/); if(m) return h + '00';
  return '';
}

document.addEventListener('DOMContentLoaded', ()=>{ loadStations(); });

// ---- Nastavitve ----
function saveCfg(){
  const data = {
    server_base: $('server_base').value.trim().replace(/\/$/,''),
    aet: $('aet').value.trim(),
    username: $('username').value,
    password: $('password').value,
    allow_self_signed: $('allow_self_signed').value === 'true'
  };
  fetch('/api/config', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data) })
    .then(r=>r.json())
    .then(()=> log('Nastavitve shranjene.','ok'))
    .catch(e=> log('Napaka pri shranjevanju: ' + e, 'err'));
}

// ---- Station AE ----
function loadStations(){
  fetch('/api/stations').then(r=>r.json()).then(j=>{
    const dl = $('stationList'); dl.innerHTML='';
    for(const v of (j.items||[])){ const opt=document.createElement('option'); opt.value=v; dl.appendChild(opt); }
  }).catch(()=>{});
}
function saveCurrentAE(){
  const val = ($('stationAET').value||'').trim();
  if(!val){ log('Najprej vnesi AE.','err'); return; }
  fetch('/api/stations', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({value:val}) })
    .then(r=>r.json())
    .then(j=>{
      const dl = $('stationList'); dl.innerHTML='';
      for(const v of (j.items||[])){ const opt=document.createElement('option'); opt.value=v; dl.appendChild(opt); }
      log('AE shranjen v seznam.','ok');
    })
    .catch(e=> log('Napaka pri shranjevanju AE: ' + e, 'err'));
}

// ---- UI: brisanje (ID-jev ne prikazujemo, jih pa uporabimo v gumbu) ----
function deleteItem(spsid, studyuid){
  if(!spsid){ log('Manjka SPS ID.','err'); return; }
  if(!confirm('Res želite izbrisati MWL?')) return;
  const body = studyuid ? {spsid, studyuid} : {spsid};
  fetch('/api/remove', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) })
    .then(r=>r.text().then(t=>({ok:r.ok,t})))
    .then(({ok,t})=>{
      if(!ok) throw new Error(t);
      log('<span class="ok">Element izbrisan.</span>','ok');
      listItems();
    })
    .catch(e=> log('Napaka pri brisanju: ' + esc(String(e)), 'err'));
}

// ---- Prikaz MWL (brez prikaza SPS/Study UID) ----
function listItems(){
  $('statusText').textContent = 'Pridobivanje...';
  fetch('/api/list')
    .then(async r=>{ const txt=await r.text(); if(!r.ok) throw new Error(txt); return txt; })
    .then(txt=>{
      try{
        const j = JSON.parse(txt);
        if(!Array.isArray(j) || !j.length){
          log('<span class="muted">Ni najdenih MWL elementov.</span>', 'ok');
          return;
        }
        let html = '<table><tr><th>Pacient</th><th>ID</th><th>Napotnica</th><th>Modaliteta</th><th>Datum</th><th>Čas</th><th>Postaja</th><th>Briši</th></tr>';
        for(const it of j){
          const s=(it.scheduledProcedureStep||[])[0]||{};
          const dHuman = daToHuman(s.scheduledProcedureStepStartDate||'');
          const tHuman = fmtTime(s.scheduledProcedureStepStartTime||'');
          const sps   = s.scheduledProcedureStepID || '';
          const suid  = it.studyInstanceUID || '';
          html += '<tr>'
            + '<td>'+esc(String(it.patientName||'').replace(/\^/g,' '))+'</td>'
            + '<td>'+esc(it.patientId||'')+'</td>'
            + '<td>'+esc(it.accessionNumber||'')+'</td>'
            + '<td>'+esc(s.modality||'')+'</td>'
            + '<td>'+esc(dHuman)+'</td>'
            + '<td>'+esc(tHuman)+'</td>'
            + '<td>'+esc(s.scheduledStationAETitle||'')+'</td>'
            + '<td>'+(sps?('<button class="btn danger" onclick="deleteItem(\\''+esc(sps)+'\\', \\''+esc(suid)+'\\')">Briši</button>'):'')+'</td>'
            + '</tr>';
        }
        html += '</table>';
        log(html,'ok');
      }catch{
        log('<pre>'+esc(txt)+'</pre>', 'err');
      }
    })
    .catch(e=> log('Napaka pri pridobivanju: ' + esc(String(e)), 'err'));
}

// ---- Ustvarjanje MWL (po uspehu samodejno osveži seznam) ----
function createItem(){
  const surname = ($('surname').value||'').trim();
  const given   = ($('given').value||'').trim();

  const birth_h = ($('birthDate_h').value||'').trim();
  const sdate_h = ($('schedDate_h').value||'').trim();
  const stime_h = ($('schedTime_h').value||'').trim();

  const birth_da = toDA(birth_h);
  const sched_da = toDA(sdate_h);
  const sched_tm = toTM(stime_h);

  const body = {
    patientSurname: surname,
    patientGiven:   given,
    patientName:    '',
    patientId:      $('patientId').value||'',
    birthDate_da:   birth_da,
    accession:      $('accession').value||'',
    autoACC:        $('autoACC').checked,   // <-- NOVO
    procDesc:       $('procDesc').value||'',
    modality:       $('modality').value||'US',
    schedDate_da:   sched_da,
    schedTime_tm:   sched_tm,
    stationAET:     $('stationAET').value||'',
    autoPID:        $('autoPID').checked
  };

  fetch('/api/create',{ method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) })
    .then(r=>r.text().then(t=>({ok:r.ok,t})))
    .then(({ok,t})=>{
      let pid='', acc='';
      try{ const j=JSON.parse(t); pid=j.dodeljenID||''; acc=j.dodeljenAccession||''; }catch{}
      if(!ok) throw new Error(t);
      if(pid){ $('patientId').value = pid; }
      if(acc && $('autoACC').checked){ $('accession').value = acc; }
      // samodejno osveži seznam
      listItems();
      log('<span class="ok">MWL uspešno ustvarjen.</span>','ok');
    })
    .catch(e=> log('Napaka pri ustvarjanju: ' + esc(String(e)), 'err'));
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