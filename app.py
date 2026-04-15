import os
import json
import re
import io
import pdfplumber
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
import streamlit as st
from groq import Groq


# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Datasheet Extractor", page_icon="🔌", layout="wide")
st.title("🔌 Datasheet Thermal Extractor")
st.caption("Upload component datasheets (PDF) to extract thermal parameters automatically.")


# ── API Key ───────────────────────────────────────────────────────────────────
client = Groq(api_key=st.secrets["GROQ_API_KEY"])


# ── System Prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an expert electronics engineer extracting data from component datasheets.

Extract the following fields and return ONLY a valid JSON object, no explanation:
- part_number: The primary part number or component name
- package: Package type (e.g. TO-220, QFN, DPAK, SMD, etc.)
- rth_ja: Thermal resistance junction-to-ambient in °C/W (numeric only, no units)
- rth_jc: Thermal resistance junction-to-case in °C/W (numeric only, no units)
- rth_jb: Thermal resistance junction-to-board in °C/W (numeric only, no units)
- tj_max: Maximum junction temperature in °C (numeric only)
- power_dissipation: Maximum power dissipation in W (numeric only, no units)
- confidence: REQUIRED dict with a key for EVERY field above. Each value must be "high", "low", or "not_found". Never return null for any key.
- flags: list of plain-English warnings. Return empty list [] if none.
- source_quote: REQUIRED dict with a key for EVERY field above. Copy the exact sentence or table row the value was pulled from. If not found, use "not found in datasheet".

Rules:
- If a field value is not found, return null for the value but still set confidence to "not_found" and source_quote to "not found in datasheet"
- confidence and source_quote must ALWAYS be complete dicts with all 7 keys: part_number, package, rth_ja, rth_jc, rth_jb, tj_max, power_dissipation
- For power_dissipation: only extract if explicitly stated as a single value in watts. If "internally limited" or formula only, return null
- If multiple packages exist, extract for the most common or first-listed and flag it
- For confidence: "high" = explicit table value, "low" = inferred or found in text, "not_found" = absent
- Return only the JSON object, nothing else
"""


# ── Helper Functions ──────────────────────────────────────────────────────────
def extract_text_from_pdf(uploaded_file):
    text = ""
    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text


def extract_thermal_section(full_text, window=6000):
    thermal_keywords = [
        r'thermal resistance',
        r'RthJC', r'RthJA', r'RthJB',
        r'θJA', r'θJC', r'θJB',
        r'ΘJA', r'ΘJC',
        r'Theta.*JA', r'Theta.*JC',
        r'junction.{0,10}ambient',
        r'junction.{0,10}case',
        r'maximum ratings',
        r'absolute maximum',
        r'thermal data',
        r'thermal information',
        r'package.*thermal',
    ]
    pattern = '|'.join(thermal_keywords)
    match = re.search(pattern, full_text, re.IGNORECASE)
    if match:
        start = max(0, match.start() - 200)
        end = min(len(full_text), match.start() + window)
        return full_text[start:end]
    else:
        return full_text[:6000]


def safe_dict(val):
    """Return val if it's a dict, else empty dict."""
    return val if isinstance(val, dict) else {}


def safe_list(val):
    """Return val if it's a list, else wrap in list or return empty."""
    if isinstance(val, list):
        return val
    return [str(val)] if val else []


def extract_component_data(uploaded_file, filename):
    FIELDS = ["part_number", "package", "rth_ja", "rth_jc", "rth_jb", "tj_max", "power_dissipation"]

def normalize_result(data):
    """Fill in missing confidence/source_quote keys so Excel is never blank."""
    conf = data.get("confidence")
    if not isinstance(conf, dict):
        conf = {}
    src = data.get("source_quote")
    if not isinstance(src, dict):
        src = {}
    flags = data.get("flags")
    if not isinstance(flags, list):
        flags = [str(flags)] if flags else []

    for field in FIELDS:
        # If LLM extracted a value but forgot confidence, infer it
        if field not in conf or conf[field] is None:
            if data.get(field) is not None:
                conf[field] = "low"   # extracted but confidence unknown → mark low
            else:
                conf[field] = "not_found"

        # If LLM extracted a value but forgot source quote
        if field not in src or src[field] is None:
            src[field] = "not found in datasheet"

    data["confidence"] = conf
    data["source_quote"] = src
    data["flags"] = flags
    return data


def extract_component_data(uploaded_file, filename):
    full_text = extract_text_from_pdf(uploaded_file)
    thermal_text = extract_thermal_section(full_text)

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Extract component data from this datasheet section:\n\n{thermal_text}"}
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        raw_output = response.choices[0].message.content.strip()
        json_match = re.search(r'\{.*\}', raw_output, re.DOTALL)
        data = json.loads(json_match.group()) if json_match else json.loads(raw_output)
        data['source_file'] = filename
        data = normalize_result(data)   # ← fix missing fields here
        return data, None

    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
    except Exception as e:
        return None, str(e)


def build_excel(results):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Component Data"

    headers = [
        "Source File", "Part Number", "Package",
        "RthJA (°C/W)", "RthJA Confidence", "RthJA Source",
        "RthJC (°C/W)", "RthJC Confidence", "RthJC Source",
        "RthJB (°C/W)", "RthJB Confidence", "RthJB Source",
        "Tj Max (°C)", "Tj Confidence", "Tj Source",
        "Power Dissipation (W)", "Power Confidence", "Power Source",
        "Flags"
    ]

    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for row, r in enumerate(results, 2):
        conf = safe_dict(r.get("confidence"))
        src = safe_dict(r.get("source_quote"))
        flags = safe_list(r.get("flags"))

        row_data = [
            r.get("source_file"),
            r.get("part_number"),
            r.get("package"),
            r.get("rth_ja"),            conf.get("rth_ja"),            src.get("rth_ja"),
            r.get("rth_jc"),            conf.get("rth_jc"),            src.get("rth_jc"),
            r.get("rth_jb"),            conf.get("rth_jb"),            src.get("rth_jb"),
            r.get("tj_max"),            conf.get("tj_max"),            src.get("tj_max"),
            r.get("power_dissipation"), conf.get("power_dissipation"), src.get("power_dissipation"),
            " | ".join(flags) if flags else ""
        ]

        for col, value in enumerate(row_data, 1):
            cell = ws.cell(row=row, column=col, value=value)
            if value == "low":
                cell.fill = PatternFill(start_color="FFD966", end_color="FFD966", fill_type="solid")
            elif value == "not_found":
                cell.fill = PatternFill(start_color="F4CCCC", end_color="F4CCCC", fill_type="solid")

    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


# ── UI ────────────────────────────────────────────────────────────────────────
uploaded_files = st.file_uploader(
    "Upload Datasheet PDFs",
    type=["pdf"],
    accept_multiple_files=True
)

if uploaded_files:
    st.write(f"**{len(uploaded_files)} file(s) uploaded.** Click below to extract.")

    if st.button("🚀 Extract Data", type="primary"):
        results = []
        progress = st.progress(0)
        status = st.empty()

        for i, file in enumerate(uploaded_files):
            status.write(f"⏳ Processing **{file.name}**...")
            data, error = extract_component_data(file, file.name)

            if data:
                results.append(data)
            else:
                st.warning(f"⚠️ Failed on {file.name}: {error}")
                results.append({
                    'source_file': file.name,
                    'part_number': 'EXTRACTION FAILED',
                    'package': None, 'rth_ja': None, 'rth_jc': None,
                    'rth_jb': None, 'tj_max': None, 'power_dissipation': None,
                    'confidence': {}, 'flags': [error], 'source_quote': {}
                })

            progress.progress((i + 1) / len(uploaded_files))

        status.success(f"✅ Done! Processed {len(results)} file(s).")

        # Results table
        st.subheader("📊 Extracted Data")
        import pandas as pd
        display_rows = []
        for r in results:
            conf = safe_dict(r.get("confidence"))
            flags = safe_list(r.get("flags"))
            display_rows.append({
                "File": r.get("source_file"),
                "Part No.": r.get("part_number"),
                "Package": r.get("package"),
                "RthJA": r.get("rth_ja"),
                "RthJA ✓": conf.get("rth_ja", "—"),
                "RthJC": r.get("rth_jc"),
                "RthJC ✓": conf.get("rth_jc", "—"),
                "RthJB": r.get("rth_jb"),
                "Tj Max": r.get("tj_max"),
                "Ptot": r.get("power_dissipation"),
                "⚠️ Flags": " | ".join(flags) if flags else "—"
            })

        df = pd.DataFrame(display_rows)
        st.dataframe(df, use_container_width=True)

        # Flags summary
        all_flags = [(r.get("source_file"), f) for r in results for f in safe_list(r.get("flags"))]
        if all_flags:
            st.subheader("⚠️ Flags to Review")
            for source, flag in all_flags:
                st.warning(f"**{source}**: {flag}")

        # Download
        excel_buffer = build_excel(results)
        st.download_button(
            label="📥 Download Excel",
            data=excel_buffer,
            file_name="extracted_data.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

else:
    st.info("👆 Upload one or more PDF datasheets to get started.")