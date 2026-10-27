import sys
import traceback

try:
    import os
    import re
    import fitz  # PyMuPDF
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    import threading
    import queue
    import json
    import time
    import csv
    import subprocess
    import math
    import concurrent.futures
    import docx2txt
    import asyncio
    import random
    from pydantic import BaseModel, Field
    from typing import Optional, List, Callable, Tuple
    from openai import OpenAI, AsyncOpenAI
    from drive_uploader import upload_all_resumes, get_file_hash, atomic_write_json
    from workflow_policy import exportable, csv_fields, csv_values, attention_reasons, merge_duplicates, ReviewStore, parsed as parsed_candidate, clean as clean_field
    from app_preferences import DEFAULT_PREFERENCES, PreferenceStore, SecureApiKeyStore, SettingsWindow
    from ui_interactions import (SmoothTreeScroll, Tooltip, animate_window_open, animate_window_close, fade_through,
                                 animate_height, is_animating, MOTION_ENTER_MS, MOTION_EXIT_MS, UI_FONT_FAMILY)
except Exception as startup_err:
    tb = traceback.format_exc()
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, f"Startup Error:\n\n{tb}", "Error", 0x10)
    except Exception:
        pass
    print(f"Startup Error:\n{tb}")
    sys.exit(1)

# Setup CustomTkinter styling
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")
ctk.ThemeManager.theme["CTkFont"]["family"] = UI_FONT_FAMILY

class LogRedirector:
    def __init__(self, filepath):
        # Line-buffered: each completed line is flushed by the io layer, so no
        # per-write flush is needed. The lock keeps concurrent writers from
        # interleaving partial writes. Reentrant so a write that triggers another
        # write on the same thread (e.g. a warning emitted mid-write) cannot deadlock.
        self.file = open(filepath, "a", encoding="utf-8", buffering=1)
        self._lock = threading.RLock()

    def write(self, message):
        with self._lock:
            self.file.write(message)

    def flush(self):
        with self._lock:
            self.file.flush()

def setup_logging():
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "app.log")
    try:
        redirector = LogRedirector(log_file)
        sys.stdout = redirector
        sys.stderr = redirector
    except Exception as e:
        print(f"Failed to setup logging redirector: {e}")

def normalize_name(name: str) -> str:
    if not name or name in ("Not Found", "API Error", "Read Error", "Filtered"):
        return name
    name = name.strip()
    
    # 1. Detect spaced letters (e.g., "R A V I N D E R   J A S S A L")
    tokens = name.split()
    if len(tokens) > 1:
        single_letter_tokens = [t for t in tokens if len(t) == 1]
        if len(single_letter_tokens) / len(tokens) >= 0.70:
            # Reconstruct words split by two or more consecutive spaces
            word_blocks = re.split(r'\s{2,}', name)
            reconstructed_words = [block.replace(" ", "") for block in word_blocks]
            name = " ".join(reconstructed_words)

    # 2. Convert all-uppercase names to title case, preserving mixed-case names (e.g., McDonald)
    if name.isupper():
        name = name.title()
        
    return name

# Country mappings and keywords for location resolution
COUNTRY_NORMALIZATION_MAP = {
    "US": "United States",
    "USA": "United States",
    "U.S.": "United States",
    "U.S.A.": "United States",
    "UNITED STATES OF AMERICA": "United States",
    "UNITED STATES": "United States",
    "UK": "United Kingdom",
    "GB": "United Kingdom",
    "GREAT BRITAIN": "United Kingdom",
    "UNITED KINGDOM": "United Kingdom",
    "UAE": "United Arab Emirates",
    "UNITED ARAB EMIRATES": "United Arab Emirates",
    "AE": "United Arab Emirates",
    "INDIA": "India",
    "IN": "India",
    "CANADA": "Canada",
    "CA": "Canada",
    "IRELAND": "Ireland",
    "IE": "Ireland",
}

TEXT_COUNTRY_KEYWORDS = {
    "India": ["india", "delhi", "mumbai", "bangalore", "bengaluru", "chennai", "hyderabad", "pune", "iit ", "bits pilani", "iisc"],
    "United Kingdom": ["united kingdom", "london", "manchester", "birmingham", "edinburgh", "glasgow", " u.k.", " uk ", "great britain"],
    "United States": ["united states", "usa", " u.s.", "california", "new york", "texas", "washington", "boston", "san francisco", "chicago", "seattle", "mit", "stanford", "harvard"],
    "Canada": ["canada", "toronto", "vancouver", "montreal", "ottawa", "calgary", "ontario", "quebec", "alberta", "british columbia", "waterloo university", "mcgill"],
    "Ireland": ["ireland", "dublin", "cork", "limerick", "galway", "trinity college", "ucd"],
    "United Arab Emirates": ["united arab emirates", "dubai", "abu dhabi", " u.a.e.", " uae "],
}

def standardize_phone_and_infer_country(phone_str: str) -> (str, str):
    if not phone_str or phone_str in ("Not Found", "API Error", "Read Error", "Filtered"):
        return phone_str, "Not Found"
        
    cleaned_digits = re.sub(r'\D', '', phone_str)
    inferred_country = "Not Found"
    local_phone_raw = cleaned_digits
    
    is_prefixed = phone_str.strip().startswith('+') or phone_str.strip().startswith('00')
    
    if is_prefixed:
        stripped_phone = phone_str.strip()
        if stripped_phone.startswith('+'):
            stripped_phone = stripped_phone[1:]
        elif stripped_phone.startswith('00'):
            stripped_phone = stripped_phone[2:]
            
        stripped_digits = re.sub(r'\D', '', stripped_phone)
        
        if stripped_digits.startswith('1'):
            local_phone_raw = stripped_phone[1:].strip()
            local_phone_digits = re.sub(r'\D', '', local_phone_raw)
            area_code = local_phone_digits[:3]
            canada_area_codes = {
                "647", "416", "437", "613", "514", "403", "204", "226", "249", "250", "289", "306", 
                "343", "365", "367", "418", "431", "438", "450", "506", "519", "579", "581", "587", 
                "705", "709", "778", "780", "782", "807", "819", "825", "867", "873", "902", "905"
            }
            if area_code in canada_area_codes:
                inferred_country = "Canada"
            else:
                inferred_country = "United States"
        else:
            matched = False
            for prefix, country in [("971", "United Arab Emirates"), ("353", "Ireland"), ("44", "United Kingdom"), ("91", "India")]:
                if stripped_digits.startswith(prefix):
                    local_phone_raw = stripped_phone[len(prefix):].strip()
                    inferred_country = country
                    matched = True
                    break
    else:
        for prefix, country in [("971", "United Arab Emirates"), ("353", "Ireland"), ("44", "United Kingdom"), ("91", "India")]:
            if cleaned_digits.startswith(prefix) and len(cleaned_digits) == 10 + len(prefix):
                local_phone_raw = cleaned_digits[len(prefix):]
                inferred_country = country
                break
        if cleaned_digits.startswith('1') and len(cleaned_digits) == 11:
            local_phone_raw = cleaned_digits[1:]
            local_phone_digits = re.sub(r'\D', '', local_phone_raw)
            area_code = local_phone_digits[:3]
            canada_area_codes = {
                "647", "416", "437", "613", "514", "403", "204", "226", "249", "250", "289", "306", 
                "343", "365", "367", "418", "431", "438", "450", "506", "519", "579", "581", "587", 
                "705", "709", "778", "780", "782", "807", "819", "825", "867", "873", "902", "905"
            }
            if area_code in canada_area_codes:
                inferred_country = "Canada"
            else:
                inferred_country = "United States"

    cleaned = re.sub(r'\D', '', local_phone_raw)
    if cleaned:
        local_phone = cleaned
    else:
        local_phone = local_phone_raw.strip()
        
    return local_phone, inferred_country

def normalize_country_name(country_str: str) -> str:
    if not country_str or country_str in ("Not Found", "API Error", "Read Error", "Filtered"):
        return "Not Found"
        
    cleaned = country_str.strip().upper()
    cleaned = re.sub(r'[^\w\s]', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned)
    
    if cleaned in COUNTRY_NORMALIZATION_MAP:
        return COUNTRY_NORMALIZATION_MAP[cleaned]
        
    return country_str.strip().title()

def infer_country_from_text(text: str) -> str:
    if not text:
        return "Not Found"
    text_lower = text.lower()
    for country, keywords in TEXT_COUNTRY_KEYWORDS.items():
        for kw in keywords:
            pattern = r'(?<!\w)' + re.escape(kw.strip()) + r'(?!\w)'
            if re.search(pattern, text_lower):
                return country
    return "Not Found"

def extract_docx_hyperlinks(docx_path: str) -> list:
    import zipfile
    import xml.etree.ElementTree as ET
    links = []
    if not docx_path or not os.path.exists(docx_path):
        return links
    try:
        with zipfile.ZipFile(docx_path, 'r') as z:
            for name in z.namelist():
                if name.startswith('word/_rels/') and name.endswith('.xml.rels'):
                    try:
                        rels_xml = z.read(name)
                        root = ET.fromstring(rels_xml)
                        for child in root:
                            tag = child.tag.split('}')[-1]
                            if tag == 'Relationship':
                                rel_type = child.attrib.get('Type', '')
                                target = child.attrib.get('Target', '')
                                if 'hyperlink' in rel_type.lower() and target:
                                    links.append(target)
                    except Exception as parse_err:
                        print(f"Error parsing relationships file {name} in {docx_path}: {parse_err}")
    except Exception as zip_err:
        print(f"Error reading DOCX archive {docx_path}: {zip_err}")
    return links

def extract_pdf_hyperlinks(pdf_path: str) -> list:
    links = []
    if not pdf_path or not os.path.exists(pdf_path):
        return links
    try:
        doc = fitz.open(pdf_path)
        for page in doc:
            try:
                page_links = page.get_links()
                for link in page_links:
                    if link.get("kind") == fitz.LINK_URI:
                        uri = link.get("uri", "")
                        if uri:
                            links.append(uri)
            except Exception as e:
                print(f"Error reading links from page in {pdf_path}: {e}")
        doc.close()
    except Exception as err:
        print(f"Error reading PDF archive {pdf_path}: {err}")
    return links

def normalize_linkedin_url(url: str) -> str:
    if not url:
        return ""
    if '](' in url:
        m = re.search(r'\]\(([^)]+)\)', url)
        if m:
            url = m.group(1)
    url = url.split('?')[0].split('#')[0]
    url = url.strip().rstrip('/')
    match = re.search(r'linkedin\.com/(in|pub|company)/([a-zA-Z0-9\-_\+%.#&=/]+)', url, re.IGNORECASE)
    if match:
        path_type = match.group(1).lower()
        path_val = match.group(2).rstrip('/')
        return f"https://www.linkedin.com/{path_type}/{path_val}"
    return url

def normalize_and_validate_candidate(candidate_data: dict, raw_resume_text: str, filepath: str = None) -> (Optional[dict], str):
    cand = candidate_data.copy()
    
    # 0. LinkedIn URL Extraction
    visible_urls = re.findall(r'(?:https?://)?(?:www\.)?linkedin\.com/(?:in|pub|company)/[a-zA-Z0-9\-_\+%.#\?&=/]+', raw_resume_text, re.IGNORECASE)
    
    docx_hyperlink_urls = []
    pdf_hyperlink_urls = []
    if filepath:
        if filepath.lower().endswith('.docx'):
            docx_hyperlink_urls = extract_docx_hyperlinks(filepath)
        elif filepath.lower().endswith('.pdf'):
            pdf_hyperlink_urls = extract_pdf_hyperlinks(filepath)
        
    normalized_visible = []
    for u in visible_urls:
        norm = normalize_linkedin_url(u)
        if norm and any(p in norm for p in ['/in/', '/pub/', '/company/']):
            if norm not in normalized_visible:
                normalized_visible.append(norm)
                
    normalized_docx_hyperlinks = []
    for u in docx_hyperlink_urls:
        norm = normalize_linkedin_url(u)
        if norm and any(p in norm for p in ['/in/', '/pub/', '/company/']):
            if norm not in normalized_docx_hyperlinks:
                normalized_docx_hyperlinks.append(norm)
                
    normalized_pdf_hyperlinks = []
    for u in pdf_hyperlink_urls:
        norm = normalize_linkedin_url(u)
        if norm:
            norm_lower = norm.lower()
            if any(p in norm_lower for p in ['/in/', '/pub/']) and not any(p in norm_lower for p in ['/company/', '/jobs/', '/learning/', '/feed/', '/posts/']):
                if norm not in normalized_pdf_hyperlinks:
                    normalized_pdf_hyperlinks.append(norm)
                
    selected_url = ""
    source = "Not Found"
    
    # Priority 1: /in/
    in_visible = [u for u in normalized_visible if '/in/' in u]
    in_pdf = [u for u in normalized_pdf_hyperlinks if '/in/' in u]
    in_docx = [u for u in normalized_docx_hyperlinks if '/in/' in u]
    if in_visible:
        selected_url = in_visible[0]
        source = "Visible Text"
    elif in_pdf:
        selected_url = in_pdf[0]
        source = "PDF Hyperlink"
    elif in_docx:
        selected_url = in_docx[0]
        source = "DOCX Hyperlink"
        
    # Priority 2: /pub/
    if not selected_url:
        pub_visible = [u for u in normalized_visible if '/pub/' in u]
        pub_pdf = [u for u in normalized_pdf_hyperlinks if '/pub/' in u]
        pub_docx = [u for u in normalized_docx_hyperlinks if '/pub/' in u]
        if pub_visible:
            selected_url = pub_visible[0]
            source = "Visible Text"
        elif pub_pdf:
            selected_url = pub_pdf[0]
            source = "PDF Hyperlink"
        elif pub_docx:
            selected_url = pub_docx[0]
            source = "DOCX Hyperlink"
            
    # Priority 3: /company/
    if not selected_url:
        comp_visible = [u for u in normalized_visible if '/company/' in u]
        comp_docx = [u for u in normalized_docx_hyperlinks if '/company/' in u]
        if comp_visible:
            selected_url = comp_visible[0]
            source = "Visible Text"
        elif comp_docx:
            selected_url = comp_docx[0]
            source = "DOCX Hyperlink"
            
    cand["linkedin_url"] = selected_url
    cand["linkedin_source"] = source

    # 1. Normalize Name
    name = normalize_name(cand.get("Name"))
    cand["Name"] = name
    
    # 2. Normalize Phone
    phone = cand.get("Phone")
    std_phone, inferred_country = standardize_phone_and_infer_country(phone)
    cand["Phone"] = std_phone
    
    # 3. Normalize & Resolve Country
    country = cand.get("Country")
    normalized_country = normalize_country_name(country)
    resolution_method = "Unresolved"
    
    if normalized_country != "Not Found":
        resolution_method = "AI Extraction"
    else:
        # Priority 2: Phone number inference
        if inferred_country != "Not Found":
            normalized_country = inferred_country
            resolution_method = "Phone Inference"
        
    # Priority 3, 4, 5: Check sections in priority order
    if normalized_country == "Not Found":
        sections = parse_resume_sections(raw_resume_text)
        
        # Priority 3: Resume address/location section (SUMMARY)
        inferred_addr = infer_country_from_text(sections.get("SUMMARY", ""))
        if inferred_addr != "Not Found":
            normalized_country = inferred_addr
            resolution_method = "Location Inference"
            
        # Priority 4: Most recent employer location (EXPERIENCE)
        if normalized_country == "Not Found":
            inferred_exp = infer_country_from_text(sections.get("EXPERIENCE", ""))
            if inferred_exp != "Not Found":
                normalized_country = inferred_exp
                resolution_method = "Location Inference"
                
        # Priority 5: University/education location (EDUCATION)
        if normalized_country == "Not Found":
            inferred_edu = infer_country_from_text(sections.get("EDUCATION", ""))
            if inferred_edu != "Not Found":
                normalized_country = inferred_edu
                resolution_method = "Location Inference"
            
    cand["Country"] = normalized_country
    cand["CountryResolutionMethod"] = resolution_method
    
    # 4. Validate Candidate
    def is_invalid_value(val):
        return val is None or str(val).strip() in ("", "Not Found", "API Error", "Read Error", "Filtered", "None")
        
    if is_invalid_value(cand.get("Name")):
        return cand, "Missing Name"
        
    if is_invalid_value(cand.get("Email")):
        return cand, "Missing Email"
        
    if is_invalid_value(cand.get("Phone")):
        return cand, "Missing Phone"
        
    if is_invalid_value(cand.get("Country")):
        return cand, "Country Unresolved"

    if is_invalid_value(cand.get("YearsOfExperience")):
        return cand, "Missing Years of Experience"
        
    return cand, "Valid"

def is_valid_candidate(row: dict, allow_pending: bool = False) -> bool:
    if not row:
        return False
    
    def is_invalid_value(val):
        return val is None or str(val).strip() in ("", "Not Found", "API Error", "Read Error", "Filtered", "None")
        
    if is_invalid_value(row.get("Name")):
        return False
    if is_invalid_value(row.get("Email")):
        return False
    if is_invalid_value(row.get("Phone")):
        return False
    if is_invalid_value(row.get("Country")):
        return False
    if is_invalid_value(row.get("YearsOfExperience")):
        return False
        
    res_link = row.get("ResumeLink")
    if is_invalid_value(res_link):
        return False

    # allow_pending (table view): a parsed candidate stays visible whatever its upload
    # state. Export (allow_pending=False) needs a finished upload.
    if res_link == "Not uploaded":
        return False
    if not allow_pending and res_link in ("Pending", "Uploading", "Failed", "Cancelled"):
        return False

    return True

def safe_match_percentage(cand: dict) -> float:
    val = cand.get("MatchPercentage", 0)
    if val is None or str(val).strip() in ("N/A", "Error", "Filtered", ""):
        return 0.0
    try:
        return float(val)
    except ValueError:
        return 0.0

def get_deduplicated_candidates(candidates: list) -> list:
    return merge_duplicates(candidates)


BATCH_THRESHOLD = 15

LUNA_MODEL = "gpt-6-luna"

# Available OpenAI models (Luna first = default)
OPENAI_MODELS = [
    LUNA_MODEL,
    "gpt-5",
    "gpt-5-mini",
    "gpt-4.1",
    "gpt-4o",
    "gpt-4o-mini",
]

class ResumeData(BaseModel):
    full_name: str = Field(description="The full name of the candidate. If not found, return 'Not Found'.")
    email_address: str = Field(description="The email address of the candidate. If not found, return 'Not Found'.")
    phone_number: str = Field(description="A single phone number... return only digits/spaces/dashes...")
    country: str = Field(description="The country the candidate is based in...")
    years_of_experience: Optional[float] = Field(default=None, description="Total years of professional work experience...")
    source_filename: str = Field(description="The exact original filename of the resume passed in the prompt.")

class ParsedJD(BaseModel):
    required_skills: List[str] = Field(description="Mandatory technical and soft skills the candidate MUST have.")
    preferred_skills: List[str] = Field(description="Nice-to-have skills that would strengthen candidacy but are not mandatory.")
    nice_to_have_technologies: List[str] = Field(description="Optional/bonus tools, languages, or frameworks mentioned.")
    experience_requirements: str = Field(description="Narrative summary of required experience (e.g., '5+ years building REST APIs').")
    minimum_years: int = Field(default=0, description="Minimum years of experience required as an integer (e.g., 5). Extract from text.")
    education_requirements: str = Field(description="Summary of required or preferred education level and field.")
    mandatory_technologies: List[str] = Field(description="Must-have programming languages, tools, cloud platforms, frameworks. Be exhaustive.")
    domain_requirements: str = Field(description="Specific industry or domain knowledge required (e.g., fintech, healthcare, e-commerce).")
    seniority_expectations: str = Field(description="Expected seniority level (e.g., Junior, Mid-level, Senior, Staff, Lead, Principal).")
    deal_breakers: List[str] = Field(description="Explicit disqualifiers or hard requirements — things the JD says are absolutely required or will result in rejection if missing.")

class SkillEvidence(BaseModel):
    skill: str = Field(description="The skill or technology from the JD")
    status: str = Field(description="One of: 'present', 'partial', 'missing'")
    evidence: str = Field(description="Brief quote or paraphrase from the resume proving presence/absence. If missing, state 'Not found in resume'.")

class MatchResult(BaseModel):
    skill_evidence: List[SkillEvidence] = Field(description="Evidence-based audit of every mandatory skill and technology from the JD. Go through each one systematically.")
    technical_match: int = Field(description="Technical skills score (0-100). Count how many mandatory_technologies are 'present' vs 'missing'. Missing = heavy deduction (-15 to -20 each). Partial = half credit.")
    experience_match: int = Field(description="Experience score (0-100). Compare candidate's demonstrated years AND seniority signals (team lead, architecture decisions, scale of systems) against JD requirements.")
    domain_match: int = Field(description="Domain/industry fit (0-100). Does the candidate's work history align with the required domain? Adjacent domains get partial credit.")
    match_percentage: int = Field(description="Overall score = (technical_match * 0.50) + (experience_match * 0.30) + (domain_match * 0.20). Compute this explicitly. Do NOT round to a multiple of 5.")
    strengths: List[str] = Field(description="3-5 specific strengths directly evidenced in resume that align to JD requirements. Quote resume evidence.")
    weaknesses: List[str] = Field(description="3-5 specific gaps with clear reasoning why they matter for this role.")
    missing_requirements: List[str] = Field(description="Skills/tools from mandatory_technologies or deal_breakers that are 'missing' or 'partial' in the evidence audit.")
    summary: str = Field(description="3 sentences: (1) overall fit assessment, (2) key strength, (3) critical gap or risk.")


def strip_country_code(phone: str) -> str:
    if not phone or phone in ("Not Found", "API Error", "Read Error"):
        return phone
    cleaned = re.sub(r"^\s*(\+|00)\d{1,3}[\s\-.]?", "", phone).strip()
    return cleaned if cleaned else phone

def extract_text_from_pdf(filepath):
    try:
        doc = fitz.open(filepath)
        text = ""
        for page in doc:
            page_text = page.get_text("text")
            if page_text:
                text += page_text + "\n"
        doc.close()
        return text
    except Exception:
        return None

def _word_open_document(word, path):
    """Open `path` in a running Word.Application via the documented Documents.Open API
    (the Application object has no Open method). Read-only, no conversion dialog, not
    added to recent files; the dummy password makes protected files fail fast instead
    of blocking on a password prompt. Close with doc.Close(SaveChanges=0)."""
    return word.Documents.Open(os.path.abspath(path), ConfirmConversions=False, ReadOnly=True,
                               AddToRecentFiles=False, PasswordDocument="x")


def convert_docx_to_pdf(docx_path: str) -> Optional[str]:
    """Convert a .docx file to PDF using fitz/PyMuPDF story or Word COM (Windows).
    Returns the path to the generated PDF, or None on failure."""
    pdf_path = docx_path[:-5] + "_converted.pdf"
    try:
        # Try Windows COM automation first (most accurate on Windows)
        import comtypes.client
        word = comtypes.client.CreateObject('Word.Application')
        word.Visible = False
        doc = _word_open_document(word, docx_path)
        doc.SaveAs(os.path.abspath(pdf_path), FileFormat=17)  # 17 = wdFormatPDF
        doc.Close(SaveChanges=0)
        word.Quit()
        return pdf_path
    except Exception:
        pass
    try:
        # Fallback: LibreOffice headless
        import subprocess
        out_dir = os.path.dirname(os.path.abspath(docx_path))
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE
        result = subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf", "--outdir", out_dir, os.path.abspath(docx_path)],
            capture_output=True, timeout=60, startupinfo=startupinfo
        )
        candidate = os.path.join(out_dir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
        if result.returncode == 0 and os.path.exists(candidate):
            return candidate
    except Exception:
        pass
    return None

def extract_text_from_doc(filepath, word=None):
    """Extract text from a legacy .doc file using Windows COM automation (if available) or LibreOffice headless fallback.

    If `word` (an already-running Word.Application) is given, it is reused: the caller owns
    CoInitialize/Quit, and a per-file COM error falls through to the LibreOffice fallback.
    """
    # 1. Try Windows COM automation first
    if word is not None:
        try:
            doc = _word_open_document(word, filepath)
            try:
                text = doc.Content.Text
            finally:
                doc.Close(SaveChanges=0)
            if text:
                return text
        except Exception as e:
            print(f"[-] COM automation failed for {filepath}: {e}")
    else:
        try:
            import pythoncom
            import comtypes.client
            pythoncom.CoInitialize()
            try:
                word = comtypes.client.CreateObject('Word.Application')
                try:
                    word.Visible = False
                    doc = _word_open_document(word, filepath)
                    try:
                        text = doc.Content.Text
                    finally:
                        doc.Close(SaveChanges=0)
                finally:
                    word.Quit()
                if text:
                    return text
            finally:
                pythoncom.CoUninitialize()
        except Exception as e:
            print(f"[-] COM automation failed for {filepath}: {e}")

    # 2. Try LibreOffice headless fallback
    try:
        import subprocess
        out_dir = os.path.dirname(os.path.abspath(filepath))
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE
        result = subprocess.run(
            ["soffice", "--headless", "--convert-to", "txt:Text", "--outdir", out_dir, os.path.abspath(filepath)],
            capture_output=True, timeout=60, startupinfo=startupinfo
        )
        txt_path = os.path.splitext(filepath)[0] + ".txt"
        if result.returncode == 0 and os.path.exists(txt_path):
            with open(txt_path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
            try:
                os.remove(txt_path)
            except Exception:
                pass
            return text
    except Exception as e:
        print(f"[-] LibreOffice conversion failed for {filepath}: {e}")

    print(f"Failed to extract legacy Word document:\n{os.path.basename(filepath)}")
    return None

def extract_doc_batch(filepaths):
    """Extract many legacy .doc files with a single Word instance.

    Returns {filepath: text_or_None}. If COM is unavailable, each file is extracted
    individually (COM per file, then LibreOffice).
    """
    results = {}
    inited = False
    word = None
    pythoncom = None
    try:
        try:
            import pythoncom
            import comtypes.client
            pythoncom.CoInitialize()
            inited = True
            word = comtypes.client.CreateObject('Word.Application')
            word.Visible = False
            try:
                word.DisplayAlerts = 0
            except Exception:
                pass
        except Exception as e:
            print(f"[-] COM automation unavailable for batch: {e}")
            word = None
        for p in filepaths:
            try:
                if word is not None:
                    results[p] = extract_text_from_doc(p, word=word)
                else:
                    results[p] = extract_text_from_doc(p)
            except Exception as e:
                print(f"[-] .doc extraction failed for {p}: {e}")
                results[p] = None
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        if inited:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
    return results

def extract_text_from_file(filepath):
    if filepath.lower().endswith('.pdf'):
        return extract_text_from_pdf(filepath)
    elif filepath.lower().endswith('.docx'):
        try:
            return docx2txt.process(filepath)
        except Exception:
            return None
    elif filepath.lower().endswith('.doc'):
        return extract_text_from_doc(filepath)
    elif filepath.lower().endswith('.txt'):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            return None
    return None

# Section headers commonly found in resumes
_SECTION_PATTERNS = [
    (r'(?i)(skills?|technical skills?|core competencies|technologies)', 'SKILLS'),
    (r'(?i)(work experience|experience|employment history|professional experience)', 'EXPERIENCE'),
    (r'(?i)(projects?|personal projects?|key projects?)', 'PROJECTS'),
    (r'(?i)(education|academic background|qualifications)', 'EDUCATION'),
    (r'(?i)(certifications?|certificates?|licenses?)', 'CERTIFICATIONS'),
    (r'(?i)(summary|objective|profile|about me)', 'SUMMARY'),
]

def preprocess_resume_text(raw_text: str, max_chars: int = 8000) -> str:
    """Clean raw resume text and label key sections for more accurate AI evaluation."""
    if not raw_text:
        return ""
    # Remove excessive whitespace and decorative lines
    text = re.sub(r'[\u2500-\u257F\u2014\u2013\-=_]{4,}', '---', raw_text)  # decorative separators
    text = re.sub(r'[ \t]+', ' ', text)               # collapse horizontal whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)            # collapse multiple blank lines
    text = re.sub(r'(Page \d+ of \d+)', '', text, flags=re.IGNORECASE)  # page numbers
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append('')
            continue
        # Check if line is a section header and label it
        labeled = False
        for pattern, label in _SECTION_PATTERNS:
            if re.match(pattern + r'\s*:?\s*$', stripped):
                cleaned_lines.append(f'\n### [{label}] ###')
                labeled = True
                break
        if not labeled:
            cleaned_lines.append(stripped)
    result = '\n'.join(cleaned_lines)
    result = re.sub(r'\n{3,}', '\n\n', result).strip()
    return result[:max_chars]

def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(a * a for a in vec1))
    magnitude2 = math.sqrt(sum(b * b for b in vec2))
    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0
    return dot_product / (magnitude1 * magnitude2)

def get_embedding(text: str, client: OpenAI) -> List[float]:
    """Generate embeddings using the small model for higher pre-filtering accuracy."""
    try:
        response = client.embeddings.create(
            model="text-embedding-3-small",
            input=text[:8192]
        )
        return response.data[0].embedding
    except Exception as e:
        print(f"Embedding error: {e}")
        return []

def parse_jd_with_ai(jd_text: str, client: OpenAI) -> Optional[ParsedJD]:
    if not jd_text.strip():
        return None
    try:
        system = (
            "You are a precision JD analyst. Extract EVERY requirement from the job description. "
            "Be exhaustive on mandatory_technologies — include ALL tools, languages, frameworks, cloud services mentioned. "
            "For deal_breakers, list anything the JD marks as required, must-have, or non-negotiable. "
            "Set minimum_years as an integer extracted from phrases like '5+ years' or 'at least 3 years'."
        )
        prompt = f"Parse this job description into structured requirements.\n\nJob Description:\n---\n{jd_text[:15000]}\n---"
        completion = client.beta.chat.completions.parse(
            model=LUNA_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt}
            ],
            response_format=ParsedJD,
            timeout=60
        )
        return completion.choices[0].message.parsed
    except Exception as e:
        print(f"JD Parse Error: {e}")
        return None

def evaluate_candidate_match(resume_text: str, jd_data: ParsedJD, candidate_data: dict, client: OpenAI, model: str) -> dict:
    try:
        system_prompt = """You are a Senior Technical Recruiter with 15 years of experience across both
tech companies and research institutions. Your evaluations are precise, evidence-based, and
calibrated to real-world hiring outcomes — neither inflated to seem encouraging nor
deflated by over-literal skill matching.

## CORE PHILOSOPHY
A score answers: "If hired today, how likely is this candidate to succeed in this role within
the first 30 days?"
- Missing a tool they can learn in a week ≠ missing a core competency.
- Partial evidence of a skill deserves genuine partial credit.
- Strong transferable or equivalent skills MUST reduce penalties.
- Preferred/nice-to-have gaps are minor friction, not disqualifiers.

════════════════════════════════════════════════════════
SECTION A — ROLE TYPE DETECTION (do this first, every time)
════════════════════════════════════════════════════════
Before scoring, classify the role as one of:
  [RESEARCH]  → PhD, scientist, research specialist, lab, academic, LLM trainer
  [TECH]      → Software engineer, developer, DevOps, data engineer, ML engineer
  [HYBRID]    → Roles requiring both research credentials AND coding/engineering

This classification governs which scoring rules apply below.

════════════════════════════════════════════════════════
SECTION B — PREFERRED vs MANDATORY CLASSIFICATION
════════════════════════════════════════════════════════
Before auditing any skill, tag each JD requirement as MANDATORY or PREFERRED:
  MANDATORY → "required", "must have", "essential", "you will need", deal_breakers list
  PREFERRED → "preferred", "is preferred", "nice to have", "a plus", "ideally", "bonus"

Apply deductions accordingly — NEVER penalise a PREFERRED gap at mandatory rates.

Deduction rates by classification:
  MANDATORY missing  → full deduction (see Section C)
  MANDATORY partial  → half deduction
  PREFERRED missing  → -4 max
  PREFERRED partial  → -2 max

════════════════════════════════════════════════════════
SECTION C — SKILL EVIDENCE AUDIT (Step 1)
════════════════════════════════════════════════════════
Go through EVERY item in mandatory_technologies and required_skills ONE BY ONE.
Classify each as:
  'present' → explicitly demonstrated with context, project use, or role evidence
  'partial' → adjacent/equivalent tech, mentioned without depth, or strongly implied
              by candidate's role/stack (e.g. a backend engineer using AWS likely
              used S3/EC2 → partial, not missing)
  'missing' → genuinely not found anywhere in the resume

AMBIGUITY RULE: When evidence is unclear, default to 'partial', never 'missing'.
Record a brief evidence quote or note for every single item.

[RESEARCH ROLE SPECIFIC AUDIT RULES]
  - Chemistry subfields (photochemistry, computational, organic, physical, theoretical)
    are ADJACENT subdisciplines — not different domains. A photochemist using
    Gaussian/GaussView/DFT tools = strong partial match for Computational Chemistry.
  - Computational tools found in resume (Gaussian, GaussView, ORCA, VASP, NAMD,
    Materials Studio, DFT, MD simulations) = 'present' for computational proficiency.
  - Prior work at the hiring company in a related capacity = 'present' for domain fit.
  - Do NOT penalise subfield mismatch (e.g. photochemistry vs computational chemistry)
    the same way you would penalise a biology PhD applying for a pure chemistry role.

════════════════════════════════════════════════════════
SECTION D — SUB-SCORES
════════════════════════════════════════════════════════

──────────────────────────────────────────────────────
D1. TECHNICAL MATCH (0–100)
──────────────────────────────────────────────────────
Start at 100.

[TECH / HYBRID ROLES]
  'missing' MANDATORY technology:  -12 points
  'partial' MANDATORY technology:  -5 points
  Strong equivalent technology:    restore up to +6
  'missing' PREFERRED technology:  -4 points max
  'missing' deal_breaker item:     additional -15 (on top of above)
  2+ deal_breakers missing:        additional -20 total (NOT per item)

[RESEARCH ROLES]
  'missing' MANDATORY credential (e.g. PhD, publications): -20 points
  'partial' MANDATORY credential:                          -8 points
  'missing' PREFERRED tool/skill (e.g. Python preferred):  -4 points max
  'missing' deal_breaker:                                  -15 points
  Computational tools present (Gaussian, GaussView, etc.): +8 bonus if role
    requires computational chemistry and tools are found in resume

Floor at 0. Cap at 100. Never deduct the same gap twice.

──────────────────────────────────────────────────────
D2. EXPERIENCE MATCH (0–100)
──────────────────────────────────────────────────────
[TECH / HYBRID ROLES]
  Under minimum_years by 1:    -8
  Under minimum_years by 2:    -18
  Under minimum_years by 3+:   -28 max
  Team lead / architecture ownership:          +8
  Measurable outcomes ("scaled to 1M users"):  +5
  Junior-only signals, no growth arc:          -8

[RESEARCH ROLES — use this block instead of above]
  PhD from any recognised university:          base of 75 experience score
  PhD from top-tier institution
    (IIT, IISc, MIT, Stanford, Oxford, ETH):   base of 82
  Each peer-reviewed publication:              +3 (max +10 total)
  Prestigious fellowship (PMRF, INSPIRE,
    NSF, Marie Curie, Rhodes, etc.):           +5
  Prior work at the hiring company
    in a related role:                         +8
  Active research in directly relevant area:   +5
  No publications at all (for roles requiring
    at least 1 peer-reviewed paper):           -20

──────────────────────────────────────────────────────
D3. DOMAIN MATCH (0–100)
──────────────────────────────────────────────────────
[TECH / HYBRID ROLES]
  Exact domain match:                          85–95
  Adjacent domain (fintech → banking):         65–80
  Transferable domain (different industry,
    same technical patterns):                  50–65
  Unrelated domain:                            35–50

[RESEARCH ROLES — use this block instead]
  PhD subfield directly matches JD field:      88–95
  PhD in adjacent subfield of same discipline
    (e.g. photochemistry → comp. chemistry):   72–84
  PhD in same broad discipline, different
    subfield (e.g. organic → computational):   62–74
  PhD in a different but related discipline
    (e.g. physics → chemistry):               50–62
  PhD in unrelated field:                      30–45
  Prior work at hiring company in related
    role (e.g. LLM trainer → research spec.):  +8 bonus, cap at 95

════════════════════════════════════════════════════════
SECTION E — FINAL SCORE FORMULA
════════════════════════════════════════════════════════
match_percentage = round((technical_match * 0.50) + (experience_match * 0.30) + (domain_match * 0.20))

Compute this literally. Do NOT round to a multiple of 5.
Run a sanity check against the calibration table below before finalising.

════════════════════════════════════════════════════════
SECTION F — DEAL BREAKER PENALTY
════════════════════════════════════════════════════════
If a deal_breaker item is 'missing':
  Apply -15 to technical_match (in addition to Step D1 deduction).
If 2 or more deal_breakers are missing:
  Apply -20 total additional (NOT cumulative per item).
Do NOT double-count: if an item is already penalised in D1, the deal_breaker
surcharge is the ADDITIONAL amount only.

════════════════════════════════════════════════════════
SECTION G — CALIBRATION TABLE (sanity check before output)
════════════════════════════════════════════════════════
Use this table to verify your scores make sense. If you land significantly
outside the relevant range, re-examine your partial/missing classifications.

TECH ROLES:
  All mandatory tech present, right seniority, same domain          → 82–91%
  1–2 missing mandatory techs, right YoE, adjacent domain          → 63–74%
  3 missing mandatory techs, 1 yr short on YoE, adjacent domain    → 48–58%
  4+ missing mandatory techs, significant YoE gap                  → 28–44%
  Strong tech match, completely wrong domain                        → 55–68%
  Great domain + YoE but 2 deal-breakers missing                   → 42–54%

RESEARCH ROLES:
  PhD matches field exactly, 2+ publications, prior Turing work     → 83–92%
  PhD in adjacent subfield, 1–2 publications, comp tools present   → 74–84%
  PhD in adjacent subfield, no publications                        → 52–62%
  No PhD but strong relevant experience + publications             → 55–68%
  PhD in unrelated field, no publications                          → 28–42%

════════════════════════════════════════════════════════
SECTION H — ANTI-INFLATION AND ANTI-DEFLATION RULES
════════════════════════════════════════════════════════
DO NOT:
  - Give full credit for skills only listed in a skills section with no supporting
    project or role context (for tech roles)
  - Inflate because a candidate "seems impressive overall"
  - Penalise the same gap twice across different sub-scores
  - Default ambiguous evidence to 'missing' — ambiguous = 'partial'
  - Treat chemistry subdiscipline mismatch as harshly as a full domain mismatch
  - Apply mandatory-level deductions to preferred/nice-to-have gaps
  - Produce round multiples of 5 — compute from the formula literally

DO:
  - Distinguish "can't do this role" gaps from "will learn in 2 weeks" gaps
  - Give genuine credit for equivalent or adjacent technologies
  - For research roles, weight credentials and publications heavily
  - Classify role type in Section A before doing anything else
  - Check your final score against Section G before outputting"""

        deal_breakers_str = ', '.join(jd_data.deal_breakers) if jd_data.deal_breakers else 'None specified'
        nice_to_have_str = ', '.join(jd_data.nice_to_have_technologies) if jd_data.nice_to_have_technologies else 'None'

        user_prompt = f"""## JOB REQUIREMENTS

**Mandatory Technologies (audit each one):** {', '.join(jd_data.mandatory_technologies)}
**Required Skills:** {', '.join(jd_data.required_skills)}
**Deal Breakers (penalise heavily if missing — see methodology):** {deal_breakers_str}
**Experience Required:** {jd_data.experience_requirements}
**Minimum Years:** {jd_data.minimum_years}
**Seniority:** {jd_data.seniority_expectations}
**Domain:** {jd_data.domain_requirements}
**Nice-to-Have Tech:** {nice_to_have_str}
**Education:** {jd_data.education_requirements}

## CANDIDATE
- **Extracted YoE:** {candidate_data.get('YearsOfExperience')} years

## RESUME (preprocessed)
```
{resume_text}
```

Now execute the 4-step methodology above. Be strict, evidence-based, and compute the formula literally."""

        completion = client.beta.chat.completions.parse(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format=MatchResult,
            timeout=120
        )
        parsed = completion.choices[0].message.parsed

        # Build skill evidence summary for display
        evidence_lines = [
            f"[{e.status.upper()}] {e.skill}: {e.evidence}"
            for e in parsed.skill_evidence
        ] if parsed.skill_evidence else []

        return {
            "MatchPercentage": parsed.match_percentage,
            "TechnicalMatch": parsed.technical_match,
            "ExperienceMatch": parsed.experience_match,
            "DomainMatch": parsed.domain_match,
            "Strengths": parsed.strengths,
            "Weaknesses": parsed.weaknesses,
            "MissingRequirements": parsed.missing_requirements,
            "Summary": parsed.summary,
            "SkillEvidence": evidence_lines,
        }
    except Exception as e:
        print(f"Match Evaluation Error: {e}")
        return {
            "MatchPercentage": "Not evaluated", "TechnicalMatch": None, "ExperienceMatch": None, "DomainMatch": None,
            "Strengths": [], "Weaknesses": [], "MissingRequirements": [],
            "Summary": f"Evaluation Failed: {e}", "SkillEvidence": []
        }


def extract_data_with_ai(text, filename, client, model=LUNA_MODEL):
    if not text:
        return {"Name": "Read Error", "Email": "Read Error", "Phone": "Read Error", "Country": "Read Error", "YearsOfExperience": "Read Error", "SourceFilename": filename}

    try:
        prompt = f"Extract information from the resume text below.\n- Full name\n- Email address\n- Single phone number (no country code)\n- Country\n- Total years of professional work experience\n- Source filename '{filename}'\n\nResume Text:\n---\n{text[:15000]}\n---"
        completion = client.beta.chat.completions.parse(
            model=model,
            messages=[
                {"role": "system", "content": "You are a highly accurate resume data extraction assistant."},
                {"role": "user", "content": prompt}
            ],
            response_format=ResumeData,
            timeout=60
        )
        extracted = completion.choices[0].message.parsed
        yoe_rounded = str(round(extracted.years_of_experience)) if extracted.years_of_experience is not None else "Not Found"
        phone = extracted.phone_number

        return {
            "Name": extracted.full_name,
            "Email": extracted.email_address,
            "Phone": phone,
            "Country": extracted.country,
            "YearsOfExperience": yoe_rounded,
            "SourceFilename": extracted.source_filename
        }
    except Exception as e:
        print(f"API Error for {filename}: {e}")
        return {"Name": "API Error", "Email": "API Error", "Phone": "API Error", "Country": "API Error", "YearsOfExperience": "API Error", "SourceFilename": filename}


PREFERENCES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preferences.json")
REVIEW_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_state.json")

RESUME_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resume_cache.json")
JD_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jd_cache.json")


MATCH_PROMPT_VERSION = 1  # bump when the Stage-2 match prompt/schema changes (invalidates cached matches)
EXTRACT_VERSION = 3  # preserve deterministic fields and retry failed AI extraction


def match_cache_key(file_hash: str, jd_hash: str, model: str) -> str:
    """Key for a cached Stage-2 match result (resume content x JD x model x prompt version)."""
    return f"{file_hash}:{jd_hash}:{model}:v{MATCH_PROMPT_VERSION}"

LLM_CONCURRENCY = 32
UPLOAD_WORKERS = 4            # parallel Drive upload threads, each with its own service
DRIVE_CACHE_SAVE_EVERY = 10   # persist drive_cache.json after this many new uploads (and on drain)
INFLIGHT_UPLOAD_WAIT_S = 300  # max wait for an in-flight upload of the same content (hash)
DRIVE_AUTH_FAILED_MSG = ("Google Drive sign-in failed earlier in this run; "
                         "skipped upload (use Retry Failed to sign in again)")
STAGE2_TOP_N = 15             # always deep-evaluate the top N Stage-1 candidates...
STAGE2_THRESHOLD = 0.35       # ...plus any candidate whose Stage-1 score reaches this
CACHE_CHECKPOINT_EVERY = 25   # write resume_cache.json every N completed match tasks

# Matching progress bar: each pipeline phase owns a fixed slice of [0, 1]
PROGRESS_PHASES = {"read": (0.0, 0.20), "embed": (0.20, 0.30), "extract": (0.30, 0.40), "match": (0.40, 1.00)}


def phase_progress(phase: str, done: int, total: int) -> float:
    """Overall progress for `done` of `total` items in `phase` (phase start when total == 0)."""
    lo, hi = PROGRESS_PHASES[phase]
    if total <= 0:
        return lo
    return lo + (hi - lo) * (min(done, total) / total)


def format_skipped_files(skipped: List[Tuple[str, str]], limit: int = 20) -> str:
    """Skipped-files block for the completion popup: header plus at most `limit`
    "filename\nreason" entries; the full list is in processing_summary.csv."""
    if not skipped:
        return ""
    text = "Skipped Files\n"
    for fname, reason in skipped[:limit]:
        text += f"{fname}\n{reason}\n"
    if len(skipped) > limit:
        text += f"\u2026and {len(skipped) - limit} more (see processing_summary.csv)\n"
    return text


MAX_RETRY_AFTER = 60.0

def retry_delay(base_delay: float, exc: Exception) -> float:
    """Jittered backoff delay, raised to the server's Retry-After (seconds) when present."""
    jittered = base_delay * random.uniform(0.5, 1.5)
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        try:
            retry_after = float(headers.get("retry-after"))
        except (TypeError, ValueError, AttributeError):
            retry_after = None
        if retry_after is not None and math.isfinite(retry_after):
            return max(jittered, min(retry_after, MAX_RETRY_AFTER))
    return jittered

async def call_openai_with_retry(func, *args, semaphore: Optional[asyncio.Semaphore] = None, **kwargs):
    retries = 5
    delay = 1.0
    for i in range(retries):
        try:
            if semaphore is not None:
                async with semaphore:
                    return await func(*args, **kwargs)
            return await func(*args, **kwargs)
        except Exception as e:
            # Authentication, permissions and malformed requests cannot recover
            # through backoff. Retain retries for rate limits and server errors.
            status = getattr(e, "status_code", None)
            if isinstance(status, int) and 400 <= status < 500 and status not in (408, 409, 429):
                raise
            if i == retries - 1:
                raise e
            wait = retry_delay(delay, e)
            print(f"OpenAI call failed (attempt {i+1}): {e}. Retrying in {wait:.1f}s...")
            await asyncio.sleep(wait)
            delay *= 2.0

async def get_embeddings_batch(texts: List[str], async_client, semaphore: Optional[asyncio.Semaphore] = None) -> List[List[float]]:
    try:
        async def call_api():
            response = await async_client.embeddings.create(
                model="text-embedding-3-small",
                input=texts
            )
            return [item.embedding for item in response.data]
        return await call_openai_with_retry(call_api, semaphore=semaphore)
    except Exception as e:
        print(f"Batch embedding error: {e}")
        return [[] for _ in texts]

def parse_resume_sections(text: str) -> dict:
    sections = {
        "SKILLS": "",
        "EXPERIENCE": "",
        "PROJECTS": "",
        "EDUCATION": "",
        "CERTIFICATIONS": "",
        "SUMMARY": "",
        "FULL_TEXT": text
    }
    lines = text.split('\n')
    current_section = "SUMMARY"
    section_lines = {k: [] for k in sections.keys()}
    
    for line in lines:
        stripped = line.strip()
        match = re.match(r'^### \[(SKILLS|EXPERIENCE|PROJECTS|EDUCATION|CERTIFICATIONS|SUMMARY)\] ###$', stripped)
        if match:
            current_section = match.group(1)
        else:
            section_lines[current_section].append(line)
            
    for k, v_lines in section_lines.items():
        sections[k] = '\n'.join(v_lines).strip()
    return sections

def compute_skills_overlap(jd_skills: List[str], candidate_skills_text: str) -> float:
    if not jd_skills or not candidate_skills_text:
        return 0.0
    candidate_words = set(re.findall(r'\w+', candidate_skills_text.lower()))
    matches = 0
    for s in jd_skills:
        s_clean = s.lower().strip()
        if s_clean in candidate_words or any(s_clean in cw for cw in candidate_words):
            matches += 1
    return matches / len(jd_skills)

def deterministic_extract(text: str, filename: str) -> dict:
    email_match = re.search(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', text)
    email = email_match.group(0) if email_match else "Not Found"
    
    phone_match = re.search(r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', text)
    phone = phone_match.group(0) if phone_match else "Not Found"
    
    lines = [line.strip() for line in text.split('\n') if line.strip() and not line.strip().startswith('###')]
    name = "Not Found"
    for line in lines[:5]:
        words = line.split()
        if 2 <= len(words) <= 3 and all(w.isalpha() for w in words):
            name = line
            break
            
    yoe = "Not Found"
    yoe_match = re.search(r'(\d+(?:\.\d+)?)\+?\s*years?\s*(?:of\s*)?experience', text, re.IGNORECASE)
    if yoe_match:
        try:
            yoe = str(round(float(yoe_match.group(1))))
        except Exception:
            pass
            
    return {
        "Name": name,
        "Email": email,
        "Phone": phone,
        "Country": "Not Found",
        "YearsOfExperience": yoe,
        "SourceFilename": filename
    }

async def extract_data_with_ai_async(text, filename, client, model=LUNA_MODEL, semaphore=None):
    if not text:
        return {"Name": "Read Error", "Email": "Read Error", "Phone": "Read Error", "Country": "Read Error", "YearsOfExperience": "Read Error", "SourceFilename": filename}

    async def call_api():
        prompt = f"Extract information from the resume text below.\n- Full name\n- Email address\n- Single phone number (no country code)\n- Country\n- Total years of professional work experience\n- Source filename '{filename}'\n\nResume Text:\n---\n{text[:15000]}\n---"
        completion = await client.beta.chat.completions.parse(
            model=model,
            messages=[
                {"role": "system", "content": "You are a highly accurate resume data extraction assistant."},
                {"role": "user", "content": prompt}
            ],
            response_format=ResumeData,
            timeout=60
        )
        extracted = completion.choices[0].message.parsed
        yoe_rounded = str(round(extracted.years_of_experience)) if extracted.years_of_experience is not None else "Not Found"
        phone = extracted.phone_number
        return {
            "Name": extracted.full_name,
            "Email": extracted.email_address,
            "Phone": phone,
            "Country": extracted.country,
            "YearsOfExperience": yoe_rounded,
            "SourceFilename": extracted.source_filename
        }

    try:
        return await call_openai_with_retry(call_api, semaphore=semaphore)
    except Exception as e:
        print(f"API Error for {filename}: {e}")
        message = {
            401: "OpenAI rejected the API key. Enter a valid key and retry.",
            403: "OpenAI denied access. Check the key's project and model permissions.",
            404: "The selected model is unavailable. Choose an accessible model and retry.",
            429: "OpenAI quota or rate limit reached. Check billing or retry later.",
        }.get(getattr(e, "status_code", None), "OpenAI request failed. Check your connection and retry.")
        return {"Name": "API Error", "Email": "API Error", "Phone": "API Error", "Country": "API Error", "YearsOfExperience": "API Error", "SourceFilename": filename,
                "ExtractionError": message}

async def evaluate_candidate_match_async(candidate_profile, jd_data, candidate_data, client, model, semaphore=None):
    try:
        deal_breakers_str = ', '.join(jd_data.deal_breakers) if jd_data.deal_breakers else 'None specified'
        nice_to_have_str = ', '.join(jd_data.nice_to_have_technologies) if jd_data.nice_to_have_technologies else 'None'
        
        system_prompt = "You are a precision recruiter scoring candidate profiles against job requirements."
        
        user_prompt = f"""Evaluate candidate match based on job requirements.

## JOB REQUIREMENTS
Mandatory Technologies: {', '.join(jd_data.mandatory_technologies)}
Required Skills: {', '.join(jd_data.required_skills)}
Deal Breakers: {deal_breakers_str}
Experience Required: {jd_data.experience_requirements}
Minimum Years: {jd_data.minimum_years}
Seniority: {jd_data.seniority_expectations}
Domain: {jd_data.domain_requirements}
Nice-to-Have Tech: {nice_to_have_str}
Education: {jd_data.education_requirements}

## CANDIDATE PROFILE
{candidate_profile}

Execute the 4-step scoring methodology and return JSON."""

        async def call_api():
            completion = await client.beta.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format=MatchResult,
                timeout=60
            )
            return completion.choices[0].message.parsed

        parsed = await call_openai_with_retry(call_api, semaphore=semaphore)

        evidence_lines = [
            f"[{e.status.upper()}] {e.skill}: {e.evidence}"
            for e in parsed.skill_evidence
        ] if parsed.skill_evidence else []

        return {
            "MatchPercentage": parsed.match_percentage,
            "TechnicalMatch": parsed.technical_match,
            "ExperienceMatch": parsed.experience_match,
            "DomainMatch": parsed.domain_match,
            "Strengths": parsed.strengths,
            "Weaknesses": parsed.weaknesses,
            "MissingRequirements": parsed.missing_requirements,
            "Summary": parsed.summary,
            "SkillEvidence": evidence_lines,
        }
    except Exception as e:
        print(f"Match Evaluation Error: {e}")
        return {
            "MatchPercentage": "Not evaluated", "TechnicalMatch": None, "ExperienceMatch": None, "DomainMatch": None,
            "Strengths": [], "Weaknesses": [], "MissingRequirements": [],
            "Summary": f"Evaluation Failed: {e}", "SkillEvidence": []
        }


def discover_resume_files(folder, recursive=False):
    """Read-only folder discovery shared by preview and explicit processing."""
    supported, skipped = [], []
    if recursive:
        filenames = [os.path.relpath(os.path.join(root, name), folder)
                     for root, dirs, names in os.walk(folder, followlinks=False)
                     for name in names]
    else:
        filenames = os.listdir(folder)
    for filename in sorted(filenames):
        if not os.path.isfile(os.path.join(folder, filename)):
            continue
        if os.path.basename(filename).startswith('~$') or os.path.basename(filename) == 'processing_summary.csv':
            continue
        if os.path.splitext(filename)[1].lower() in ('.pdf', '.docx', '.doc', '.txt'):
            supported.append(filename)
        else:
            skipped.append((filename, "Unsupported file format"))
    return supported, skipped


# ── UI tokens ───────────────────────────────────────────────────────────────
# One place for the dark palette so every control uses the same few colours.
ACCENT = "#1F6AA5"
ACCENT_HOVER = "#185A8E"
SECONDARY = "#343A43"
SECONDARY_HOVER = "#414955"
TEXT = "#E6EAF0"
MUTED_TEXT = "#B8C0CC"
HINT_TEXT = "#9AA4B2"
LINK_TEXT = "#7DB4E6"
WARNING_TEXT = "#E5B567"
DANGER_TEXT = "#F09A9A"
TABLE_BG = "#242830"
HEADING_BG = "#303640"
HEADING_ACTIVE = "#3A424E"
HEADING_TEXT = "#CDD4DF"
SELECTED_ROW = "#1F538D"
SCROLL_THUMB = "#47505C"
SCROLL_THUMB_HOVER = "#657184"

COMPACT_COLUMNS = ("Name", "MatchPct", "YearsExp", "Country", "ResumeLink")
SCORE_FILTERS = {"All": "0", "50+": "50", "70+": "70", "85+": "85"}
def shortcut_label(action, platform=None):
    """Display text for a keyboard shortcut in the platform's own notation."""
    mac = (platform or sys.platform) == "darwin"
    labels = {"open": ("⌘O", "Ctrl+O"), "process": ("⌘↩", "Ctrl+Enter"), "export": ("⌘E", "Ctrl+E"),
              "search": ("⌘F", "Ctrl+F"), "settings": ("⌘,", "Ctrl+,"), "fullscreen": ("⌃⌘F", "F11")}
    return labels[action][0 if mac else 1]
REVEAL_LABEL = "Show in Finder" if sys.platform == "darwin" else "Open folder"


def secondary_button(parent, **kwargs):
    """Neutral button used for every non-primary action."""
    kwargs.setdefault("height", 36)
    return ctk.CTkButton(parent, fg_color=SECONDARY, hover_color=SECONDARY_HOVER, **kwargs)


def link_button(parent, **kwargs):
    """Text-weight action that sits inside a line of text or a header."""
    kwargs.setdefault("height", 28)
    kwargs.setdefault("width", 0)
    kwargs.setdefault("text_color", LINK_TEXT)
    return ctk.CTkButton(parent, fg_color="transparent", hover_color=SECONDARY,
                         text_color_disabled=HINT_TEXT, **kwargs)


def upload_status_display(status):
    """Table text and row tag for an upload status; icons carry meaning without colour."""
    status = str(status or "")
    if status == "Uploaded":
        return "✓ Uploaded", ()
    if status == "Uploading":
        return "↑ Uploading", ()
    if status.startswith("Pending"):
        return "• Pending", ()
    if status == "Failed":
        return "✕ Failed", ("problem",)
    if status == "Cancelled":
        return "– Cancelled", ("muted",)
    return status, ()


def as_percent(value):
    """A score as a float, or None when it is missing or not numeric (e.g. 'N/A')."""
    if isinstance(value, bool):
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


MATCH_TIERS = ((75, "Strong", "#2E7D4F"), (50, "Partial", "#8A6A1F"), (0, "Weak", "#8B3A3A"))


def match_tier(score):
    """(word, badge colour) for a 0-100 score; white text meets 4.5:1 on each colour."""
    for floor, word, colour in MATCH_TIERS:
        if score >= floor:
            return word, colour
    return MATCH_TIERS[-1][1:]


def format_phone_display(phone):
    """Group a stored digits-only phone for reading; anything else is shown as stored."""
    digits = str(phone or "")
    if not digits.isdigit():
        return digits
    if len(digits) == 10:
        return f"{digits[:3]} {digits[3:6]} {digits[6:]}"
    if len(digits) == 11 and digits[0] == "1":
        return f"+1 {digits[1:4]} {digits[4:7]} {digits[7:]}"
    return digits


def match_display(value):
    """Table text for a match score: "86%", or an em dash when there is no score.
    Why a score is missing is listed under Needs attention and in the details."""
    score = as_percent(value)
    return "—" if score is None else f"{score:.0f}%"


def run_progress(match_progress, finished_uploads, total_uploads, pipeline_running, run_started):
    """Overall run progress for the single bar: half processing, half uploads."""
    if not run_started:
        return 0.0
    if total_uploads:
        uploads = finished_uploads / total_uploads
    else:
        uploads = 0.0 if pipeline_running else 1.0
    return max(0.0, min(1.0, 0.5 * match_progress + 0.5 * uploads))


def reveal_in_file_manager(path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("win"):
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path])



class ExtractorApp(ctk.CTk):
    def __init__(self):
        self._preference_store = PreferenceStore(PREFERENCES_FILE)
        self.preferences = self._preference_store.load()
        self._review_store = ReviewStore(REVIEW_FILE)
        self._secret_store = SecureApiKeyStore()
        ctk.set_widget_scaling(self.preferences['text_scale'])
        super().__init__()
        self.title("Bulk Upload Assistant")
        self.center_window(1440, 900)
        self.minsize(1200, 700)
        if self.preferences['remember_workspace'] and self.preferences['window_geometry']:
            try: self.geometry(self.preferences['window_geometry'])
            except tk.TclError: pass

        # A PNG works for the window and Dock on every platform; an .ico, if present, suits Windows.
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            self._icon = tk.PhotoImage(file=os.path.join(here, "app_icon.png"))
            self.iconphoto(True, self._icon)
        except (tk.TclError, OSError):
            pass
        if sys.platform.startswith("win") and os.path.exists(os.path.join(here, "app_icon.ico")):
            try:
                self.wm_iconbitmap(os.path.join(here, "app_icon.ico"))
            except tk.TclError:
                pass

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        self._details_window = None
        self._batch_windows = []
        self._settings_open = False
        self._jd_open = True
        self._activity_open = False

        # A quiet header keeps configuration out of the working area.
        self.frame_header = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_header.grid(row=0, column=0, padx=20, pady=(14, 8), sticky="ew")
        self.lbl_title = ctk.CTkLabel(self.frame_header, text="Bulk Upload Assistant",
                                    font=ctk.CTkFont(size=18, weight="bold"))
        self.lbl_title.pack(side="left")
        self.btn_settings = secondary_button(self.frame_header, text="Settings", width=120,
                                             command=self.toggle_settings)
        self.btn_settings.pack(side="right")
        # Shown only while the key is missing; Settings keeps its place either way.
        self.btn_key_chip = ctk.CTkButton(self.frame_header, text="API key needed", width=120, height=36,
            corner_radius=6, fg_color="transparent", border_width=1, border_color=WARNING_TEXT,
            text_color=WARNING_TEXT, hover_color=SECONDARY, command=self.toggle_settings)
        self.btn_key_chip.pack(side="right", padx=12)

        self.frame_controls = ctk.CTkFrame(self)
        self.frame_controls.grid(row=1, column=0, padx=20, pady=(0, 8), sticky="ew")
        self.frame_controls.grid_columnconfigure(1, weight=1)
        self.lbl_dir = ctk.CTkLabel(self.frame_controls, text="Resume folder", font=ctk.CTkFont(size=14))
        self.lbl_dir.grid(row=0, column=0, padx=16, pady=12, sticky="w")
        self.sv_dir = tk.StringVar(value=os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads"))
        self.entry_dir = ctk.CTkEntry(self.frame_controls, textvariable=self.sv_dir, height=36)
        self.entry_dir.grid(row=0, column=1, padx=(0, 8), pady=12, sticky="ew")
        self.btn_browse = secondary_button(self.frame_controls, text="Choose folder", width=120,
                                           command=self.browse_dir)
        self.btn_browse.grid(row=0, column=2, padx=(0, 16), pady=12)

        self.sv_api_key = tk.StringVar()
        self.sv_model = tk.StringVar(value=self.preferences['model'])
        if self.preferences['remember_api_key']:
            try: self.sv_api_key.set(self._secret_store.get() or '')
            except Exception: pass  # The key can still be entered for this session.
        self._settings_window = self._make_settings_window()
        self.entry_api = self._settings_window.api_entry
        self.combo_model = self._settings_window.model_menu
        if self.preferences['reopen_last_folder'] and self.preferences['last_folder']:
            self.sv_dir.set(self.preferences['last_folder'])

        # Job setup collapses to a one-line summary without changing the results area's width.
        self.frame_jd = ctk.CTkFrame(self)
        self.frame_jd.grid(row=2, column=0, padx=20, pady=(0, 8), sticky="ew")
        self.frame_jd.grid_columnconfigure(0, weight=1)
        self.frame_jd_header = ctk.CTkFrame(self.frame_jd, fg_color="transparent")
        self.frame_jd_header.grid(row=0, column=0, padx=16, pady=8, sticky="ew")
        self.frame_jd_header.grid_columnconfigure(1, weight=1)
        self.bv_jd_matching = tk.BooleanVar(value=self.preferences['jd_matching_default'])
        self.chk_jd_matching = ctk.CTkCheckBox(self.frame_jd_header,
            text="Match against a job description", variable=self.bv_jd_matching,
            command=self.on_jd_matching_toggled)
        self.chk_jd_matching.grid(row=0, column=0, sticky="w")
        # While processing, the box folds to this summary; clicking it opens the box again.
        self.lbl_jd_summary = ctk.CTkLabel(self.frame_jd_header, text="", text_color=MUTED_TEXT, anchor="w")
        self.lbl_jd_summary.grid(row=0, column=1, sticky="ew", padx=12)
        self.lbl_jd_summary.bind("<Button-1>", lambda event: self._open_jd())
        self.btn_upload_jd = link_button(self.frame_jd_header, text="Choose JD file…", command=self.upload_jd)
        self.btn_upload_jd.grid(row=0, column=2, sticky="e")
        # The box opens and closes by animating a clipping frame's height; the body
        # keeps its natural size inside it, so nothing reflows mid-animation.
        jd_background = self.frame_jd._apply_appearance_mode(self.frame_jd.cget("fg_color"))
        self.frame_jd_clip = tk.Frame(self.frame_jd, height=0, bd=0, highlightthickness=0, bg=jd_background)
        self.frame_jd_clip.grid(row=1, column=0, sticky="ew")
        self.frame_jd_clip.grid_propagate(False)
        self.frame_jd_body = ctk.CTkFrame(self.frame_jd_clip, fg_color="transparent")
        self.frame_jd_body.grid_columnconfigure(0, weight=1)
        self.frame_jd_body.bind("<Configure>", lambda event: self._fit_jd_clip(), add="+")
        self.txt_jd = ctk.CTkTextbox(self.frame_jd_body, wrap="word", height=88, font=ctk.CTkFont(size=14))
        self.txt_jd.grid(row=0, column=0, sticky="ew", padx=16)
        # CTkTextbox has no placeholder; an overlaid hint disappears on focus or text.
        self.lbl_jd_hint = ctk.CTkLabel(self.txt_jd, text="Paste the job description here, or choose a file.",
            font=ctk.CTkFont(size=14, slant="italic"), text_color=HINT_TEXT,
            fg_color=self.txt_jd.cget("fg_color"), height=20, anchor="w")
        self.lbl_jd_hint.place(x=10, y=8)
        self.lbl_jd_hint.bind("<Button-1>", lambda event: self.txt_jd.focus_set())
        self.txt_jd.bind("<FocusIn>", lambda event: self._update_jd_hint(), add="+")
        self.txt_jd.bind("<FocusOut>", lambda event: self._update_jd_hint(), add="+")
        self.txt_jd.bind("<KeyRelease>", lambda event: self._on_jd_edited(), add="+")
        self.txt_jd.bind("<<Paste>>", lambda event: self.after_idle(self._on_jd_edited), add="+")
        # Text inserted by code (a JD file, a cleared batch) must hide the hint too.
        def on_modified(event):
            self.txt_jd._textbox.edit_modified(False)
            self._update_jd_hint()
        self.txt_jd._textbox.bind("<<Modified>>", on_modified, add="+")
        self.lbl_jd_error = ctk.CTkLabel(self.frame_jd_body, text="Add a job description, or untick matching "
            "to process resumes without scores.", text_color=WARNING_TEXT, anchor="w", height=20)
        self._jd_error_shown = False

        self.frame_table_container = ctk.CTkFrame(self)
        self.frame_table_container.grid(row=3, column=0, padx=20, pady=(0, 8), sticky="nsew")
        self.frame_table_container.grid_rowconfigure(1, weight=1)
        self.frame_table_container.grid_columnconfigure(0, weight=1)
        self.frame_filters = ctk.CTkFrame(self.frame_table_container, fg_color="transparent")
        self.frame_filters.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=10)
        self.sv_search = tk.StringVar()
        self.sv_search.trace_add("write", self._on_search_changed)
        self.entry_search = ctk.CTkEntry(self.frame_filters, textvariable=self.sv_search,
                                        width=320, height=36, font=ctk.CTkFont(size=14))
        self.entry_search.pack(side="left", padx=(0, 16))
        # CTkEntry suppresses its built-in placeholder when a StringVar is bound.
        # A separate hint leaves the search value empty and typed text upright.
        self.lbl_search_hint = ctk.CTkLabel(self.entry_search, text="Search candidates",
            font=ctk.CTkFont(size=14, slant="italic"), text_color=HINT_TEXT,
            fg_color=self.entry_search.cget("fg_color"), height=20, anchor="w")
        self.lbl_search_hint.place(x=12, rely=0.5, anchor="w")
        self.lbl_search_hint.bind("<Button-1>", lambda event: self.entry_search.focus_set())
        self.entry_search.bind("<FocusIn>", lambda event: self.lbl_search_hint.place_forget())
        self.entry_search.bind("<FocusOut>", lambda event: self._update_search_hint())
        # Preset thresholds instead of free text: no invalid values, one click to change.
        self.frame_min_score = ctk.CTkFrame(self.frame_filters, fg_color="transparent")
        self.frame_min_score.pack(side="left")
        self.lbl_min_score = ctk.CTkLabel(self.frame_min_score, text="Match")
        self.lbl_min_score.pack(side="left", padx=(0, 8))
        self.sv_min_score = tk.StringVar(value="0")
        self.sv_min_score.trace_add("write", lambda *args: self.filter_table())
        self.seg_min_score = ctk.CTkSegmentedButton(self.frame_min_score, values=list(SCORE_FILTERS),
                                                    height=36, command=self._on_score_filter)
        self.seg_min_score.set("All")
        self.seg_min_score.pack(side="left")
        self.btn_details = secondary_button(self.frame_filters, text="View details", width=110,
            state="disabled", command=lambda: self.on_row_double_click(None))
        self.btn_details.pack(side="right", padx=(12, 0))
        self.bv_all_columns = tk.BooleanVar(value=False)
        self.chk_all_columns = ctk.CTkCheckBox(self.frame_filters, text="All columns", width=100,
            variable=self.bv_all_columns, command=self.toggle_table_columns)
        self.chk_all_columns.pack(side="right", padx=(16, 0))

        style = ttk.Style()
        style.theme_use("default")
        table_font = UI_FONT_FAMILY
        style.configure("Treeview", background=TABLE_BG, foreground=TEXT, rowheight=36,
            fieldbackground=TABLE_BG, borderwidth=0, font=(table_font, 12))
        style.map("Treeview", background=[("selected", SELECTED_ROW)])
        style.configure("Treeview.Heading", background=HEADING_BG, foreground=HEADING_TEXT,
            relief="flat", font=(table_font, 12, "bold"), padding=8)
        style.map("Treeview.Heading", background=[("active", HEADING_ACTIVE)])
        columns = ("Name", "Status", "Email", "Phone", "Country", "YearsExp", "MatchPct", "MissingSkills", "ResumeLink")
        self.tree = ttk.Treeview(self.frame_table_container, columns=columns, show="headings",
            displaycolumns=COMPACT_COLUMNS)
        self._headings = {"Name": "Name", "Status": "Processing status", "Email": "Email", "Phone": "Phone",
                          "Country": "Country", "YearsExp": "Experience", "MatchPct": "Match %",
                          "MissingSkills": "Missing requirements", "ResumeLink": "Upload status"}
        widths = {"Name": 260, "Status": 200, "Email": 240, "Phone": 150, "Country": 180,
                  "YearsExp": 110, "MatchPct": 100, "MissingSkills": 260, "ResumeLink": 180}
        for col in columns:
            # Headings align with their values: text left, numbers right.
            anchor = {"MatchPct": "e", "YearsExp": "center"}.get(col, "w")
            self.tree.heading(col, text=self._headings[col], anchor=anchor,
                              command=lambda c=col: self.sort_table(c, c in ("YearsExp", "MatchPct")))
            self.tree.column(col, width=widths[col], minwidth=90, anchor=anchor)
        # ttk cannot colour one cell, and a red row reads as a rejected candidate.
        # Problem rows keep normal text; the status glyph and Needs attention carry it.
        self.tree.tag_configure("muted", foreground=HINT_TEXT)
        # Scrollbars keep their space and only show a thumb on overflow, so the table never resizes.
        self.scrollbar_y = ctk.CTkScrollbar(self.frame_table_container, orientation="vertical", width=8,
            corner_radius=4, border_spacing=1, fg_color="transparent", command=self.tree.yview)
        self.scrollbar_x = ctk.CTkScrollbar(self.frame_table_container, orientation="horizontal", height=8,
            corner_radius=4, border_spacing=1, fg_color="transparent", command=self.tree.xview)
        self.tree.configure(yscrollcommand=self._set_table_yview, xscrollcommand=self._set_table_xview)
        self.tree.grid(row=1, column=0, sticky="nsew")
        self.scrollbar_y.grid(row=1, column=1, sticky="ns")
        self.scrollbar_x.grid(row=2, column=0, sticky="ew")
        self._show_scroll_thumb(self.scrollbar_y, False)
        self._show_scroll_thumb(self.scrollbar_x, False)
        self.lbl_empty = ctk.CTkLabel(self.frame_table_container, text=self._idle_empty_text(),
                                      text_color=MUTED_TEXT, fg_color=TABLE_BG)
        self.lbl_empty.place(relx=0.5, rely=0.56, anchor="center")
        # First run: a short checklist that ticks itself off, instead of only shortcuts.
        self.frame_empty = ctk.CTkFrame(self.frame_table_container, fg_color=TABLE_BG)
        ctk.CTkLabel(self.frame_empty, text="Get started", font=ctk.CTkFont(size=16, weight="bold"),
                     anchor="w").pack(fill="x", pady=(0, 6))
        self._setup_steps = {}
        for key, command in (("key", self._open_api_key_settings), ("folder", self.browse_dir),
                             ("jd", self._focus_jd)):
            step = link_button(self.frame_empty, text="", height=30, anchor="w",
                               font=ctk.CTkFont(size=14), command=command)
            step.pack(fill="x")
            self._setup_steps[key] = step
        ctk.CTkLabel(self.frame_empty, text=self._idle_empty_text().split("\n", 1)[1],
                     text_color=HINT_TEXT, anchor="w").pack(fill="x", pady=(8, 0))
        self.tree.bind("<Double-1>", self.on_row_double_click)
        self.tree.bind("<Return>", self.on_row_double_click)
        self.tree.bind("<space>", self.on_row_double_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_candidate_selected)
        self._smooth_scroll = SmoothTreeScroll(self.tree,
            enabled=lambda: self.preferences['smooth_scrolling'],
            reduced_motion=lambda: self.preferences['reduce_motion'])
        for sequence in ('<MouseWheel>', '<Shift-MouseWheel>', '<Button-4>', '<Button-5>'):
            self.bind(sequence, self._route_table_wheel, add='+')
        self.bind('<TouchpadScroll>', lambda event: self._route_table_wheel(event, touchpad=True), add='+')
        # Bypass CTkScrollbar's coarse wheel callback; keep its click/drag bindings.
        for bar, axis in ((self.scrollbar_x,'x'), (self.scrollbar_y,'y')):
            for sequence in ('<MouseWheel>', '<Shift-MouseWheel>', '<Button-4>', '<Button-5>'):
                bar._canvas.bind(sequence, lambda event, a=axis:
                    self._smooth_scroll.handle_wheel(event, axis='x' if event.state & 1 else a))
            bar._canvas.bind('<TouchpadScroll>', self._smooth_scroll.handle_touchpad)
        self.bv_attention = tk.BooleanVar(value=False)
        self.chk_attention = ctk.CTkCheckBox(self.frame_filters, text='Needs attention (0)',
            variable=self.bv_attention, command=self.filter_table)
        self.chk_attention.pack(side='left', padx=8)
        self.lbl_result_count = ctk.CTkLabel(self.frame_header, text='', text_color=MUTED_TEXT)
        self.lbl_result_count.pack(side='left', padx=20)

        # A reusable activity window keeps the table and actions fixed in place.
        self._activity_window = ctk.CTkToplevel(self)
        self._activity_window.title("Processing activity")
        self._activity_window.geometry("760x420")
        self._activity_window.minsize(620, 320)
        self._activity_window.transient(self)
        self._activity_window.withdraw()
        self._activity_window.protocol("WM_DELETE_WINDOW", self._hide_activity)
        self._activity_window.grid_columnconfigure(0, weight=1)
        self._activity_window.grid_rowconfigure(0, weight=1)
        self.frame_activity = ctk.CTkFrame(self._activity_window, fg_color="transparent")
        self.frame_activity.grid(row=0, column=0, sticky="nsew", padx=8, pady=12)
        self.frame_activity.grid_columnconfigure(0, weight=1)
        self.frame_activity.grid_rowconfigure(2, weight=1)
        self.frame_upload_stats = ctk.CTkFrame(self.frame_activity, fg_color="transparent")
        self.frame_upload_stats.grid(row=0, column=0, sticky="ew", padx=16)
        for attr, label in (("lbl_uploaded_cnt", "Uploaded"), ("lbl_uploading_cnt", "Uploading"),
                            ("lbl_failed_cnt", "Failed"), ("lbl_cancelled_cnt", "Cancelled"), ("lbl_total_cnt", "Total")):
            if attr == "lbl_failed_cnt":
                # Failures are the one count worth acting on: it filters the table to them.
                widget = link_button(self.frame_upload_stats, text=f"{label}: 0", height=22,
                                     command=self._show_attention_rows)
            else:
                widget = ctk.CTkLabel(self.frame_upload_stats, text=f"{label}: 0", text_color=MUTED_TEXT)
            widget.pack(side="left", padx=(0, 20))
            setattr(self, attr, widget)
        self.btn_copy_log = link_button(self.frame_upload_stats, text="Copy log", height=22,
            command=lambda: self._copy_to_clipboard(self.btn_copy_log, self._activity_text(), "Copy log"))
        self.btn_copy_log.pack(side="right")
        self.lbl_curr_upload = ctk.CTkLabel(self.frame_activity, text="No uploads in progress", anchor="w", text_color=MUTED_TEXT)
        self.lbl_curr_upload.grid(row=1, column=0, sticky="ew", padx=16)
        self.txt_activity = ctk.CTkTextbox(self.frame_activity, height=130, wrap="word")
        self.txt_activity.grid(row=2, column=0, sticky="nsew", padx=16, pady=(4, 12))
        self.txt_activity.configure(state="disabled")

        # One action bar: run status, progress and its inline actions on the left;
        # at most three buttons on the right, with Process fixed in the rightmost slot.
        self.frame_bottom = ctk.CTkFrame(self)
        self.frame_bottom.grid(row=4, column=0, padx=20, pady=(0, 16), sticky="ew")
        self.frame_bottom.grid_columnconfigure(0, weight=1)
        self.frame_status = ctk.CTkFrame(self.frame_bottom, fg_color="transparent")
        self.frame_status.grid(row=0, column=0, padx=(16, 12), pady=12, sticky="ew")
        self.frame_status.grid_columnconfigure(0, weight=1)
        self.lbl_status = ctk.CTkLabel(self.frame_status, text="Ready to process resumes", anchor="w", height=22)
        self.lbl_status.grid(row=0, column=0, sticky="ew")
        self.btn_skipped = link_button(self.frame_status, text="", height=22, command=self.show_skipped_files)
        self.btn_skipped.grid(row=0, column=1, padx=(8, 0))
        self.btn_skipped.grid_remove()
        self.btn_reveal = link_button(self.frame_status, text=REVEAL_LABEL, height=22, command=self._reveal_export)
        self.btn_reveal.grid(row=0, column=2, padx=(8, 0))
        self.btn_reveal.grid_remove()
        self.lbl_upload_pct = ctk.CTkLabel(self.frame_status, text="", anchor="e", text_color=MUTED_TEXT, height=22)
        self.lbl_upload_pct.grid(row=0, column=3, sticky="e", padx=(12, 0))
        self.btn_retry = link_button(self.frame_status, text="Retry uploads", height=22,
            state="disabled", command=self.retry_failed_uploads)
        self.btn_retry.grid(row=0, column=4, padx=(8, 0))
        self.btn_cancel_uploads = link_button(self.frame_status, text="Cancel uploads", height=22,
            state="disabled", command=self.cancel_uploads)
        self.btn_cancel_uploads.grid(row=0, column=5, padx=(8, 0))
        self.btn_activity = link_button(self.frame_status, text="Activity log", height=22,
            text_color=MUTED_TEXT, command=self.toggle_activity)
        self.btn_activity.grid(row=0, column=6, padx=(12, 0))
        self.progress_bar = ctk.CTkProgressBar(self.frame_status, height=4)
        self.progress_bar.grid(row=1, column=0, columnspan=7, sticky="ew", pady=(8, 0))
        self.progress_bar.set(0)
        self._progress_colors = (self.progress_bar.cget("fg_color"), self.progress_bar.cget("progress_color"))
        self._progress_shown = True
        self.btn_clear = secondary_button(self.frame_bottom, text='Clear batch', width=130,
            state='disabled', command=self.clear_batch)
        self.btn_clear.grid(row=0, column=1, padx=(0, 8))
        self.btn_export = secondary_button(self.frame_bottom, text="Export CSV", width=150,
            state="disabled", command=self.show_export_menu)
        self.btn_export.grid(row=0, column=2, padx=(0, 8))
        self.btn_extract = ctk.CTkButton(self.frame_bottom, text="Process resumes", height=36, width=150,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=14), command=self.process_action)
        self.btn_extract.grid(row=0, column=3, padx=(0, 16))
        # Slots keep their width when their button is hidden, so Process never moves.
        for col, width in ((1, 138), (2, 158)):
            self.frame_bottom.grid_columnconfigure(col, minsize=width)
        self._export_is_next = None
        self._sync_action_buttons()

        mod = "Command" if sys.platform == "darwin" else "Control"
        self.bind(f"<{mod}-o>", lambda event: self._shortcut(self.btn_browse, self.browse_dir))
        self.bind(f"<{mod}-Return>", lambda event: self._shortcut(self.btn_extract, self.process_action))
        self.bind(f"<{mod}-e>", lambda event: self._shortcut(self.btn_export, self.show_export_menu))
        self.bind(f"<{mod}-f>", lambda event: self._shortcut(self.entry_search, self.entry_search.focus_set))
        self.bind(f"<{mod}-comma>", lambda event: self._shortcut(self.btn_settings, self.toggle_settings))
        self.bind("<Button-1>", self._dismiss_settings_on_outside_click, add="+")
        # macOS has a native animated fullscreen; elsewhere the change fades through.
        self.bind("<Control-Command-f>" if sys.platform == "darwin" else "<F11>", lambda event: self.toggle_fullscreen())
        self.bind("<Escape>", lambda event: self.toggle_fullscreen() if self.attributes("-fullscreen") else None, add="+")

        # Disabled controls say why on hover; usable ones show their shortcut.
        def when_disabled(widget, reason, shortcut=None):
            return lambda: reason() if widget.cget("state") == "disabled" else shortcut
        self._tooltips = [
            Tooltip(self.btn_extract, when_disabled(self.btn_extract, self._process_disabled_reason,
                    f"Process resumes ({shortcut_label('process')})")),
            Tooltip(self.btn_settings, f"Settings ({shortcut_label('settings')})"),
            Tooltip(self.btn_export, lambda: f"Export CSV ({shortcut_label('export')})"),
            Tooltip(self.btn_details, when_disabled(self.btn_details, lambda: "Select a candidate in the table first.")),
            Tooltip(self.btn_clear, when_disabled(self.btn_clear, lambda: "Available when the batch has finished.")),
            Tooltip(self.chk_jd_matching, when_disabled(self.chk_jd_matching,
                    lambda: "Matching can't change while a batch is running.")),
            Tooltip(self.btn_browse, when_disabled(self.btn_browse,
                    lambda: "The folder is locked while a batch is running.",
                    f"Choose folder ({shortcut_label('open')})")),
            Tooltip(self.btn_upload_jd, when_disabled(self.btn_upload_jd,
                    lambda: "Tick “Match against a job description” to add one.")),
        ]

        # Upload Tracking State
        self.total_uploads = 0
        self.completed_uploads = 0
        self.failed_uploads = 0
        self.cancelled_uploads = 0
        self.uploading_files = set()
        self._pipeline_running = False
        self._connection_busy = False
        # Run generation of the run whose uploads were cancelled (None = not cancelled).
        # Written on the main thread, read by upload workers.
        self._uploads_cancelled_gen = None
        # Run generation: bumped by start_extraction. Upload jobs and their UI events
        # carry the generation they were queued under; stale ones are dropped.
        self._run_gen = 0

        self.extracted_data = [] # stores all fetched rows (dict format)
        self.candidates_by_file = {}  # SourceFilename -> row dict (same objects as extracted_data)

        # Main-thread UI event queue: background threads only ever call _post()
        self.ui_events = queue.Queue()
        self._table_dirty = False
        self._progress_dirty = False
        # Coalesced matching progress/status: the pipeline posts _queue_status_update,
        # the drain applies only the latest values once per tick.
        self._pending_progress = None
        self._pending_status = None
        self._status_dirty = False
        self._sort_state = None  # (col, numeric, descending) or None = insertion order
        self.removal_missing_name = 0
        self.removal_missing_contact = 0
        self.removal_country_unresolved = 0
        self.processing_records = []
        self.pdf_count = 0
        self.docx_count = 0
        self.doc_count = 0
        self.unsupported_count = 0
        self.skipped_files = []
        self._closing = False
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_upload_worker()
        self.after(100, self._drain_ui_events)
        self._preview_after = None
        self._preview_generation = 0
        self._pipeline_finished = False
        self._pipeline_error = None
        self._processed_count = 0
        self._processing_issue_count = 0
        self._processing_report = "No processing activity yet."
        self._activity_log = []
        self._preview_skipped = []
        self._preview_found = None
        self._match_progress = 0.0
        self._shown_progress = 0.0
        self._progress_waiting = False
        self._last_export_dir = None
        self._batch_folder = ''
        self._batch_jd = ''
        self._batch_model = self.sv_model.get()
        self._batch_matching_enabled = False
        self._batch_drive_folder = ''
        self._evaluate_anyway = set()
        self._visible_candidates = []
        if self.preferences['remember_workspace']:
            self._sort_state = tuple(self.preferences['sort_state']) if self.preferences['sort_state'] else None
            for col, width in self.preferences['column_widths'].items():
                if col in self.tree['columns']: self.tree.column(col, width=width)
        self._apply_text_scale(self.preferences['text_scale'])
        self._refresh_run_state()
        self._update_matching_filters()
        self._update_connection_status()
        self.sv_api_key.trace_add("write", self._update_connection_status)
        self.sv_dir.trace_add("write", self._schedule_folder_preview)
        self._sync_jd_section(animate=False)
        self._schedule_folder_preview()  # Show what the default folder holds right away.

    def _make_settings_window(self):
        return SettingsWindow(self, self.preferences, self.sv_api_key.get(), OPENAI_MODELS,
            self._save_settings, on_test_ai=self._test_ai_connection,
            on_clear_analysis=self._clear_analysis_cache, on_clear_history=self._clear_export_history)

    def toggle_settings(self):
        if self._settings_window.is_open():
            self._settings_window.cancel()
            self._settings_open = False
            return
        self._settings_open = True
        self._settings_window.show(current=self.preferences, api_key=self.sv_api_key.get())
        self._settings_window.set_busy(self._pipeline_running or self._has_pending_uploads() or self._connection_busy)

    def _dismiss_settings_on_outside_click(self, event):
        """Settings behaves like a popover: a click anywhere in the main window closes
        it without saving. Clicks inside Settings or other windows are left alone."""
        settings = self._settings_window
        # Controls that open Settings handle their own click; it is not "outside".
        openers = (self.btn_settings, self.btn_key_chip, self._setup_steps['key'])
        widget = getattr(event, 'widget', None)
        try:
            while widget is not None and widget is not self:
                if widget in openers:
                    return
                widget = widget.master
            inside_main = event.widget.winfo_toplevel() is self
        except (AttributeError, KeyError, tk.TclError):
            inside_main = False
        if inside_main and settings.is_open():
            settings.cancel()
            self._settings_open = False

    def _save_settings(self, prefs, api_key):
        busy = self._pipeline_running or self._has_pending_uploads() or self._connection_busy
        if busy and (api_key != self.sv_api_key.get() or prefs['model'] != self.sv_model.get()
                     or prefs['drive_folder_id'] != self.preferences['drive_folder_id']):
            raise ValueError('Wait for this batch to finish before changing connections.')
        import drive_uploader
        prefs = dict(prefs)
        prefs['drive_folder_id'] = drive_uploader.normalize_folder_id(prefs['drive_folder_id'])
        previous_key = None
        changing_key = prefs['remember_api_key'] or self.preferences['remember_api_key']
        if changing_key:
            previous_key = self._secret_store.get()
        try:
            if prefs['remember_api_key']: self._secret_store.set(api_key)
            elif self.preferences['remember_api_key']: self._secret_store.delete()
            saved = self._preference_store.save(prefs)
        except Exception:
            if changing_key:
                try:
                    if previous_key is None: self._secret_store.delete()
                    else: self._secret_store.set(previous_key)
                except Exception:
                    messagebox.showerror('Settings', 'Settings could not be saved and secure key storage could not be restored. Check Remember API key before closing.')
            raise
        self.preferences = saved
        self.sv_api_key.set(api_key)
        self.sv_model.set(prefs['model'])
        self._settings_open = False
        self._apply_text_scale(prefs['text_scale'])
        self._schedule_folder_preview()
        self._refresh_run_state()

    def _apply_text_scale(self, scale=1.0):
        scale = 1.0
        ctk.set_widget_scaling(scale)
        if hasattr(self, 'tree'):
            ttk.Style().configure('Treeview', rowheight=round(36*scale), font=(UI_FONT_FAMILY,round(12*scale)))
            ttk.Style().configure('Treeview.Heading', font=(UI_FONT_FAMILY,round(12*scale),'bold'))
            # CTk's scaling callback replays its last geometry-manager call,
            # including widgets hidden by grid_remove. Restore intended visibility.
            self._sync_jd_section(animate=False)
            self._update_matching_filters()
            self._sync_action_buttons()
            if getattr(self, '_last_export_dir', None): self.btn_reveal.grid()
            else: self.btn_reveal.grid_remove()
            self._sync_skipped_link()
            if hasattr(self, 'extracted_data'):
                self._update_empty_state(len(self.tree.get_children("")))

    def _test_ai_connection(self, api_key, model):
        def test():
            try:
                client = OpenAI(api_key=api_key, timeout=20, max_retries=0)
                # Test every model used by this workflow, including the fixed JD parser.
                for name in dict.fromkeys([model, LUNA_MODEL]):
                    client.chat.completions.create(model=name, messages=[{'role':'user','content':'Reply OK.'}], max_completion_tokens=16)
                client.embeddings.create(model='text-embedding-3-small', input='connection test')
                self._post(lambda: messagebox.showinfo('AI connection', 'Extraction, matching, JD parsing and embeddings are available.'))
            except Exception as exc:
                self._post(lambda m=str(exc): messagebox.showerror('AI connection', m))
        threading.Thread(target=test,daemon=True).start()

    def _reconnect_drive_account(self):
        if self._pipeline_running or self._has_pending_uploads() or self._connection_busy: return
        self._connection_busy = True
        self._refresh_run_state()
        self._settings_window.set_busy(True)
        def reconnect():
            try:
                import drive_uploader
                creds=drive_uploader.get_drive_credentials(force_reauth=True)
                with self._drive_creds_lock:
                    self._drive_creds=creds
                    self._drive_auth_failed=False
                service=drive_uploader.get_drive_service(creds=creds)
                user=service.about().get(fields='user(emailAddress)').execute().get('user',{})
                message=f"Drive account: {user.get('emailAddress','connected')}. Test folder access before processing."
                def complete():
                    self._drive_account = user.get('emailAddress','Connected')
                self._post(complete)
            except Exception:
                self._post(lambda:messagebox.showerror('Drive sign-in','Could not switch Drive account. Try again in Settings.'))
            finally:
                self._post(self._finish_drive_connection)
        threading.Thread(target=reconnect,daemon=True).start()

    def _finish_drive_connection(self):
        self._connection_busy = False
        if self._settings_window.winfo_exists(): self._settings_window.set_busy(self._pipeline_running or self._has_pending_uploads())
        self._refresh_run_state()

    def _clear_analysis_cache(self):
        if self._pipeline_running or self._has_pending_uploads():
            messagebox.showinfo('Cache', 'Wait for the batch to finish before clearing cached analysis.')
            return
        if messagebox.askyesno('Clear cached analysis', 'Remove cached parsing and matching results? Saved corrections, CSV history and Drive links remain.'):
            for path in (RESUME_CACHE_FILE, JD_CACHE_FILE):
                if os.path.exists(path): os.remove(path)
            self.lbl_status.configure(text='Cached analysis cleared.')

    def _clear_export_history(self):
        if messagebox.askyesno('Clear CSV history','Clear local CSV history? Existing CSV files and saved corrections remain.'):
            self._review_store.clear_history()
            self.lbl_status.configure(text='CSV history cleared.')

    def _update_connection_status(self, *_):
        # Presence is not proof of a valid connection; validation happens on use.
        if self.sv_api_key.get().strip():
            self.btn_key_chip.pack_forget()
        elif not self.btn_key_chip.winfo_manager():
            self.btn_key_chip.pack(side="right", padx=12)
        if hasattr(self, "frame_empty"):
            self._update_empty_state(len(self.tree.get_children("")))

    def toggle_jd(self):
        self._jd_open = not self._jd_open
        self._sync_jd_section()

    def _jd_visible(self):
        return bool(self.bv_jd_matching.get()) and self._jd_open

    def _jd_target_height(self):
        return self.frame_jd_body.winfo_reqheight() + 12  # bottom padding inside the card

    def _fit_jd_clip(self):
        """Follow content changes (e.g. the inline error) once the box is open."""
        if self._jd_visible() and not is_animating(self.frame_jd_clip):
            self.frame_jd_clip.configure(height=self._jd_target_height())

    def _sync_jd_section(self, animate=True):
        """Box visible only while matching is on and open; folded shows a one-line summary."""
        enabled = self.bv_jd_matching.get()
        text = self.txt_jd.get("1.0", tk.END).strip()
        if enabled and self._jd_open and self._jd_error_shown and not text:
            self.lbl_jd_error.grid(row=1, column=0, sticky="ew", padx=16, pady=(6, 0))
        else:
            self.lbl_jd_error.grid_remove()
        if self._jd_visible():
            if self.frame_jd_body.winfo_manager() != "place":
                self.frame_jd_body.place(x=0, y=0, relwidth=1)
            self.frame_jd_body.update_idletasks()
            animate_height(self.frame_jd_clip, self._jd_target_height(),
                           MOTION_ENTER_MS if animate else 0, ease="out")
        else:
            animate_height(self.frame_jd_clip, 0, MOTION_EXIT_MS if animate else 0, ease="in",
                           on_done=self.frame_jd_body.place_forget)
        if not enabled:
            summary = "Off — resumes are only parsed and uploaded"
        elif self._jd_open:
            summary = ""
        elif text:
            first = text.splitlines()[0].strip()
            if len(first) > 60:
                first = first[:59] + "…"
            summary = f"{first} · {len(text.split())} words"
        else:
            summary = "No description yet — click to add one"
        self.lbl_jd_summary.configure(text=summary,
            text_color=WARNING_TEXT if enabled and not text and not self._jd_open else MUTED_TEXT,
            cursor="hand2" if enabled and not self._jd_open else "")
        self._update_jd_hint()

    def _open_jd(self):
        if self.bv_jd_matching.get() and not self._jd_open:
            self.toggle_jd()

    def _update_jd_hint(self):
        try:
            focus = self.focus_get()
        except (KeyError, tk.TclError):  # Focus inside a native dialog.
            focus = None
        focused = focus is not None and str(focus).startswith(str(self.txt_jd))
        if focused or self.txt_jd.get("1.0", tk.END).strip() or self.txt_jd.cget("state") == "disabled":
            self.lbl_jd_hint.place_forget()
        else:
            self.lbl_jd_hint.place(x=10, y=8)

    def _on_jd_edited(self):
        if self._jd_error_shown and self.txt_jd.get("1.0", tk.END).strip():
            self._jd_error_shown = False
            self._sync_jd_section()
        self._update_empty_state(len(self.tree.get_children("")))

    def _jd_missing(self):
        """Matching is on but there is nothing to match against."""
        return self.bv_jd_matching.get() is True and not self.txt_jd.get("1.0", tk.END).strip()

    def toggle_activity(self):
        if self._activity_open:
            self._hide_activity()
            return
        self._activity_open = True
        self._update_activity_report()
        animate_window_open(self._activity_window, self.preferences["reduce_motion"])
        self._activity_window.lift()

    def _hide_activity(self):
        self._activity_open = False
        self._activity_window.withdraw()

    def _log_activity(self, message):
        """Timestamped event for the activity window; the run report stays below it."""
        self._activity_log.append(f"{time.strftime('%H:%M:%S')}  {message}")
        del self._activity_log[:-500]
        self._update_activity_report()

    def _activity_text(self):
        events = "\n".join(self._activity_log)
        if not events:
            return self._processing_report
        if self._processing_report == "No processing activity yet.":
            return events
        return f"{events}\n\n{self._processing_report}"

    def _update_activity_report(self):
        self.txt_activity.configure(state="normal")
        self.txt_activity.delete("1.0", tk.END)
        self.txt_activity.insert("1.0", self._activity_text())
        self.txt_activity.configure(state="disabled")
        if self._activity_log:
            self.txt_activity.see(tk.END)

    def _show_attention_rows(self):
        self.bv_attention.set(True)
        self.filter_table()
        self.lift()

    def toggle_table_columns(self):
        columns = tuple(self.tree.cget("columns")) if self.bv_all_columns.get() else COMPACT_COLUMNS
        self.tree.configure(displaycolumns=columns)

    def _route_table_wheel(self, event, touchpad=False):
        """Native wheel events may target window focus instead of the table."""
        x,y=getattr(event,'x_root',-1),getattr(event,'y_root',-1)
        def contains(widget):
            return (widget.winfo_viewable() and
                widget.winfo_rootx() <= x < widget.winfo_rootx()+widget.winfo_width() and
                widget.winfo_rooty() <= y < widget.winfo_rooty()+widget.winfo_height())
        if contains(self.scrollbar_x):
            return self._smooth_scroll.handle_touchpad(event) if touchpad else self._smooth_scroll.handle_wheel(event,axis='x')
        if contains(self.tree) or contains(self.scrollbar_y):
            return self._smooth_scroll.handle_touchpad(event) if touchpad else self._smooth_scroll.handle_wheel(event)
        return None

    def _set_table_yview(self, first, last):
        self.scrollbar_y.set(first, last)
        self._show_scroll_thumb(self.scrollbar_y, not (float(first) <= 0 and float(last) >= 1))

    def _set_table_xview(self, first, last):
        self.scrollbar_x.set(first, last)
        self._show_scroll_thumb(self.scrollbar_x, not (float(first) <= 0 and float(last) >= 1))

    def _show_scroll_thumb(self, bar, show):
        """Hide a scrollbar by blending its thumb into the background; its space stays reserved."""
        if getattr(bar, "_thumb_shown", None) == show:
            return
        bar._thumb_shown = show
        hidden = self.frame_table_container.cget("fg_color")
        bar.configure(button_color=SCROLL_THUMB if show else hidden,
                      button_hover_color=SCROLL_THUMB_HOVER if show else hidden)

    def _shortcut(self, widget, action):
        if widget.cget("state") == "normal":
            action()
        return "break"

    def _update_search_hint(self):
        focus = self.focus_get()
        focused = focus is not None and str(focus).startswith(str(self.entry_search) + ".")
        if self.sv_search.get() or focused:
            self.lbl_search_hint.place_forget()
        else:
            self.lbl_search_hint.place(x=12, rely=0.5, anchor="w")

    def _on_search_changed(self, *_):
        self._update_search_hint()
        self.filter_table()

    def _on_score_filter(self, label):
        self.sv_min_score.set(SCORE_FILTERS[label])

    def _update_matching_filters(self):
        enabled = self.bv_jd_matching.get()
        self.seg_min_score.configure(state="normal" if enabled else "disabled")
        if enabled: self.frame_min_score.pack(side='left', padx=8, after=self.entry_search)
        else: self.frame_min_score.pack_forget()

    def on_candidate_selected(self, event=None):
        self.btn_details.configure(state="normal" if self.tree.selection() else "disabled")

    def _idle_empty_text(self):
        return (f"No results yet\n{shortcut_label('open')} choose folder  ·  "
                f"{shortcut_label('process')} process")

    def _update_empty_state(self, count):
        self.on_candidate_selected()
        self.frame_empty.place_forget()
        if count:
            self.lbl_empty.place_forget()
            return
        if self.extracted_data:
            text = "No candidates match these filters."
        elif self._pipeline_running:
            text = "Results appear here as resumes are processed."
        else:
            self.lbl_empty.place_forget()
            self._sync_setup_steps()
            self.frame_empty.place(relx=0.5, rely=0.56, anchor="center")
            return
        self.lbl_empty.configure(text=text)
        self.lbl_empty.place(relx=0.5, rely=0.56, anchor="center")

    def _sync_setup_steps(self):
        matching = self.bv_jd_matching.get()
        steps = (
            ("key", bool(self.sv_api_key.get().strip()), "Add your OpenAI API key"),
            ("folder", bool(getattr(self, "_preview_found", None)), "Choose a folder of resumes"),
            ("jd", not matching or bool(self.txt_jd.get("1.0", tk.END).strip()),
             "Paste a job description" if matching else "Job description — matching is off"),
        )
        for number, (key, done, label) in enumerate(steps, 1):
            optional = "" if key != "jd" or not matching else "  (optional)"
            self._setup_steps[key].configure(text=f"{'✓' if done else number}   {label}{optional}",
                text_color=MUTED_TEXT if done else LINK_TEXT)

    def _open_api_key_settings(self):
        if not self._settings_window.is_open():
            self.toggle_settings()
        self._settings_window._select_page('Connections')
        self.entry_api.focus_set()

    def _focus_jd(self):
        if not self.bv_jd_matching.get():
            return
        self._jd_open = True
        self._sync_jd_section()
        self.txt_jd.focus_set()

    def _pref(self, key):
        prefs = getattr(self, 'preferences', {})
        return prefs.get(key, DEFAULT_PREFERENCES.get(key)) if isinstance(prefs,dict) else DEFAULT_PREFERENCES.get(key)

    def _eligible_rows(self, filtered=False):
        rows = self._visible_candidates if filtered else self.extracted_data
        return get_deduplicated_candidates([r for r in rows if exportable(r, self._pref('export_missing'))])

    def process_action(self):
        if self._pipeline_running or self._has_pending_uploads() or self._connection_busy: return
        if self._pipeline_error:
            self.start_extraction()
        elif any(r.get('ResumeLink') in ('Failed','Cancelled') and r.get('Parsed', True) for r in self.extracted_data):
            self.retry_failed_uploads()
        elif self._pipeline_finished:
            return  # Done is an acknowledgement; Clear starts the next batch.
        else:
            self.start_extraction()

    def _close_batch_windows(self):
        for window in self._batch_windows:
            if window.winfo_exists(): window.destroy()
        self._batch_windows.clear()
        self._details_window = None

    def clear_batch(self):
        if self._pipeline_running or self._has_pending_uploads() or self._connection_busy: return
        self._close_batch_windows()
        self._run_gen += 1  # Ignore callbacks from the settled batch.
        self._preview_generation += 1
        self._smooth_scroll.cancel()
        self.extracted_data.clear()
        self.candidates_by_file.clear()
        self._visible_candidates = []
        self._pipeline_finished = False
        self._pipeline_error = None
        self._processed_count = self._processing_issue_count = 0
        self.total_uploads = self.completed_uploads = self.failed_uploads = self.cancelled_uploads = 0
        self.uploading_files.clear()
        self.skipped_files = []
        self._evaluate_anyway.clear()
        self.bv_jd_matching.set(self._pref('jd_matching_default'))
        self._jd_open = True  # A new batch starts with the job description in view.
        self._sync_jd_section()
        self._update_matching_filters()
        self._processing_report = 'No processing activity yet.'
        self._update_activity_report()
        self._last_export_dir = None
        self.btn_reveal.grid_remove()
        self.sv_search.set('')
        self.bv_attention.set(False)
        self.sv_min_score.set('0')
        self.seg_min_score.set('All')
        self._set_progress_waiting(False)
        self._match_progress = self._shown_progress = 0
        self.progress_bar.set(0)
        self.filter_table()
        self.update_upload_progress_ui()
        self.lbl_status.configure(text='Batch cleared. Choose a folder or process the next batch.')

    def _has_pending_uploads(self):
        return self.total_uploads > self.completed_uploads + self.failed_uploads + self.cancelled_uploads

    def _refresh_run_state(self):
        """Main thread: pipeline completion and upload completion are separate events."""
        pending = self._has_pending_uploads()
        busy = self._pipeline_running or pending or getattr(self,"_connection_busy",False) is True
        self._set_source_controls("disabled" if busy else "normal")
        self.chk_jd_matching.configure(state="disabled" if busy else "normal")
        failed = any(r.get('ResumeLink') in ('Failed','Cancelled') and r.get('Parsed', True) for r in self.extracted_data)
        label = ('Connecting Drive…' if getattr(self,'_connection_busy',False) is True else 'Processing…' if self._pipeline_running else 'Uploading…' if pending else
                 'Retry processing' if self._pipeline_error else 'Retry uploads' if failed else
                 'Done ✓' if self._pipeline_finished else self._start_button_text())
        self.btn_extract.configure(state='disabled' if busy or (self._pipeline_finished and not failed and not self._pipeline_error) else 'normal', text=label)
        if hasattr(self,'btn_clear'):
            self.btn_clear.configure(state='normal' if not busy and (self.extracted_data or self._pipeline_error or self._pipeline_finished) else 'disabled')
        retryable = sum(row.get("ResumeLink") in ("Failed", "Cancelled") and row.get("Parsed", True) for row in self.extracted_data)
        self.btn_retry.configure(state="normal" if retryable and getattr(self,"_connection_busy",False) is not True else "disabled",
                                 text=f"Retry {retryable} upload{'s' if retryable != 1 else ''}" if retryable else "Retry uploads")
        self.btn_cancel_uploads.configure(state="normal" if pending else "disabled")
        eligible = [r for r in self.extracted_data if exportable(r, ExtractorApp._pref(self, "export_missing"))]
        eligible = get_deduplicated_candidates(eligible)
        self.btn_export.configure(state="normal" if eligible and not self._pipeline_running else "disabled",
                                  text=f"Export CSV ({len(eligible)})" if eligible else "Export CSV")
        if self._pipeline_error:
            suffix = " Uploads continuing." if pending else ""
            self.lbl_status.configure(text=f"Processing failed — {self._pipeline_error}.{suffix}")
        elif not self._pipeline_running and self._pipeline_finished:
            if pending:
                # The upload count sits beside the status line, so it is not repeated here.
                text = "Processing finished — uploads continuing…"
            else:
                issues = self.failed_uploads + self.cancelled_uploads + self._processing_issue_count + len(self.skipped_files)
                label = "Complete with issues" if issues else "Complete"
                parts = [f"{self._processed_count} processed", f"{self.completed_uploads} uploaded"]
                if self.failed_uploads:
                    parts.append(f"{self.failed_uploads} failed")
                if self.cancelled_uploads:
                    parts.append(f"{self.cancelled_uploads} cancelled")
                if self._processing_issue_count:
                    parts.append(f"{self._processing_issue_count} need review")
                if self.skipped_files:
                    parts.append(f"{len(self.skipped_files)} skipped")
                text = f"{label} — " + ", ".join(parts) + "."
            self.lbl_status.configure(text=text)
        self._sync_action_buttons()

    def _sync_action_buttons(self):
        """Show only usable actions. Their columns keep a minimum width, so nothing shifts."""
        for button in (self.btn_retry, self.btn_cancel_uploads, self.btn_export, self.btn_clear):
            usable = button.cget("state") == "normal"
            if usable and not button.winfo_manager():
                button.grid()
            elif not usable and button.winfo_manager():
                button.grid_remove()
        # The accent marks the next step: Export once results are ready, otherwise Process.
        export_next = self.btn_export.cget("state") == "normal"
        if export_next != self._export_is_next:
            self._export_is_next = export_next
            self.btn_export.configure(fg_color=ACCENT if export_next else SECONDARY,
                                      hover_color=ACCENT_HOVER if export_next else SECONDARY_HOVER)
            self.btn_extract.configure(fg_color=SECONDARY if export_next else ACCENT,
                                       hover_color=SECONDARY_HOVER if export_next else ACCENT_HOVER)
        self._sync_progress_visibility()
        self._sync_skipped_link()

    def _sync_progress_visibility(self):
        """The bar only appears while work is under way. It keeps its row, so nothing moves."""
        active = bool(self._pipeline_running or self._has_pending_uploads()) if hasattr(self, "total_uploads") else False
        if active == self._progress_shown:
            return
        self._progress_shown = active
        background = self.frame_bottom.cget("fg_color")
        track, fill = self._progress_colors if active else (background, background)
        self.progress_bar.configure(fg_color=track, progress_color=fill)

    def _skipped_for_display(self):
        if self._pipeline_running or self._pipeline_finished or self._pipeline_error:
            return self.skipped_files
        return self._preview_skipped

    def _sync_skipped_link(self):
        skipped = self._skipped_for_display() if hasattr(self, "_preview_skipped") else []
        if skipped:
            count = len(skipped)
            self.btn_skipped.configure(text=f"View {count} skipped file{'s' if count != 1 else ''}")
            self.btn_skipped.grid()
        else:
            self.btn_skipped.grid_remove()

    def show_skipped_files(self):
        skipped = self._skipped_for_display()
        if skipped:
            messagebox.showinfo("Skipped files", format_skipped_files(skipped, limit=40), parent=self)

    def center_window(self, width, height):
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        width = min(width, screen_width - 80)
        height = min(height, screen_height - 100)
        x = (screen_width / 2) - (width / 2)
        y = (screen_height / 2) - (height / 2)
        self.geometry(f"{width}x{height}+{int(x)}+{int(y)}")

    def set_matching_progress(self, val):
        self._match_progress = val
        self._render_progress()

    def _render_progress(self):
        """One bar for the whole run. It animates until there is something to measure,
        then never moves backwards within a run."""
        if self._pipeline_running and self._match_progress <= 0:
            self._set_progress_waiting(True)
            return
        finished = self.completed_uploads + self.failed_uploads + self.cancelled_uploads
        value = run_progress(self._match_progress, finished, self.total_uploads,
                             self._pipeline_running, bool(self.extracted_data) or self._pipeline_finished)
        self._shown_progress = max(self._shown_progress, value)
        self._set_progress_waiting(False)
        self.progress_bar.set(self._shown_progress)

    def _set_progress_waiting(self, waiting):
        if waiting == self._progress_waiting:
            return
        self._progress_waiting = waiting
        if waiting:
            self.progress_bar.configure(mode="indeterminate")
            self.progress_bar.start()
        else:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")

    def _reset_progress(self):
        self._match_progress = 0.0
        self._shown_progress = 0.0
        self._render_progress()

    # ── Main-thread UI event queue ──────────────────────────────────────────

    def _post(self, fn: Callable[[], None]) -> None:
        """Queue a callable to run on the Tk main thread. The only UI entry point for background threads."""
        self.ui_events.put(fn)

    def _queue_status_update(self, progress: Optional[float] = None, text: Optional[str] = None) -> None:
        """Main thread (posted by the pipeline): remember the latest matching progress
        and/or status text. _drain_ui_events applies them once per tick."""
        if progress is not None:
            self._pending_progress = progress
        if text is not None:
            self._pending_status = text
        self._status_dirty = True

    def _apply_pending_status(self) -> None:
        """Main thread: push the latest queued progress/status to the widgets, once."""
        if not self._status_dirty:
            return
        self._status_dirty = False
        progress, text = self._pending_progress, self._pending_status
        self._pending_progress = None
        self._pending_status = None
        if progress is not None:
            self.set_matching_progress(progress)
        if text is not None:
            self.lbl_status.configure(text=text)

    def _drain_ui_events(self) -> None:
        """Main thread: process a bounded batch so Tk can handle input and paint."""
        try:
            deadline = time.perf_counter() + 0.008
            for _ in range(100):
                try:
                    fn = self.ui_events.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn()
                except Exception as e:
                    print(f"[-] UI event error: {e}")
                    traceback.print_exc()
                if time.perf_counter() >= deadline:
                    break
            self._apply_pending_status()
            if self._table_dirty:
                self._table_dirty = False
                self.filter_table()
            if self._progress_dirty:
                self._progress_dirty = False
                self.update_upload_progress_ui()
        except Exception as e:
            print(f"[-] UI drain error: {e}")
            traceback.print_exc()
        finally:
            if not self._closing:
                self.after(10 if not self.ui_events.empty() else 100, self._drain_ui_events)

    def on_close(self, force=False) -> None:
        """Window close: stop the drain loop, flush unsaved Drive links, then destroy.
        The resume cache is checkpointed during the run, so closing mid-run loses
        at most CACHE_CHECKPOINT_EVERY match results."""
        if not force and (self._pipeline_running or self._has_pending_uploads()):
            if not messagebox.askyesno('Processing in progress',
                'Close and stop this batch? Queued uploads will stop; completed uploads and cached results remain. You can rerun the folder later.'):
                return
        self._closing = True
        self._uploads_cancelled_gen = self._run_gen
        self._run_gen += 1
        self._smooth_scroll.destroy()
        try:
            prefs = dict(self.preferences)
            if prefs['reopen_last_folder']: prefs['last_folder'] = self.sv_dir.get()
            else: prefs['last_folder'] = ''
            if prefs['remember_workspace']:
                prefs.update(window_geometry=self.geometry(),
                    column_widths={col:self.tree.column(col,'width') for col in self.tree['columns']},
                    sort_state=self._sort_state)
            else: prefs.update(window_geometry='', column_widths={}, sort_state=None)
            self._preference_store.save(prefs)
        except Exception as exc:
            print(f'Could not save workspace preferences: {exc}')
        try:
            self._save_drive_cache(force=True)
        except Exception as e:
            print(f"[-] Drive cache flush on close failed: {e}")
        self.destroy()

    def _record_upload_counts(self, total: int = 0, failed: int = 0) -> None:
        """Main thread: bump upload counters and mark the progress labels dirty."""
        self.total_uploads += total
        self.failed_uploads += failed
        self._progress_dirty = True

    def update_upload_progress_ui(self):
        completed_count = self.completed_uploads + self.failed_uploads + self.cancelled_uploads
        if self.total_uploads > 0:
            self.lbl_upload_pct.configure(
                text=f"{self.completed_uploads} of {self.total_uploads} uploaded"
            )
        else:
            self.lbl_upload_pct.configure(text="")
        self.lbl_uploaded_cnt.configure(text=f"Uploaded: {self.completed_uploads}")
        self.lbl_uploading_cnt.configure(text=f"Uploading: {len(self.uploading_files)}")
        self.lbl_failed_cnt.configure(text=f"Failed: {self.failed_uploads}")
        self.lbl_cancelled_cnt.configure(text=f"Cancelled: {self.cancelled_uploads}")
        self.lbl_total_cnt.configure(text=f"Total: {self.total_uploads}")
        if self.uploading_files:
            current = f"Currently uploading: {', '.join(sorted(self.uploading_files))}"
        elif self.total_uploads == 0:
            # Uploads are queued once resumes are parsed; a run can also end with none
            current = ("Waiting for parsed resumes\u2026" if self._pipeline_running
                       else "Nothing to upload (no valid candidates)" if self._run_gen else "Currently uploading: None")
        elif completed_count < self.total_uploads:
            current = "Waiting to upload\u2026"
        else:
            current = "Currently uploading: None"
        self.lbl_curr_upload.configure(text=current[:160] + ("…" if len(current) > 160 else ""))
        self._render_progress()
        self._refresh_run_state()

    def retry_failed_uploads(self):
        """Re-queue failed and cancelled uploads of parsed candidates."""
        if getattr(self,"_connection_busy",False) is True: return
        import drive_uploader
        self._batch_drive_folder = drive_uploader.normalize_folder_id(ExtractorApp._pref(self,"drive_folder_id")) or drive_uploader.DRIVE_FOLDER_ID
        failed_candidates = [row for row in self.extracted_data if row.get("ResumeLink") in ("Failed", "Cancelled") and row.get("Parsed", True)]
        if not failed_candidates:
            messagebox.showinfo("Retry", "No failed or cancelled uploads to retry.")
            return

        n_cancelled = sum(1 for row in failed_candidates if row.get("ResumeLink") == "Cancelled")
        self.failed_uploads -= len(failed_candidates) - n_cancelled
        self.cancelled_uploads -= n_cancelled
        self._uploads_cancelled_gen = None
        self._shown_progress = 0.0  # Retried uploads lower the true progress again.
        folder = getattr(self, "_batch_folder", "")
        if not isinstance(folder,str) or not folder: folder = self.sv_dir.get()
        # Let the retry attempt Drive sign-in again, with fresh credentials (recovers a
        # revoked/expired token); workers rebuild their service when the creds change.
        with self._drive_creds_lock:  # short: sign-in never holds this lock
            self._drive_auth_failed = False
            self._drive_creds = None

        for row in failed_candidates:
            filename = row.get("SourceFilename")
            row["ResumeLink"] = "Pending"
            self.add_or_update_candidate(row)
            
            filepath = os.path.join(folder, filename)
            self.upload_queue.put((self._run_gen, filename, filepath, None))

        self._progress_dirty = True
        self._refresh_run_state()

    def cancel_uploads(self):
        """Stop this run's Drive uploads: queued jobs are dropped now, in-flight ones
        stop at their next chunk, and jobs the pipeline queues later are skipped.
        Matching keeps running. Retry Failed re-queues the cancelled files."""
        if not any(row.get("ResumeLink") in ("Pending", "Uploading") for row in self.extracted_data):
            messagebox.showinfo("Cancel Uploads", "No uploads in progress.")
            return
        self._uploads_cancelled_gen = self._run_gen
        stop_jobs = 0
        while True:
            try:
                job = self.upload_queue.get_nowait()
            except queue.Empty:
                break
            if job is None:
                stop_jobs += 1
            else:
                gen, filename, _filepath, _file_hash = job
                self.handle_upload_cancelled(filename, gen)
            self.upload_queue.task_done()
        for _ in range(stop_jobs):
            self.upload_queue.put(None)
        self.lbl_status.configure(text="Uploads cancelled \u2014 use Retry to upload them later.")
        self._log_activity("Uploads cancelled")

    # ── UI Actions ──────────────────────────────────────────────────────────

    def on_jd_matching_toggled(self):
        """Grey out the JD inputs when matching is off; the run then only parses and uploads."""
        enabled = self.bv_jd_matching.get()
        state = "normal" if enabled else "disabled"
        self.txt_jd.configure(state=state)
        self.btn_upload_jd.configure(state=state)
        if enabled and not self.txt_jd.get("1.0", tk.END).strip():
            self._jd_open = True  # Nothing to summarise yet: open it for typing.
        if not enabled:
            self._jd_error_shown = False
        self._sync_jd_section()
        self._update_matching_filters()
        self.filter_table()
        if not self._pipeline_running:
            self.btn_extract.configure(text=self._start_button_text())

    def toggle_fullscreen(self):
        entering = not self.attributes("-fullscreen")
        if sys.platform == "darwin":
            self.attributes("-fullscreen", entering)  # AppKit animates this itself.
        else:
            fade_through(self, lambda: self.attributes("-fullscreen", entering), self._pref("reduce_motion"))
        return "break"

    def _process_disabled_reason(self):
        if self._connection_busy:
            return "Checking the Google Drive connection…"
        if self._pipeline_running:
            return "Resumes are being processed."
        if self._has_pending_uploads():
            return "Waiting for uploads to finish."
        if self._pipeline_finished:
            return "This batch is done. Clear batch to start another."
        return None

    def _start_button_text(self):
        return "Process resumes"

    def _schedule_folder_preview(self, *_):
        """Debounce read-only previews. Input changes never start the pipeline."""
        self._preview_generation += 1
        if self._preview_after is not None:
            self.after_cancel(self._preview_after)
        self._preview_after = self.after(400, self._start_folder_preview)

    def _start_folder_preview(self):
        self._preview_after = None
        if self._closing:
            return
        folder = os.path.expanduser(self.sv_dir.get().strip().strip('\"\''))
        generation = self._preview_generation
        if not folder:
            self._show_idle_status("Choose a folder containing PDF, DOCX, DOC, or TXT resumes.")
            return
        self._show_idle_status("Checking folder…")
        threading.Thread(target=self._preview_resume_folder, args=(folder, generation), daemon=True).start()

    def _preview_resume_folder(self, folder, generation):
        try:
            if not os.path.isdir(folder):
                raise FileNotFoundError
            supported, skipped = discover_resume_files(folder, recursive=ExtractorApp._pref(self, "recursive"))
            count = len(supported)
            text = f"{count} resume{'s' if count != 1 else ''} found"
            if skipped:
                text += f" · {len(skipped)} unsupported file{'s' if len(skipped) != 1 else ''} will be skipped"
            text += ". Click Process resumes to begin." if supported else ". Choose another folder."
        except FileNotFoundError:
            text, supported, skipped = "Folder not found. Choose a resume folder to begin.", [], []
        except OSError as exc:
            text, supported, skipped = f"Could not read folder: {exc}", [], []
        self._post(lambda message=text, found=len(supported), skip=list(skipped):
                   self._show_folder_preview(generation, message, skip, found))

    def _show_folder_preview(self, generation, message, skipped=None, found=None):
        if not self._closing and generation == self._preview_generation:
            self._preview_skipped = list(skipped or [])
            self._preview_found = found
            self._show_idle_status(message)
            self._sync_skipped_link()
            self._update_empty_state(len(self.tree.get_children("")))

    def _show_idle_status(self, text):
        """Folder feedback shares the status line, but never replaces a run's progress."""
        if not (self._pipeline_running or self._has_pending_uploads() or self._pipeline_finished or self._pipeline_error):
            self.lbl_status.configure(text=text)

    def _set_source_controls(self, state):
        for widget in (self.entry_dir, self.entry_api, self.btn_browse, self.combo_model):
            widget.configure(state=state)

    def browse_dir(self):
        new_dir = filedialog.askdirectory(initialdir=self.sv_dir.get())
        if new_dir:
            self.sv_dir.set(new_dir)
            
    def upload_jd(self):
        file_path = filedialog.askopenfilename(
            filetypes=[("Text/Doc/PDF", "*.txt *.pdf *.docx"), ("All Files", "*.*")]
        )
        if file_path:
            text = extract_text_from_file(file_path)
            if text:
                self.txt_jd.delete("1.0", tk.END)
                self.txt_jd.insert("1.0", text)
                self._jd_error_shown = False
                self._sync_jd_section()
                self._update_empty_state(len(self.tree.get_children("")))
            else:
                messagebox.showerror("Error", "Could not read the JD file.")

    def sort_table(self, col, numeric=False):
        state = getattr(self, "_sort_state", None)
        if state is not None and state[0] == col:
            descending = not state[2]
        else:
            descending = True  # first click on a column sorts descending
        self._sort_state = (col, numeric, descending)
        self._apply_sort()
        self._update_sort_indicators()

    def _update_sort_indicators(self):
        for col, text in self._headings.items():
            if self._sort_state and self._sort_state[0] == col:
                text += " ▼" if self._sort_state[2] else " ▲"
            self.tree.heading(col, text=text)

    def _apply_default_sort(self):
        """After a matching run, rank by match unless the user already chose an order."""
        if self._sort_state is None and self.bv_jd_matching.get():
            self._sort_state = ("MatchPct", True, True)
            self._apply_sort()
            self._update_sort_indicators()

    def _apply_sort(self):
        """Reorder tree children per _sort_state without toggling direction."""
        state = getattr(self, "_sort_state", None)
        if not state:
            return
        col, numeric, descending = state
        data = [(self.tree.set(child, col), child) for child in self.tree.get_children("")]

        def num_key(item):
            # Placeholders such as "—" sort below every real value.
            value = as_percent(item[0])
            return -1.0 if value is None else value

        if numeric:
            data.sort(key=num_key, reverse=descending)
        else:
            data.sort(key=lambda x: str(x[0]).lower(), reverse=descending)

        for index, (_val, child) in enumerate(data):
            self.tree.move(child, "", index)

    def filter_table(self):
        query = self.sv_search.get().lower()
        min_score_str = self.sv_min_score.get()
        try:
            min_score = float(min_score_str) if min_score_str else 0.0
        except ValueError:
            min_score = 0.0

        if not self.bv_jd_matching.get():
            min_score = 0.0

        # Keep existing items alive: replacing the whole tree loses keyboard
        # focus and causes flicker whenever parsing/upload progress changes.
        saved_selection = self.tree.selection()
        saved_first = self.tree.yview()[0]
        visible = []

        # Every parsed file is shown (its Status says whether it is valid); only
        # export is limited to valid, uploaded candidates. Duplicates are merged.
        deduped_cands = get_deduplicated_candidates(self.extracted_data)

        attention_count = sum(bool(attention_reasons(r)) for r in deduped_cands)
        if hasattr(self,'chk_attention'):
            self.chk_attention.configure(text=f'Needs attention ({attention_count})')
        for row in deduped_cands:
            if hasattr(self,'bv_attention') and self.bv_attention.get() is True and not attention_reasons(row):
                continue
            if safe_match_percentage(row) < min_score:
                continue

            row_vals = [str(v).lower() for v in row.values()]
            if query and not any(query in v for v in row_vals):
                continue

            iid = self.add_to_tree(row)
            visible.append(iid)

        wanted = set(visible)
        for iid in self.tree.get_children():
            if iid not in wanted:
                self.tree.delete(iid)

        if not self._sort_state:
            for index, iid in enumerate(visible):
                self.tree.move(iid, "", index)

        self._apply_sort()

        restore = [iid for iid in saved_selection if self.tree.exists(iid)]
        if restore:
            self.tree.selection_set(restore)
        if self.tree.get_children(""):
            self.tree.yview_moveto(saved_first)
        self._visible_candidates = [r for r in deduped_cands if r.get('SourceFilename') in wanted]
        if hasattr(self, 'lbl_result_count'):
            merged = len(self.extracted_data)-len(deduped_cands)
            self.lbl_result_count.configure(text=f'{len(visible)} shown · {len(deduped_cands)} total' + (f' · {merged} duplicate{"s" if merged != 1 else ""} merged' if merged else ''))
        self._update_empty_state(len(visible))

    def on_row_double_click(self, event):
        item = self.tree.selection()
        if not item: return

        iid = item[0]
        data = next((r for r in merge_duplicates(self.extracted_data) if r.get("SourceFilename") == iid), None)
        if data is None:
            # Rows without SourceFilename have tree-generated iids: fall back to email
            values = self.tree.item(iid, "values")
            email = values[2]
            data = next((r for r in self.extracted_data if r.get("Email") == email), None)
        if not data: return

        self.show_candidate_details(data)

    def show_candidate_details(self, data):
        import webbrowser
        win = self._details_window
        if win is None or not win.winfo_exists():
            win = self._details_window = ctk.CTkToplevel(self)
            self._batch_windows.append(win)
            win.geometry("620x700")
            win.minsize(500, 440)
            win.transient(self)
            win.bind("<Escape>", lambda event: win.withdraw())
        else:
            for child in win.winfo_children():
                child.destroy()
        win.title(f"Candidate details — {data.get('Name', '')}")
        animate_window_open(win, self._pref("reduce_motion"))
        win.lift()

        frame = ctk.CTkScrollableFrame(win, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=16, pady=16)

        # Header: who, how well they match, and where their upload stands.
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x")
        ctk.CTkLabel(header, text=data.get("Name") or "Unnamed candidate", anchor="w",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")
        score = as_percent(data.get("MatchPercentage"))
        if score is not None:
            # The tier word carries the meaning; colour only reinforces it.
            tier, colour = match_tier(score)
            ctk.CTkLabel(header, text=f"{score:.0f}% · {tier}", width=130, height=28, corner_radius=14,
                         fg_color=colour, text_color="#FFFFFF",
                         font=ctk.CTkFont(weight="bold")).pack(side="right")
        years = data.get("YearsOfExperience")
        upload = upload_status_display(data.get("ResumeLink"))[0]
        subline = [f"{years} yrs experience" if years not in (None, "", "N/A") else None,
                   data.get("Country") or None, upload or None]
        ctk.CTkLabel(frame, text="  ·  ".join(part for part in subline if part), text_color=MUTED_TEXT,
                     anchor="w").pack(fill="x", pady=(2, 12))

        facts = ctk.CTkFrame(frame, fg_color="transparent")
        facts.pack(fill="x")
        facts.grid_columnconfigure(1, weight=1)
        for index, (label, key, copyable) in enumerate((("Email", "Email", True), ("Phone", "Phone", True),
                                                        ("Status", "Status", False),
                                                        ("Source file", "SourceFilename", False))):
            value = str(data.get(key) or "—")
            shown = format_phone_display(value) if key == "Phone" else value
            ctk.CTkLabel(facts, text=label, text_color=MUTED_TEXT, width=90, anchor="w").grid(
                row=index, column=0, sticky="w", pady=2)
            ctk.CTkLabel(facts, text=shown, anchor="w", justify="left", wraplength=380).grid(
                row=index, column=1, sticky="w", pady=2)
            if copyable and value != "—":
                button = ctk.CTkButton(facts, text="Copy", width=80, height=36, fg_color="transparent",
                                       hover_color=SECONDARY, text_color=LINK_TEXT)
                button.configure(command=lambda b=button, v=value: self._copy_to_clipboard(b, v))
                button.grid(row=index, column=2, sticky="e")

        # One row of actions; an empty frame would otherwise keep its default height.
        tools = ctk.CTkFrame(frame, fg_color='transparent')
        tools.pack(fill='x', pady=(12, 8))
        resume_url = data.get("ResumeDriveURL") or data.get("ResumeLink", "")
        linkedin_url = data.get("linkedin_url", "")
        # Links that do not exist are left out rather than shown disabled.
        for label, url in (("Open resume", resume_url), ("Open LinkedIn", linkedin_url)):
            if isinstance(url, str) and url.startswith(("https://", "http://")):
                secondary_button(tools, text=label, width=130,
                    command=lambda target=url: webbrowser.open(target)).pack(side="left", padx=(0, 8))

        busy = self._pipeline_running or self._has_pending_uploads()
        edit_reason = ("Editing is available once processing and uploads finish." if busy else
                       None if parsed_candidate(data) else "This file could not be read, so there are no fields to edit.")
        edit = secondary_button(tools,text='Edit fields',width=130, state='disabled' if edit_reason else 'normal',
            command=lambda:self.edit_candidate(data))
        edit.pack(side='left',padx=(0,8))
        Tooltip(edit, edit_reason)
        source_path = os.path.join(self._batch_folder or self.sv_dir.get(),data.get('SourceFilename',''))
        secondary_button(tools,text='Open source',width=130,
            command=lambda:reveal_in_file_manager(source_path)).pack(side='left',padx=(0,8))
        if data.get('MatchPercentage') == 'Not evaluated' or not parsed_candidate(data) or data.get('ExtractionError'):
            secondary_button(tools,text='Evaluate anyway' if data.get('MatchPercentage') == 'Not evaluated' else 'Retry processing',width=150,
                state='disabled' if self._pipeline_running or self._has_pending_uploads() else 'normal',
                command=lambda generation=self._run_gen:self.retry_candidate(data,generation)).pack(side='left')
        elif data.get('ResumeLink') in ('Failed','Cancelled'):
            secondary_button(tools,text='Retry upload',width=130,command=self.retry_failed_uploads).pack(side='left')
        reasons = attention_reasons(data)
        if edit_reason:
            ctk.CTkLabel(frame, text=edit_reason, text_color=HINT_TEXT, anchor='w').pack(fill='x')
        if reasons:
            ctk.CTkLabel(frame,text='Needs attention: '+ '; '.join(reasons),text_color=WARNING_TEXT,
                wraplength=540,justify='left',anchor='w').pack(fill='x',pady=8)
        if data.get('EditedFields'):
            ctk.CTkLabel(frame,text='Edited: '+', '.join(data['EditedFields']),text_color=LINK_TEXT,anchor='w').pack(fill='x')
        sources = data.get('MergedSources',[])
        if len(sources)>1:
            ctk.CTkLabel(frame,text='Merged source files: '+', '.join(sources),text_color=MUTED_TEXT,
                wraplength=540,justify='left',anchor='w').pack(fill='x',pady=8)

        def heading(text):
            ctk.CTkLabel(frame, text=text, font=ctk.CTkFont(weight="bold"), anchor="w").pack(
                fill="x", pady=(16, 4))

        breakdown = [(label, as_percent(data.get(key))) for label, key in (
            ("Technical", "TechnicalMatch"), ("Experience", "ExperienceMatch"), ("Domain", "DomainMatch"))]
        if any(value is not None for _, value in breakdown):
            heading("Score breakdown")
            bars = ctk.CTkFrame(frame, fg_color="transparent")
            bars.pack(fill="x")
            bars.grid_columnconfigure(1, weight=1)
            for index, (label, value) in enumerate(breakdown):
                ctk.CTkLabel(bars, text=label, text_color=MUTED_TEXT, width=90, anchor="w").grid(
                    row=index, column=0, sticky="w", pady=3)
                bar = ctk.CTkProgressBar(bars, height=6)
                bar.set(max(0.0, min(1.0, (value or 0) / 100)))
                bar.grid(row=index, column=1, sticky="ew", padx=(0, 12))
                ctk.CTkLabel(bars, text="—" if value is None else f"{value:.0f}%", width=44, anchor="e").grid(
                    row=index, column=2, sticky="e")

        def section(title, value, mono=False):
            if isinstance(value, list):
                value = "\n".join(f"• {item}" for item in value if item)
            if not value or value == "N/A":
                return
            heading(title)
            font = ctk.CTkFont(family=UI_FONT_FAMILY, size=11) if mono else None
            ctk.CTkLabel(frame, text=value, wraplength=540, justify="left", anchor="w", font=font).pack(fill="x")

        # Gaps come before strengths: they decide whether a candidate goes forward.
        section("Summary", data.get("Summary"))
        section("Missing requirements", data.get("MissingRequirements"))
        section("Strengths", data.get("Strengths"))
        section("Weaknesses", data.get("Weaknesses"))
        evidence = data.get("SkillEvidence") or []
        section("Skill evidence", "\n".join(evidence) if isinstance(evidence, list) else evidence, mono=True)

    def retry_candidate(self, row, generation=None):
        if generation is not None and generation != self._run_gen: return
        filename = row.get('SourceFilename')
        current = self.candidates_by_file.get(filename)
        if current is None or current.get('FileHash') != row.get('FileHash'): return
        if self._pipeline_running or self._has_pending_uploads() or self._connection_busy: return
        filename = row.get('SourceFilename')
        if not filename: return
        self._evaluate_anyway.add(filename)
        self._reuse_uploads = {filename: (row.get('FileHash'),row.get('ResumeDriveURL'))} if row.get('ResumeLink')=='Uploaded' else {}
        self._pipeline_running = True
        self._pipeline_finished = False
        self._pipeline_error = None
        self._reset_progress()
        self._refresh_run_state()
        # Same generation and destination: existing batch rows and uploads stay intact.
        threading.Thread(target=self.extraction_thread,
            args=(self._batch_folder,[filename],[],self._batch_jd,self.sv_api_key.get(),self._batch_model),
            kwargs={'run_gen':self._run_gen},daemon=True).start()

    def edit_candidate(self, data):
        if self._pipeline_running or self._has_pending_uploads() or self._connection_busy: return
        win = ctk.CTkToplevel(self)
        self._batch_windows.append(win)
        generation = self._run_gen
        win.title('Edit candidate fields')
        win.transient(self)
        body = ctk.CTkFrame(win,fg_color='transparent')
        body.pack(fill='both',expand=True,padx=24,pady=20)
        entries = {}
        for i,(label,key) in enumerate([('Name','Name'),('Email','Email'),('Phone','Phone'),('Country','Country'),('Experience (years)','YearsOfExperience')]):
            ctk.CTkLabel(body,text=label,anchor='w').grid(row=i,column=0,sticky='w',pady=6)
            entry=ctk.CTkEntry(body,width=300,height=36)
            entry.insert(0, str(clean_field(data.get(key,''))))
            entry.grid(row=i,column=1,padx=(12,0),pady=6)
            entries[key]=entry
        error=ctk.CTkLabel(body,text='',text_color=DANGER_TEXT,wraplength=450)
        error.grid(row=5,column=0,columnspan=2)
        def save():
            if generation != self._run_gen or data.get('SourceFilename') not in self.candidates_by_file:
                if win.winfo_exists(): win.destroy()
                return
            try:
                values={k:e.get().strip() for k,e in entries.items()}
                if values['Email'] and not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$',values['Email']):
                    raise ValueError('Enter a valid email or leave it blank.')
                if values['YearsOfExperience']:
                    years=float(values['YearsOfExperience'])
                    if not math.isfinite(years) or years<0: raise ValueError('Experience must be a positive number or zero.')
                filename=data['SourceFilename']
                source=os.path.join(self._batch_folder or self.sv_dir.get(),filename)
                file_hash=data.get('FileHash') or get_file_hash(source)
                self._review_store.correct(file_hash,values)
                target=self.candidates_by_file[filename]
                target.update(values,FileHash=file_hash,EditedFields=list(values))
                target['Parsed']=True
                if all(clean_field(target.get(k)) != '' for k in values) and target.get('ResumeLink')=='Not uploaded':
                    target.update(Status='Edited · parsed',ResumeLink='Pending')
                    self.total_uploads+=1
                    self.upload_queue.put((self._run_gen,filename,source,file_hash))
                self._table_dirty=True
                self._refresh_run_state()
                self.filter_table()
                win.destroy()
                self.show_candidate_details(self._review_store.apply(target))
            except (ValueError,OSError) as exc: error.configure(text=str(exc))
        buttons=ctk.CTkFrame(body,fg_color='transparent')
        buttons.grid(row=6,column=0,columnspan=2,sticky='e',pady=(12,0))
        secondary_button(buttons,text='Cancel',width=120,command=win.destroy).pack(side='left',padx=8)
        ctk.CTkButton(buttons,text='Save edits',width=120,height=36,command=save).pack(side='left')
        win.bind('<Escape>',lambda e:win.destroy())
        win.grab_set()
        animate_window_open(win,self._pref('reduce_motion'))

    def _copy_to_clipboard(self, button, value, label="Copy"):
        self.clipboard_clear()
        self.clipboard_append(value)
        button.configure(text="Copied")
        button.after(1200, lambda: button.winfo_exists() and button.configure(text=label))

    # ── Extraction Logic ────────────────────────────────────────────────────

    def start_extraction(self, automatic=False):
        if self._pipeline_running is True or self._closing is True or self._has_pending_uploads() is True or getattr(self,"_connection_busy",False) is True:
            return
        try:
            import drive_uploader
            drive_uploader.normalize_folder_id(ExtractorApp._pref(self,'drive_folder_id'))
        except ValueError as exc:
            self.lbl_status.configure(text=str(exc))
            return
        api_key = self.sv_api_key.get().strip()
        if not api_key:
            self._open_api_key_settings()
            self.lbl_status.configure(text="Add your OpenAI API key in Settings to process resumes.")
            return

        folder = os.path.expanduser(self.sv_dir.get().strip().strip('\"\''))
        if not folder:
            if not automatic:
                messagebox.showerror("Error", "Please choose a resumes folder.")
            return
        if self._jd_missing() is True:
            # Matching silently turning off would surprise; ask for the missing input next to its field.
            self._jd_error_shown = True
            self._jd_open = True
            self._sync_jd_section()
            self.txt_jd.focus_set()
            self.lbl_status.configure(text="Add a job description to match against, or untick matching.")
            return

        jd_text = self.txt_jd.get("1.0", tk.END).strip() if self.bv_jd_matching.get() else ""
        model = self.sv_model.get()
        self._pipeline_finished = False
        self._pipeline_error = None
        self._pipeline_running = True
        self._set_source_controls("disabled")
        self.chk_jd_matching.configure(state="disabled")
        self.btn_extract.configure(state="disabled", text="Reading folder…")
        self._reset_progress()
        self.lbl_status.configure(text="Finding resume files…")
        threading.Thread(target=self._discover_resume_folder,
                         args=(folder, jd_text, api_key, model, automatic), daemon=True).start()

    def _discover_resume_folder(self, folder, jd_text, api_key, model, automatic):
        """Worker thread: keep directory/network I/O away from Tk."""
        try:
            supported_files, skipped_files = discover_resume_files(folder, recursive=ExtractorApp._pref(self, "recursive"))
        except OSError as exc:
            self._post(lambda msg=f"Could not read resumes folder: {exc}": self._folder_load_failed(msg, automatic))
            return
        if not supported_files:
            self._post(lambda: self._folder_load_failed(
                "No supported resume files (.pdf, .docx, .doc, .txt) found in the selected folder.", automatic))
            return
        self._post(lambda: self._begin_extraction(folder, supported_files, skipped_files, jd_text, api_key, model))

    def _folder_load_failed(self, message, automatic):
        self._pipeline_stopped()
        self.lbl_status.configure(text=message)
        if not automatic:
            self.after(0, lambda: messagebox.showerror("Resumes folder", message))

    def _begin_extraction(self, folder, supported_files, skipped_files, jd_text, api_key, model):
        """Main thread: initialise a run after background discovery completes."""
        if self._closing is True:
            return
        self._close_batch_windows()
        # New run: upload jobs/events from earlier runs no longer match _run_gen
        self._run_gen += 1
        run_gen = self._run_gen
        self._batch_folder = folder
        self._batch_jd = jd_text
        self._batch_model = model
        self._reuse_uploads = {}
        self._batch_matching_enabled = bool(jd_text)
        import drive_uploader
        self._batch_drive_folder = drive_uploader.normalize_folder_id(ExtractorApp._pref(self,'drive_folder_id')) or drive_uploader.DRIVE_FOLDER_ID

        self.btn_extract.configure(state="disabled", text="Processing…")
        self.btn_export.configure(state="disabled")
        self.btn_reveal.grid_remove()
        self._sync_action_buttons()
        self._pending_progress = None
        self._pending_status = None
        self._status_dirty = False
        self.lbl_status.configure(text=f"Processing {len(supported_files)} resumes…")
        self._pipeline_finished = False
        self._pipeline_error = None
        self._processed_count = 0
        self._processing_issue_count = 0
        self._processing_report = "Processing resumes. Detailed statistics will be available when processing finishes."
        self._log_activity(f"Started processing {len(supported_files)} resumes from {folder}")
        if self._settings_window.is_open():
            self._settings_window.cancel()
        self._settings_open = False
        if self._jd_open:
            self.toggle_jd()
        self._reset_progress()

        self.total_uploads = 0
        self.completed_uploads = 0
        self.failed_uploads = 0
        self.cancelled_uploads = 0
        self.uploading_files.clear()
        self._pipeline_running = True
        self.chk_jd_matching.configure(state="disabled")
        self._drive_auth_failed = False  # a new run may attempt Drive sign-in again
        self.removal_missing_name = 0
        self.removal_missing_contact = 0
        self.removal_country_unresolved = 0
        self.processing_records = []
        
        self.pdf_count = sum(1 for f in supported_files if f.lower().endswith('.pdf'))
        self.docx_count = sum(1 for f in supported_files if f.lower().endswith('.docx'))
        self.doc_count = sum(1 for f in supported_files if f.lower().endswith('.doc'))
        self.unsupported_count = len(skipped_files)
        self.skipped_files = skipped_files
        
        self._progress_dirty = True

        for item in self.tree.get_children():
            self.tree.delete(item)
        self.extracted_data = []
        self.candidates_by_file = {}
        self._update_empty_state(0)

        thread = threading.Thread(
            target=self.extraction_thread,
            args=(folder, supported_files, skipped_files, jd_text, api_key, model),
            kwargs={"run_gen": run_gen},
        )
        thread.daemon = True
        thread.start()

    def extraction_thread(self, folder, files, skipped_files, jd_text, api_key, model, run_gen=0):
        import asyncio
        asyncio.run(self.async_extraction_pipeline(folder, files, skipped_files, jd_text, api_key, model,
                                                   run_gen=run_gen))

    async def async_extraction_pipeline(self, folder, files, skipped_files, jd_text, api_key, model, run_gen=0):
        # run_gen: the run generation captured on the main thread when this run started
        # (never read self._run_gen from this thread).
        import time
        import json
        import hashlib
        
        start_time = time.time()

        # Publish every source file before any potentially slow disk or API work.
        # A slow Word document or network file must not leave the table empty.
        for filename in files:
            self._post(lambda f=filename: self.add_or_update_candidate(
                {"SourceFilename": f, "Name": f, "Status": "Reading…", "Parsed": False, "ResumeLink": "",
                 "MatchPercentage": ""}))
        
        read_time = 0
        embed_time = 0
        extract_time = 0
        match_time = 0
        
        try:
            client = OpenAI(api_key=api_key)
            async_client = AsyncOpenAI(api_key=api_key, max_retries=0)
            llm_sem = asyncio.Semaphore(LLM_CONCURRENCY)
            
            # Step 1: Parse JD
            parsed_jd = None
            jd_embedding = None
            jd_hash = ""
            if jd_text:
                jd_hash = hashlib.md5(jd_text.encode('utf-8')).hexdigest()
                cache_file = JD_CACHE_FILE
                cache_hit = False
                
                if os.path.exists(cache_file):
                    try:
                        with open(cache_file, "r", encoding="utf-8") as f:
                            cache_data = json.load(f)
                        if cache_data.get("jd_hash") == jd_hash:
                            self._post(lambda: self._queue_status_update(text="JD cache hit — skipping parse and embedding"))
                            parsed_jd = ParsedJD(**cache_data.get("parsed_jd"))
                            jd_embedding = cache_data.get("embedding")
                            cache_hit = True
                    except Exception as cache_err:
                        print(f"JD cache read error: {cache_err}")
                
                if not cache_hit:
                    self._post(lambda: self._queue_status_update(text="Parsing Job Description..."))
                    parsed_jd, jd_embedding = await asyncio.gather(
                        asyncio.to_thread(parse_jd_with_ai, jd_text, client),
                        asyncio.to_thread(get_embedding, jd_text, client),
                    )
                    if not parsed_jd:
                        jd_embedding = None
                    if parsed_jd and jd_embedding:
                        try:
                            atomic_write_json(cache_file, {
                                "jd_hash": jd_hash,
                                "embedding": jd_embedding,
                                "parsed_jd": parsed_jd.model_dump()
                            })
                        except Exception as cache_err:
                            print(f"JD cache write error: {cache_err}")

            total = len(files)
            t0 = time.time()
            pre_read_resumes = []
            
            def finish_read(filename, filepath, raw_text_fn):
                file_hash = get_file_hash(filepath)
                try:
                    raw_text = raw_text_fn()
                    if not raw_text or not raw_text.strip(): return filename, None, file_hash
                    clean_text = preprocess_resume_text(raw_text)
                    return filename, clean_text, file_hash
                except Exception: return filename, None, file_hash

            def read_worker(args):
                filename, filepath = args
                return finish_read(filename, filepath, lambda: extract_text_from_file(filepath))

            def doc_batch_worker(doc_items):
                # One Word instance for all .doc files; hash + preprocess like read_worker
                try:
                    texts = extract_doc_batch([fp for _, fp in doc_items])
                except Exception as batch_err:
                    print(f"[-] .doc batch extraction failed ({len(doc_items)} file(s)): {batch_err}")
                    traceback.print_exc()
                    texts = {}  # every .doc in the batch becomes a Read Error row
                return [finish_read(fn, fp, lambda fp=fp: texts.get(fp)) for fn, fp in doc_items]

            results_dict = {}
            hashes_dict = {}
            doc_items = [(f, os.path.join(folder, f)) for f in files if f.lower().endswith('.doc')]
            other_files = [f for f in files if not f.lower().endswith('.doc')]
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as read_executor:
                # Submit the slow serial Word batch FIRST so it overlaps the per-file reads
                futures = []
                if doc_items:
                    futures.append(read_executor.submit(doc_batch_worker, doc_items))
                futures.extend(read_executor.submit(read_worker, (f, os.path.join(folder, f))) for f in other_files)
                read_done = 0
                for future in concurrent.futures.as_completed(futures):
                    res = future.result()
                    batch_results = res if isinstance(res, list) else [res]
                    for fname, clean_text, fhash in batch_results:
                        results_dict[fname] = clean_text
                        hashes_dict[fname] = fhash
                        read_done += 1
                        self._post(lambda c=read_done: self._queue_status_update(text=f"Reading files... ({c}/{total})"))
                        self._post(lambda p=phase_progress("read", read_done, total): self._queue_status_update(progress=p))

            for filename in files:
                pre_read_resumes.append({"filename": filename, "clean_text": results_dict.get(filename),
                                         "file_hash": hashes_dict.get(filename)})
            read_time = time.time() - t0

            # Resume cache setup (file hashes were computed in read_worker)
            resume_cache = {}
            if os.path.exists(RESUME_CACHE_FILE):
                try:
                    with open(RESUME_CACHE_FILE, "r", encoding="utf-8") as f:
                        resume_cache = json.load(f)
                except Exception: pass
            if not isinstance(resume_cache, dict):
                resume_cache = {}
            if not isinstance(resume_cache.get("match_cache"), dict):
                resume_cache["match_cache"] = {}
            match_cache = resume_cache["match_cache"]
            cache_dirty = False
            cache_write_lock = threading.Lock()  # every resume_cache.json write goes through it

            def snapshot_resume_cache():
                """Loop thread: cheap two-level copy. Per-file entries and their
                candidate_data dicts are copied (the match step updates them); the big
                values (embeddings, clean_text, sections) are shared because they are
                never mutated in place."""
                snap = {}
                for key, entry in resume_cache.items():
                    if key == "match_cache":
                        snap[key] = dict(match_cache)
                    elif isinstance(entry, dict):
                        entry = dict(entry)
                        if isinstance(entry.get("candidate_data"), dict):
                            entry["candidate_data"] = dict(entry["candidate_data"])
                        snap[key] = entry
                    else:
                        snap[key] = entry
                return snap

            def write_snapshot(snapshot, reason):
                """Worker thread: serialise and write; a failed write re-marks the cache dirty."""
                nonlocal cache_dirty
                with cache_write_lock:
                    try:
                        atomic_write_json(RESUME_CACHE_FILE, snapshot)
                    except Exception as cache_err:
                        print(f"Failed to write resume cache ({reason}): {cache_err}")
                        cache_dirty = True  # a later checkpoint or the final write retries

            async def write_resume_cache(reason):
                """Persist resume_cache if dirty without blocking the event loop."""
                nonlocal cache_dirty
                if not cache_dirty:
                    return
                snapshot = snapshot_resume_cache()
                cache_dirty = False
                await asyncio.to_thread(write_snapshot, snapshot, reason)

            resumes_to_embed = []
            extracted_candidate_data = {}
            for resume in pre_read_resumes:
                filename, file_hash = resume["filename"], resume["file_hash"]
                if resume["clean_text"] is None:
                    error_row = self.create_error_row(filename, {}, "Read Error")
                    error_row["Status"] = "Read Error"
                    error_row["ResumeLink"] = "Not uploaded"
                    error_row["Parsed"] = False
                    self._post(lambda r=error_row: self.add_or_update_candidate(r))
                    
                    self.processing_records.append({
                        "filename": filename,
                        "status": "removed",
                        "reason": "Invalid Resume",
                        "resolution_method": "Unresolved",
                        "email_found": "No",
                        "phone_found": "No"
                    })
                    continue
                # Every readable file gets a table row now; extraction fills it in
                self._post(lambda f=filename: self.add_or_update_candidate(
                    {"SourceFilename": f, "Name": f, "Status": "Parsing\u2026", "Parsed": False, "ResumeLink": "",
                     "MatchPercentage": ""}))
                if file_hash in resume_cache:
                    cached = resume_cache[file_hash]
                    resume.update({"clean_text": cached.get("clean_text", resume["clean_text"]), "res_embedding": cached.get("res_embedding"), "sections": cached.get("sections")})
                    # Candidate data from an older extractor is re-extracted
                    if (cached.get("extract_version") == EXTRACT_VERSION and cached.get("candidate_data")
                            and not cached["candidate_data"].get("ExtractionError")):
                        extracted_candidate_data[filename] = cached.get("candidate_data")
                else:
                    resume["res_embedding"] = None
                    resume["sections"] = parse_resume_sections(resume["clean_text"])
                resumes_to_embed.append(resume)

            # API Embeddings
            t0_embed = time.time()
            if parsed_jd and resumes_to_embed:
                resumes_needing_embed = [r for r in resumes_to_embed if r.get("res_embedding") is None]
                if resumes_needing_embed:
                    total_to_embed = len(resumes_needing_embed)
                    self._post(lambda n=total_to_embed: self._queue_status_update(text=f"Computing similarities... (0/{n})"))
                    chunk_size = 50
                    chunks = [resumes_needing_embed[i:i + chunk_size] for i in range(0, len(resumes_needing_embed), chunk_size)]
                    chunks_done = 0

                    async def embed_chunk(chunk):
                        nonlocal chunks_done
                        embs = await get_embeddings_batch([r["clean_text"][:8192] for r in chunk], async_client, semaphore=llm_sem)
                        chunks_done += 1
                        self._post(lambda p=phase_progress("embed", chunks_done, len(chunks)): self._queue_status_update(progress=p))
                        return embs
                    chunk_embs = await asyncio.gather(*(embed_chunk(chunk) for chunk in chunks))
                    for chunk, embs in zip(chunks, chunk_embs):
                        for r, emb in zip(chunk, embs): r["res_embedding"] = emb
                for r in resumes_to_embed:
                    r["similarity"] = cosine_similarity(jd_embedding, r["res_embedding"]) if jd_embedding and r.get("res_embedding") else 1.0
            else:
                for r in resumes_to_embed: r["similarity"] = 1.0
            self._post(lambda p=phase_progress("embed", 1, 1): self._queue_status_update(progress=p))
            embed_time = time.time() - t0_embed

            # Extract Data for all resumes
            t0 = time.time()
            self._post(lambda: self._queue_status_update(text="Extracting candidate data..."))
            candidates_needing_extract = [r for r in resumes_to_embed if r["filename"] not in extracted_candidate_data]
            extracts_done = 0
            records_by_file = {}         # filename -> processing record (CSV keeps file order)
            candidates_to_extract = []   # valid, unfiltered candidates: uploaded and scored

            def is_invalid_value(val):
                return val is None or str(val).strip() in ("", "Not Found", "API Error", "Read Error", "Filtered", "None")

            def finalize_candidate(resume):
                """Loop thread: validate one extracted resume, show its row and, for a
                valid candidate, queue its upload. Runs as soon as that file's data is
                ready, so rows fill in while other files are still being extracted."""
                filename = resume["filename"]
                cand_data = extracted_candidate_data.get(filename)

                if not cand_data or any(cand_data.get(k) == "API Error" for k in ("Name", "Email", "Phone", "Country")):
                    records_by_file[filename] = {
                        "filename": filename,
                        "status": "removed",
                        "reason": "Extraction Failure",
                        "resolution_method": "Unresolved",
                        "email_found": "No",
                        "phone_found": "No"
                    }
                    print(f"[Data Quality] Candidate from file {filename} removed. Reason: Extraction Failure")
                    failed_row = self.create_error_row(filename, {}, "API Error")
                    failed_row.update({"Status": "Extraction Failed", "ResumeLink": "Not uploaded", "MatchPercentage": "N/A", "Parsed": False})
                    self._post(lambda r=failed_row: self.add_or_update_candidate(r))
                    return

                filepath = os.path.join(folder, filename)
                review = getattr(self, '_review_store', None)
                if isinstance(review,ReviewStore):
                    cand_data = review.apply(dict(cand_data,FileHash=resume['file_hash']))
                normalized_cand, reason = normalize_and_validate_candidate(cand_data, resume["clean_text"], filepath=filepath)

                email_found = "No" if is_invalid_value(normalized_cand.get("Email")) else "Yes"
                phone_found = "No" if is_invalid_value(normalized_cand.get("Phone")) else "Yes"
                resolution_method = normalized_cand.get("CountryResolutionMethod", "Unresolved")

                if reason != "Valid":
                    records_by_file[filename] = {
                        "filename": filename,
                        "status": "removed",
                        "reason": reason,
                        "resolution_method": resolution_method,
                        "email_found": email_found,
                        "phone_found": phone_found
                    }
                    print(f"[Data Quality] Candidate from file {filename} removed. Reason: {reason}")
                    if reason == "Missing Name":
                        self.removal_missing_name += 1
                    elif reason == "Missing Contact Information":
                        self.removal_missing_contact += 1
                    elif reason == "Country Unresolved":
                        self.removal_country_unresolved += 1
                    # Shown with what was parsed, but not uploaded or exported
                    removed_row = dict(normalized_cand)
                    removed_row.update({"SourceFilename": filename, "Status": reason, "ResumeLink": "Not uploaded",
                                        "MatchPercentage": "N/A", "Parsed": True, "FileHash": resume["file_hash"]})
                    if cand_data.get("ExtractionError"):
                        removed_row["Status"] = f"AI unavailable · {reason}"
                        removed_row["Summary"] = cand_data["ExtractionError"]
                    self._post(lambda r=removed_row: self.add_or_update_candidate(r))
                    return

                normalized_cand.update(SourceFilename=filename, FileHash=resume["file_hash"], Parsed=True)
                extracted_candidate_data[filename] = normalized_cand
                records_by_file[filename] = {
                    "filename": filename,
                    "status": "valid",
                    "reason": "Valid",
                    "resolution_method": resolution_method,
                    "email_found": email_found,
                    "phone_found": phone_found
                }

                # Similarity pre-filter applies only to valid candidates
                if parsed_jd and resume.get("similarity", 1.0) < 0.20 and filename not in getattr(self, "_evaluate_anyway", set()):
                    filtered_row = dict(normalized_cand)
                    filtered_row.update(SourceFilename=filename, ResumeLink='Not uploaded',
                        MatchPercentage='Not evaluated', Status='Not evaluated', Parsed=True,
                        FileHash=resume['file_hash'],
                        Summary=f"JD similarity {resume.get('similarity', 1.0):.2f}. Choose Evaluate anyway to score and upload.")
                    self._post(lambda r=filtered_row: self.add_or_update_candidate(r))
                    return
                candidates_to_extract.append(resume)

                # Start the upload now (before scoring) and show the parsed row.
                early_row = dict(normalized_cand)
                early_row.update({"SourceFilename": filename, "Status": "Parsed", "ResumeLink": "Pending",
                                  "ResumeDriveURL": "",
                                  "MatchPercentage": "Scoring\u2026" if parsed_jd else "N/A"})
                if cand_data.get("ExtractionError"):
                    early_row["Status"] = "Parsed (AI unavailable)"
                self._post(lambda r=early_row: self.add_or_update_candidate(r))
                reuse = getattr(self,'_reuse_uploads',{})
                uploaded = isinstance(reuse,dict) and reuse.get(filename) and reuse[filename][0] == resume['file_hash']
                if not uploaded:
                    self._post(lambda: self._record_upload_counts(total=1))
                    self.upload_queue.put((run_gen, filename, filepath, resume["file_hash"]))

            def safe_finalize(resume):
                """One candidate's validation error must never abort the run."""
                try:
                    finalize_candidate(resume)
                except Exception as fin_err:
                    print(f"Finalizing {resume['filename']} failed: {fin_err}")
                    traceback.print_exc()
                    extracted_candidate_data[resume["filename"]] = None
                    finalize_candidate(resume)  # recorded and shown as an extraction failure

            # Cached candidates are ready now
            for r in resumes_to_embed:
                if r["filename"] in extracted_candidate_data:
                    safe_finalize(r)

            async def run_extract_task(resume):
                nonlocal extracts_done
                try:
                    det_data = deterministic_extract(resume["clean_text"], resume["filename"])
                    # Country is never found deterministically (it is inferred during
                    # normalization), so only the other fields trigger the AI fallback.
                    if any(det_data[k] == "Not Found" for k in ("Name", "Email", "Phone", "YearsOfExperience")):
                        llm_data = await extract_data_with_ai_async(resume["clean_text"], resume["filename"], async_client,
                                                                  model=model, semaphore=llm_sem)
                        if any(llm_data.get(k) == "API Error" for k in ("Name", "Email", "Phone", "Country")):
                            det_data["ExtractionError"] = llm_data.get("ExtractionError") or "OpenAI extraction failed. Check the API key and connection, then retry."
                        for k in ["Name", "Email", "Phone", "Country", "YearsOfExperience"]:
                            value = llm_data.get(k)
                            if not is_invalid_value(value):
                                det_data[k] = value
                    extracted_candidate_data[resume["filename"]] = det_data
                except Exception as ext_err:
                    print(f"Extraction failed for {resume['filename']}: {ext_err}")
                    extracted_candidate_data[resume["filename"]] = {
                        "Name": "API Error",
                        "Email": "API Error",
                        "Phone": "API Error",
                        "Country": "API Error",
                        "SourceFilename": resume["filename"]
                    }
                finally:
                    extracts_done += 1
                    self._post(lambda p=phase_progress("extract", extracts_done, len(candidates_needing_extract)):
                               self._queue_status_update(progress=p))
                safe_finalize(resume)
            await asyncio.gather(*(run_extract_task(r) for r in candidates_needing_extract))
            self._post(lambda p=phase_progress("extract", 1, 1): self._queue_status_update(progress=p))
            extract_time = time.time() - t0

            # Checkpoint new resumes (text, sections, embedding, extracted data) before
            # matching, so closing the app mid-match keeps the embed/extract work.
            new_cache_entries = []  # (file_hash, filename) added or re-extracted by this run
            reextracted = {r["filename"] for r in candidates_needing_extract}
            for resume in resumes_to_embed:
                file_hash = resume["file_hash"]
                filename = resume["filename"]
                if file_hash not in resume_cache or filename in reextracted:
                    cache_dirty = True
                    resume_cache[file_hash] = {
                        "clean_text": resume["clean_text"],
                        "res_embedding": resume["res_embedding"],
                        "sections": resume["sections"],
                        "candidate_data": extracted_candidate_data.get(filename),
                        "extract_version": EXTRACT_VERSION
                    }
                    new_cache_entries.append((file_hash, filename))
            await write_resume_cache("post-extract")

            # Processing records in file order (rows were already posted by finalize_candidate)
            self.processing_records.extend(records_by_file[r["filename"]] for r in resumes_to_embed
                                           if r["filename"] in records_by_file)

            # Calculate stats and write CSV
            total_resumes = len(self.processing_records)
            valid_count = sum(1 for r in self.processing_records if r["status"] == "valid")
            removed_count = sum(1 for r in self.processing_records if r["status"] == "removed")
            
            missing_name = sum(1 for r in self.processing_records if r["reason"] == "Missing Name")
            missing_contact = sum(1 for r in self.processing_records if r["reason"] == "Missing Contact Information")
            country_unresolved = sum(1 for r in self.processing_records if r["reason"] == "Country Unresolved")
            extraction_failed = sum(1 for r in self.processing_records if r["reason"] == "Extraction Failure")
            invalid_resume = sum(1 for r in self.processing_records if r["reason"] == "Invalid Resume")
            
            both_count = 0
            email_only_count = 0
            phone_only_count = 0
            neither_count = 0
            for r in self.processing_records:
                if r["status"] == "valid":
                    if r["email_found"] == "Yes" and r["phone_found"] == "Yes":
                        both_count += 1
                    elif r["email_found"] == "Yes":
                        email_only_count += 1
                    elif r["phone_found"] == "Yes":
                        phone_only_count += 1
                elif r["reason"] == "Missing Contact Information":
                    neither_count += 1
            
            ai_extraction_count = 0
            phone_inference_count = 0
            location_inference_count = 0
            unresolved = 0
            for r in self.processing_records:
                if r["status"] == "valid" or r["reason"] == "Country Unresolved":
                    m = r["resolution_method"]
                    if m == "AI Extraction":
                        ai_extraction_count += 1
                    elif m == "Phone Inference":
                        phone_inference_count += 1
                    elif m == "Location Inference":
                        location_inference_count += 1
                    elif m == "Unresolved":
                        unresolved += 1
            
            denom = total_resumes if total_resumes > 0 else 1
            retention_rate = (valid_count / denom) * 100
            country_resolution_rate = ((total_resumes - country_unresolved) / denom) * 100
            contact_availability_rate = ((total_resumes - neither_count) / denom) * 100
            
            reasons_map = {
                "Missing Name": missing_name,
                "Missing Contact Information": missing_contact,
                "Country Unresolved": country_unresolved,
                "Extraction Failure": extraction_failed,
                "Invalid Resume": invalid_resume
            }
            if removed_count == 0:
                top_reason = "None"
                top_reason_count = 0
            else:
                top_reason = max(reasons_map, key=reasons_map.get)
                top_reason_count = reasons_map[top_reason]
                
            pdf_count = sum(1 for f in files if f.lower().endswith('.pdf'))
            docx_count = sum(1 for f in files if f.lower().endswith('.docx'))
            doc_count = sum(1 for f in files if f.lower().endswith('.doc'))
            unsupported_count = len(skipped_files)

            stats_summary = {
                "total_resumes": total_resumes,
                "valid_count": valid_count,
                "removed_count": removed_count,
                "top_reason": top_reason,
                "top_reason_count": top_reason_count,
                "retention_rate": retention_rate,
                "country_resolution_rate": country_resolution_rate,
                "contact_availability_rate": contact_availability_rate,
                "pdf_count": pdf_count,
                "docx_count": docx_count,
                "doc_count": doc_count,
                "unsupported_count": unsupported_count,
                "skipped_files": skipped_files,
                "ai_extraction": ai_extraction_count,
                "phone_inference": phone_inference_count,
                "location_inference": location_inference_count,
                "unresolved_country": unresolved
            }
            
            # Print processing summary to console/logs
            print("\n" + "="*50)
            print("PROCESSING SUMMARY REPORT")
            print("="*50)
            print(f"Total Resumes: {total_resumes}")
            print(f"Valid Candidates: {valid_count}")
            print(f"Removed Candidates: {removed_count}\n")
            print("Removal Breakdown:")
            print(f"Missing Name: {missing_name}")
            print(f"Missing Contact Information: {missing_contact}")
            print(f"Country Unresolved: {country_unresolved}")
            print(f"Extraction Failed: {extraction_failed}")
            print(f"Invalid Resume: {invalid_resume}\n")
            print("Country Resolution Statistics:")
            print(f"AI Extraction: {ai_extraction_count}")
            print(f"Phone Inference: {phone_inference_count}")
            print(f"Location Inference: {location_inference_count}")
            print(f"Unresolved: {unresolved}\n")
            print("Contact Statistics:")
            print(f"Both Email and Phone: {both_count}")
            print(f"Email Only: {email_only_count}")
            print(f"Phone Only: {phone_only_count}")
            print(f"Neither: {neither_count}\n")
            print("File Type Statistics:")
            print(f"PDF: {pdf_count}")
            print(f"DOCX: {docx_count}")
            print(f"DOC: {doc_count}")
            print(f"Unsupported: {unsupported_count}\n")
            
            if skipped_files:
                print("Skipped Files")
                for fname, reason in skipped_files:
                    print(fname)
                    print(reason)
                print()
            print("="*50 + "\n")
            # Export processing_summary.csv
            import csv
            report_path = os.path.join(folder, "processing_summary.csv")
            try:
                with open(report_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "Candidate Filename", "Status", "Reason", 
                        "Country Resolution Method", "Email Found", "Phone Found"
                    ])
                    for rec in self.processing_records:
                        writer.writerow([
                            rec["filename"],
                            rec["status"],
                            rec["reason"],
                            rec["resolution_method"],
                            rec["email_found"],
                            rec["phone_found"]
                        ])
                print(f"[Reporting] Saved processing summary CSV to: {report_path}")
            except Exception as csv_err:
                print(f"[Reporting] Failed to write processing summary CSV: {csv_err}")

            if not candidates_to_extract:
                self._post(lambda s=stats_summary: self.extraction_finished(total, folder, [], start_time, stats_summary=s))
                return

            # Scoring
            jd_skills = parsed_jd.mandatory_technologies + parsed_jd.required_skills if parsed_jd else []
            for resume in candidates_to_extract:
                overlap = compute_skills_overlap(jd_skills, resume["sections"].get("SKILLS", ""))
                resume["stage1_score"] = (resume.get("similarity", 1.0) * 0.50) + (overlap * 0.50)
                resume["skills_overlap"] = overlap
            candidates_to_extract.sort(key=lambda x: x.get("stage1_score", 0.0), reverse=True)

            # Stage 2 Setup (STAGE2_TOP_N / STAGE2_THRESHOLD are module constants)
            for idx, resume in enumerate(candidates_to_extract):
                resume["stage2_selected"] = (idx < STAGE2_TOP_N or resume.get("stage1_score", 0.0) >= STAGE2_THRESHOLD)

            # Point this run's new cache entries at the normalized candidate dict, which
            # the match step updates, so mid-run checkpoints and the final write carry
            # the match results.
            for file_hash, filename in new_cache_entries:
                cand = extracted_candidate_data.get(filename)
                if resume_cache[file_hash].get("candidate_data") is not cand:
                    resume_cache[file_hash]["candidate_data"] = cand
                    cache_dirty = True

            # Evaluate Matches
            t0 = time.time()
            n_match = len(candidates_to_extract)
            matches_done = 0
            last_checkpoint_at = 0
            checkpoint_in_flight = False
            self._post(lambda: self._queue_status_update(text=f"Evaluating matches (0/{n_match})..."))
            self._post(lambda p=phase_progress("match", 0, n_match): self._queue_status_update(progress=p))
            
            batch_results_dict = {}

            def post_match_row(filename, cand_data):
                batch_results_dict[filename] = cand_data
                # Upload state (ResumeLink/ResumeDriveURL) is owned by the early row and
                # the upload handlers; the match update must never carry it (cached
                # candidate dicts may hold stale values).
                match_row = {k: v for k, v in cand_data.items() if k not in ("ResumeLink", "ResumeDriveURL")}
                match_row.setdefault("SourceFilename", filename)
                self._post(lambda c=match_row: self.add_or_update_candidate(c))
            
            async def run_match_task(resume):
                # One candidate's failure must never abort the gather (and the run).
                nonlocal cache_dirty, matches_done, last_checkpoint_at, checkpoint_in_flight
                filename = resume.get("filename")
                try:
                    cand_data = extracted_candidate_data[filename]
                    sim = resume.get("similarity", 1.0)

                    if resume["stage2_selected"] and parsed_jd:
                        # Token-optimized structured profile
                        sections = resume["sections"]
                        profile = f"""### CANDIDATE PROFILE
Name: {cand_data.get('Name')}
Years of Experience: {cand_data.get('YearsOfExperience')}

[SKILLS]
{sections.get('SKILLS')}

[EXPERIENCE]
{sections.get('EXPERIENCE')[:4000]}

[EDUCATION]
{sections.get('EDUCATION')}

[PROJECTS]
{sections.get('PROJECTS')[:2000]}
"""
                        mkey = match_cache_key(resume["file_hash"], jd_hash, model)
                        cached_match = match_cache.get(mkey)
                        if cached_match is not None:
                            cand_data.update(dict(cached_match))
                        else:
                            match_data = await evaluate_candidate_match_async(profile, parsed_jd, cand_data, async_client, model, semaphore=llm_sem)
                            cand_data.update(match_data)
                            if not str(match_data.get("Summary", "")).startswith("Evaluation Failed"):
                                match_cache[mkey] = dict(match_data)
                                cache_dirty = True
                    else:
                        # Stage 1 fallback score
                        overlap = resume.get("skills_overlap", 0.0)
                        match_pct = int(resume["stage1_score"] * 100) if parsed_jd else "N/A"
                        cand_data.update({
                            "MatchPercentage": match_pct,
                            "TechnicalMatch": int(overlap * 100) if parsed_jd else 0,
                            "ExperienceMatch": int(sim * 100) if parsed_jd else 0,
                            "DomainMatch": int(sim * 100) if parsed_jd else 0,
                            "Strengths": ["Good skills match" if overlap > 0.3 else "General match"],
                            "Weaknesses": ["Bypassed deep evaluation due to lower Stage 1 ranking" if parsed_jd else "No JD provided"],
                            "MissingRequirements": [],
                            "Summary": f"Auto-scored based on skills overlap ({int(overlap*100)}%) and embedding similarity ({int(sim*100)}%)." if parsed_jd else "No JD provided.",
                            "SkillEvidence": []
                        })

                    post_match_row(filename, cand_data)
                except Exception as e:
                    print(f"Match task failed for {filename}: {e}")
                    traceback.print_exc()
                    cand_data = extracted_candidate_data.get(filename)
                    if not isinstance(cand_data, dict):
                        cand_data = {"SourceFilename": filename}
                    cand_data.update({
                        "MatchPercentage": "Not evaluated", "TechnicalMatch": None, "ExperienceMatch": None, "DomainMatch": None,
                        "Strengths": [], "Weaknesses": [], "MissingRequirements": [],
                        "Summary": f"Evaluation Failed: {e}", "SkillEvidence": []
                    })
                    post_match_row(filename, cand_data)
                finally:
                    # Counted in finally so failures advance progress too. A checkpoint is
                    # due once CACHE_CHECKPOINT_EVERY match tasks (cached, deep-evaluated,
                    # Stage-1 or failed alike) completed since the last one; it is skipped
                    # while another is in flight (taken at a later completion instead) and
                    # writes only when the cache is dirty.
                    matches_done += 1
                    self._post(lambda p=phase_progress("match", matches_done, n_match): self._queue_status_update(progress=p))
                    self._post(lambda c=matches_done: self._queue_status_update(text=f"Evaluating matches ({c}/{n_match})..."))
                    if (not checkpoint_in_flight
                            and matches_done - last_checkpoint_at >= CACHE_CHECKPOINT_EVERY):
                        checkpoint_in_flight = True
                        last_checkpoint_at = matches_done
                        try:
                            await write_resume_cache("checkpoint")
                        finally:
                            checkpoint_in_flight = False

            await asyncio.gather(*(run_match_task(r) for r in candidates_to_extract))
            match_time = time.time() - t0

            # Step 10: final resume cache write. gather() has awaited every checkpoint,
            # so none is in flight; this always runs and takes the same lock.
            await write_resume_cache("final")

            total_elapsed = time.time() - start_time
            
            # Print performance report
            print("\n" + "="*50)
            print("PERFORMANCE PROFILING REPORT")
            print("="*50)
            print(f"File Read/Preprocess Time: {read_time:.2f}s")
            print(f"Embedding Generation Time: {embed_time:.2f}s")
            print(f"Candidate Data Extraction: {extract_time:.2f}s")
            print(f"Match Evaluation Time:    {match_time:.2f}s")
            print(f"Total Runtime:            {total_elapsed:.2f}s")
            print("="*50)
            print("DATA QUALITY REMOVAL SUMMARY")
            print("="*50)
            print(f"Missing Name:               {self.removal_missing_name}")
            print(f"Missing Contact Info:       {self.removal_missing_contact}")
            print(f"Country Unresolved:         {self.removal_country_unresolved}")
            print(f"Total Candidates Removed:   {self.removal_missing_name + self.removal_missing_contact + self.removal_country_unresolved}")
            print("="*50 + "\n")

            non_filtered_filenames = [r["filename"] for r in candidates_to_extract]
            self._post(lambda names=non_filtered_filenames: self.extraction_finished(total, folder, names, start_time))

        except Exception as e:
            traceback.print_exc()
            # The modal is deferred out of the drain so UI ticks keep running while it is open.
            self._post(lambda msg=f"Pipeline failed: {e}": self.after(0, lambda m=msg: messagebox.showerror("Error", m)))
            self._post(lambda msg=str(e): self._pipeline_failed(msg))

    def _pipeline_failed(self, message):
        self._pending_progress = None
        self._pending_status = None
        self._status_dirty = False
        self._pipeline_error = message
        self._processing_report = f"Processing failed: {message}"
        self._log_activity(f"Processing failed: {message}")
        self._pipeline_stopped()
        self._refresh_run_state()
        self._update_activity_report()

    def _pipeline_stopped(self):
        """Main thread: the run's parse/match pipeline ended (uploads may continue)."""
        self._pipeline_running = False
        pending = self._has_pending_uploads() is True
        self._set_source_controls("disabled" if pending else "normal")
        self.chk_jd_matching.configure(state="disabled" if pending else "normal")
        self.btn_extract.configure(state="disabled" if pending else "normal",
                                   text="Uploading…" if pending else self._start_button_text())
        self._progress_dirty = True

    def update_progress(self, p, c, total):
        self.set_matching_progress(p)
        self.lbl_status.configure(text=f"Processing ({c}/{total})")

    def process_single_file(self, clean_text, filename, candidate_data, similarity, parsed_jd, client, model):
        if candidate_data is None:
            return self.create_error_row(filename, {}, "API Error")

        if not parsed_jd:
            candidate_data.update({
                "MatchPercentage": "N/A", "MissingRequirements": [], "Strengths": [],
                "Weaknesses": [], "Summary": "No JD provided.", "SkillEvidence": []
            })
            return candidate_data

        # Match Evaluation with preprocessed text
        match_data = evaluate_candidate_match(clean_text, parsed_jd, candidate_data, client, model)
        candidate_data.update(match_data)
        return candidate_data

    def create_error_row(self, filename, links_dict, msg):
        return {
            "Name": msg, "Email": msg, "Phone": msg, "Country": msg, "YearsOfExperience": msg,
            "SourceFilename": filename, "ResumeLink": "Failed", "Status": msg,
            "MatchPercentage": "Error",
            "MissingRequirements": [], "Strengths": [], "Weaknesses": [],
            "Summary": msg, "SkillEvidence": [], "Parsed": False
        }

    def add_to_tree(self, row):
        missing = row.get("MissingRequirements", [])
        missing_str = ", ".join(missing) if isinstance(missing, list) else str(missing)
        if row.get("ResumeLink") == "Not uploaded":
            upload_text, tags = row.get("Status", "Needs review"), ("muted",)
        else:
            upload_text, tags = upload_status_display(row.get("ResumeLink") or row.get("Status", ""))
        values = (
            row.get("Name", ""),
            row.get("Status", ""),
            row.get("Email", ""),
            row.get("Phone", ""),
            row.get("Country", ""),
            row.get("YearsOfExperience", ""),
            match_display(row.get("MatchPercentage")),
            missing_str,
            upload_text,
        )
        iid = row.get("SourceFilename")
        if iid:
            if self.tree.exists(iid):
                if (self.tree.item(iid, "values") != tuple(str(v) for v in values)
                        or tuple(self.tree.item(iid, "tags")) != tags):
                    self.tree.item(iid, values=values, tags=tags)
                return iid
            return self.tree.insert("", tk.END, iid=iid, values=values, tags=tags)
        else:
            return self.tree.insert("", tk.END, values=values, tags=tags)

    def add_or_update_candidate(self, candidate_data):
        """Main thread only. Background threads must go through _post()."""
        review = getattr(self, '_review_store', None)
        if isinstance(review, ReviewStore):
            candidate_data = review.apply(candidate_data)
        filename = candidate_data.get("SourceFilename")
        if not filename:
            self.extracted_data.append(candidate_data)
            print("[Logging] Candidate added: Unknown")
            self._table_dirty = True
            return

        existing = self.candidates_by_file.get(filename)

        if existing is not None:
            old_link = existing.get("ResumeLink", "Pending...")
            old_url = existing.get("ResumeDriveURL", "")
            same_source = not candidate_data.get("FileHash") or not existing.get("FileHash") or candidate_data["FileHash"] == existing["FileHash"]
            new_link = candidate_data.get("ResumeLink")
            
            existing.update(candidate_data)
            if isinstance(review, ReviewStore): existing.update(review.apply(existing))
            
            if same_source and (not new_link or new_link == "Pending"):
                if old_link not in ("Pending", ""):
                    existing["ResumeLink"] = old_link
                    existing["ResumeDriveURL"] = old_url
            
            print(f"[Logging] Candidate updated: {filename}")
        else:
            self.extracted_data.append(candidate_data)
            self.candidates_by_file[filename] = candidate_data
            print(f"[Logging] Candidate added: {filename}")

        self._table_dirty = True

    def start_upload_worker(self):
        """Start the upload pool (once per app): UPLOAD_WORKERS daemon threads sharing
        one queue, one set of Drive credentials and one in-memory drive cache."""
        if getattr(self, "_upload_workers", None):
            return
        import drive_uploader

        cache = {}
        if os.path.exists(drive_uploader.DRIVE_CACHE_FILE):
            try:
                with open(drive_uploader.DRIVE_CACHE_FILE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception as e:
                print(f"[-] Failed to read drive cache: {e}. Starting with empty cache.")
                cache = {}
        if not isinstance(cache, dict):
            print("[-] Drive cache is not a JSON object. Starting with empty cache.")
            cache = {}

        self.upload_queue = queue.Queue()
        self._drive_creds = None
        self._drive_creds_lock = threading.Lock()   # guards _drive_creds; never held across sign-in
        self._drive_signin_lock = threading.Lock()  # serialises sign-in (workers wait for the first fetch)
        self._drive_auth_failed: bool = False  # circuit breaker; reset by retry/start
        self._drive_cache = cache
        self._drive_cache_lock = threading.Lock()       # guards _drive_cache and _uploads_since_save
        self._drive_cache_save_lock = threading.RLock()  # orders snapshot+write so saves never go backwards
        self._uploads_since_save = 0
        self._inflight_hashes = {}  # file_hash -> Event set when that upload ends; under _drive_cache_lock
        self._upload_workers = [
            threading.Thread(target=self.upload_worker_loop, daemon=True, name=f"upload-worker-{i}")
            for i in range(UPLOAD_WORKERS)
        ]
        for t in self._upload_workers:
            t.start()

    def _get_shared_drive_creds(self):
        """Fetch Drive credentials once for all workers (others wait for the first fetch).
        On failure nothing is memoised and the auth circuit breaker opens: later jobs
        fail fast instead of re-running sign-in until retry/start resets it.
        _drive_creds_lock is only held for reads/writes of _drive_creds (never across
        the sign-in), so the main thread can reset it without waiting on the network."""
        import drive_uploader
        with self._drive_creds_lock:
            if self._drive_creds is not None:
                return self._drive_creds
        with self._drive_signin_lock:
            with self._drive_creds_lock:
                if self._drive_creds is not None:  # another worker signed in meanwhile
                    return self._drive_creds
                if self._drive_auth_failed:
                    raise RuntimeError(DRIVE_AUTH_FAILED_MSG)
            try:
                creds = drive_uploader.get_drive_credentials()
            except Exception:
                self._drive_auth_failed = True
                raise
            with self._drive_creds_lock:
                self._drive_creds = creds
            return creds

    def _save_drive_cache(self, force: bool = False) -> None:
        """Persist the drive cache when force or DRIVE_CACHE_SAVE_EVERY uploads are unsaved.
        No-op when nothing is unsaved. Never raises (must not kill a worker)."""
        import drive_uploader
        with self._drive_cache_save_lock:
            with self._drive_cache_lock:
                unsaved = self._uploads_since_save
                if unsaved == 0 or (not force and unsaved < DRIVE_CACHE_SAVE_EVERY):
                    return
                snapshot = dict(self._drive_cache)
                self._uploads_since_save = 0
            try:
                atomic_write_json(drive_uploader.DRIVE_CACHE_FILE, snapshot)
            except Exception as e:
                print(f"[-] Drive cache saving failed: {e}")
                with self._drive_cache_lock:
                    self._uploads_since_save += unsaved  # retry on a later save

    def _claim_upload(self, file_hash):
        """Worker thread: dedupe uploads of the same content across workers.

        Returns (link, None) on a drive-cache hit, else (None, claim) where claim is
        the Event this job registered in _inflight_hashes (the caller must release it),
        or None when there is no hash or another upload of the same hash is still
        running after INFLIGHT_UPLOAD_WAIT_S (the caller then uploads unclaimed).
        A job finding its hash in flight waits (no lock held) and re-checks: a cache
        hit if the first upload succeeded, otherwise it claims and uploads itself."""
        deadline = time.monotonic() + INFLIGHT_UPLOAD_WAIT_S
        while True:
            with self._drive_cache_lock:
                link = self._drive_cache.get(file_hash) if file_hash else None
                if link or not file_hash:
                    return link, None
                other = self._inflight_hashes.get(file_hash)
                if other is None:
                    claim = threading.Event()
                    self._inflight_hashes[file_hash] = claim
                    return None, claim
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, None
            other.wait(remaining)

    def _release_upload_claim(self, file_hash, claim) -> None:
        """Worker thread: end this job's in-flight claim and wake jobs waiting on it."""
        with self._drive_cache_lock:
            if self._inflight_hashes.get(file_hash) is claim:
                del self._inflight_hashes[file_hash]
        claim.set()

    def upload_worker_loop(self):
        """One of UPLOAD_WORKERS threads. Owns its Drive service (httplib2 is not
        thread-safe); talks to the UI only via _post. A None job stops the thread.
        Jobs are (run_gen, filename, filepath, file_hash); jobs queued by an earlier
        run are dropped, and every UI event carries the job's run_gen."""
        import drive_uploader

        service = None        # this thread's Drive service, built on first cache miss
        service_creds = None  # the credentials object `service` was built from
        while True:
            job = self.upload_queue.get()
            if job is None:
                self.upload_queue.task_done()
                break

            filename = None
            gen = None
            file_hash = None
            claim = None  # in-flight claim on file_hash, released in finally
            try:
                gen, filename, filepath, file_hash = job
                if gen != self._run_gen:
                    # Queued by a previous run whose table was reset: no upload, no events
                    print(f"[Upload] Skipped {filename}: queued by a previous run")
                    continue  # finally still marks the job done

                # Check cache first (waits for an in-flight upload of the same content)
                link = None
                try:
                    if file_hash is None:
                        file_hash = drive_uploader.get_file_hash(filepath)
                    destination = getattr(self, '_batch_drive_folder', '')
                    if isinstance(destination,str) and destination:
                        file_hash = destination + ':' + file_hash
                    link, claim = self._claim_upload(file_hash)
                except Exception as e:
                    print(f"[-] Error checking cache/hash for {filename}: {e}")
                if link:
                    print(f"[Logging] Upload completed: {filename}")
                    self._post(lambda f=filename, l=link, g=gen: self.handle_upload_success(f, l, g))
                    continue  # finally still marks the job done

                # Uploads of this run were cancelled (checked after the claim: a waiter
                # woken by a cancelled upload of the same content must not upload it)
                if gen == self._uploads_cancelled_gen:
                    self._post(lambda f=filename, g=gen: self.handle_upload_cancelled(f, g))
                    continue  # finally still marks the job done

                # Auth circuit breaker: sign-in already failed this run, fail fast
                if self._drive_auth_failed:
                    print(f"[Upload] Skipped {filename}: Google Drive sign-in failed earlier")
                    self._post(lambda f=filename, g=gen: self.handle_upload_failure(
                        f, RuntimeError(DRIVE_AUTH_FAILED_MSG), g))
                    continue  # finally still marks the job done

                # Transition UI state to Uploading...
                self._post(lambda f=filename, g=gen: self.handle_upload_starting(f, g))
                print(f"[Logging] Upload started: {filename}")

                # Lazily build this worker's service from the shared credentials, and
                # rebuild it when they were replaced (Retry discards the old creds)
                creds = self._get_shared_drive_creds()
                if service is None or creds is not service_creds:
                    service = drive_uploader.get_drive_service(creds=creds)
                    service_creds = creds

                destination = getattr(self, '_batch_drive_folder', '')
                options = {'parent_id': destination} if isinstance(destination,str) and destination else {}
                link = drive_uploader._upload_single_file(
                    service, filepath, should_cancel=lambda g=gen: g == self._uploads_cancelled_gen or getattr(self,'_closing',False) is True,
                    **options)
                if not link:
                    raise ValueError("No link returned from Drive upload")
                if file_hash:
                    with self._drive_cache_save_lock:
                        with self._drive_cache_lock:
                            self._drive_cache[file_hash] = link
                            self._uploads_since_save += 1
                        self._save_drive_cache()
                print(f"[Logging] Upload completed: {filename}")
                self._post(lambda f=filename, l=link, g=gen: self.handle_upload_success(f, l, g))
            except drive_uploader.UploadCancelled:
                print(f"[Logging] Upload cancelled: {filename}")
                self._post(lambda f=filename, g=gen: self.handle_upload_cancelled(f, g))
            except Exception as e:
                print(f"[Logging] Upload failed: {filename}")
                print(f"Exception Type: {type(e).__name__}")
                traceback.print_exc()
                if filename is not None:
                    self._post(lambda f=filename, err=e, g=gen: self.handle_upload_failure(f, err, g))
            finally:
                if claim is not None:
                    self._release_upload_claim(file_hash, claim)
                self.upload_queue.task_done()
                if self.upload_queue.unfinished_tasks == 0:
                    self._save_drive_cache(force=True)

    # Upload handlers: main thread only (posted by upload_worker_loop). Events from an
    # earlier run (gen != _run_gen) or for files no longer in the table are ignored.

    def handle_upload_starting(self, filename, gen):
        if gen != self._run_gen:
            return
        row = self.candidates_by_file.get(filename)
        if row is None:
            return
        self.uploading_files.add(filename)
        row["ResumeLink"] = "Uploading"
        self._table_dirty = True
        self._progress_dirty = True

    def handle_upload_success(self, filename, link, gen):
        if gen != self._run_gen:
            return
        row = self.candidates_by_file.get(filename)
        if row is None:
            return
        self.uploading_files.discard(filename)
        self.completed_uploads += 1
        row["ResumeLink"] = "Uploaded"
        row.pop("UploadError", None)
        row["ResumeDriveURL"] = link
        self._table_dirty = True
        self._progress_dirty = True

    def handle_upload_failure(self, filename, error, gen):
        if gen != self._run_gen:
            return
        row = self.candidates_by_file.get(filename)
        if row is None:
            return
        self.uploading_files.discard(filename)
        self.failed_uploads += 1
        row["ResumeLink"] = "Failed"
        row["UploadError"] = str(error)
        self._log_activity(f"Upload failed: {filename} — {error}")
        self._table_dirty = True
        self._progress_dirty = True

    def handle_upload_cancelled(self, filename, gen):
        if gen != self._run_gen:
            return
        row = self.candidates_by_file.get(filename)
        if row is None or row.get("ResumeLink") == "Cancelled":
            return
        self.uploading_files.discard(filename)
        self.cancelled_uploads += 1
        row["ResumeLink"] = "Cancelled"
        self._table_dirty = True
        self._progress_dirty = True

    def extraction_finished(self, processed_count, folder, non_filtered_filenames, start_time, stats_summary=None):
        # Final state wins over any progress/status the pipeline queued this tick
        self._pending_progress = None
        self._pending_status = None
        self._status_dirty = False
        self.set_matching_progress(1.0)
        elapsed = int(time.time() - start_time)
        mins = elapsed // 60
        secs = elapsed % 60
        formatted_time = f"{mins}m {secs}s"
        
        self._processed_count = max(processed_count, len(self.extracted_data))
        self._pipeline_finished = True
        self._processing_issue_count = sum(
            not is_valid_candidate(row, allow_pending=True)
            or "AI unavailable" in str(row.get("Status", ""))
            or str(row.get("Summary", "")).startswith("Evaluation Failed:")
            for row in self.extracted_data)
        self._pipeline_stopped()
            
        # Calculate Phase 1 Candidate Quality Statistics
        total_extracted = 0
        invalid_fields_count = 0
        valid_candidates_temp = []
        
        ai_extraction_count = 0
        phone_inference_count = 0
        location_inference_count = 0
        unresolved_country_count = 0
        
        linkedin_visible_count = 0
        linkedin_pdf_hyperlink_count = 0
        linkedin_docx_hyperlink_count = 0
        linkedin_not_found_count = 0
        
        for cand in self.extracted_data:
            name = cand.get("Name")
            if name in ("Read Error", "Invalid Resume", "API Error"):
                continue
            total_extracted += 1
            if is_valid_candidate(cand, allow_pending=True):
                valid_candidates_temp.append(cand)
            else:
                invalid_fields_count += 1
                
            m = cand.get("CountryResolutionMethod", "Unresolved")
            if m == "AI Extraction":
                ai_extraction_count += 1
            elif m == "Phone Inference":
                phone_inference_count += 1
            elif m == "Location Inference":
                location_inference_count += 1
            elif m in ("Unresolved", "Unresolved Country"):
                unresolved_country_count += 1
                
            src = cand.get("linkedin_source", "Not Found")
            if src == "Visible Text":
                linkedin_visible_count += 1
            elif src == "PDF Hyperlink":
                linkedin_pdf_hyperlink_count += 1
            elif src == "DOCX Hyperlink":
                linkedin_docx_hyperlink_count += 1
            else:
                linkedin_not_found_count += 1

        email_groups = {}
        for cand in valid_candidates_temp:
            email = str(cand.get("Email", "")).lower().strip()
            if email not in email_groups:
                email_groups[email] = []
            email_groups[email].append(cand)
            
        final_valid_candidates = []
        duplicates_removed = 0
        for email, group in email_groups.items():
            if len(group) == 1:
                final_valid_candidates.append(group[0])
            else:
                best_cand = max(group, key=safe_match_percentage)
                final_valid_candidates.append(best_cand)
                duplicates_removed += (len(group) - 1)
                
        final_valid_count = len(final_valid_candidates)
        
        # Print to logs
        print("\n" + "="*50)
        print("CANDIDATE PROCESSING SUMMARY")
        print("="*50)
        print(f"Total Candidates Extracted:                      {total_extracted}")
        print(f"Duplicates Removed:                              {duplicates_removed}")
        print(f"Candidates Removed For Missing Required Fields:  {invalid_fields_count}")
        print(f"Final Valid Candidates:                          {final_valid_count}\n")
        print("Country Resolution Statistics:")
        print(f"AI Extraction:                                   {ai_extraction_count}")
        print(f"Phone Inference:                                 {phone_inference_count}")
        print(f"Location Inference:                              {location_inference_count}")
        print(f"Unresolved:                                      {unresolved_country_count}\n")
        print("LinkedIn URL Statistics:")
        print(f"LinkedIn URLs from visible text:                 {linkedin_visible_count}")
        print(f"LinkedIn URLs from PDF hyperlink annotations:    {linkedin_pdf_hyperlink_count}")
        print(f"LinkedIn URLs from DOCX hyperlinks:              {linkedin_docx_hyperlink_count}")
        print(f"LinkedIn URLs not found:                         {linkedin_not_found_count}")
        print("="*50 + "\n")
        
        file_stats_msg = f"\n\nFile Type Statistics:\n"
        file_stats_msg += f"PDF: {self.pdf_count}\n"
        file_stats_msg += f"DOCX: {self.docx_count}\n"
        file_stats_msg += f"DOC: {self.doc_count}\n"
        file_stats_msg += f"Unsupported: {self.unsupported_count}"
        
        if self.skipped_files:
            file_stats_msg += "\n\n" + format_skipped_files(self.skipped_files)
            
        popup_msg = f"Processing finished in {formatted_time}\n\n"
        popup_msg += f"Total Candidates Extracted: {total_extracted}\n"
        popup_msg += f"Duplicates Removed: {duplicates_removed}\n"
        popup_msg += f"Candidates Removed For Missing Required Fields: {invalid_fields_count}\n"
        popup_msg += f"Final Valid Candidates: {final_valid_count}\n\n"
        popup_msg += f"Country Resolution Statistics:\n"
        popup_msg += f"- AI Extraction: {ai_extraction_count}\n"
        popup_msg += f"- Phone Inference: {phone_inference_count}\n"
        popup_msg += f"- Location Inference: {location_inference_count}\n"
        popup_msg += f"- Unresolved: {unresolved_country_count}\n\n"
        popup_msg += f"LinkedIn URL Statistics:\n"
        popup_msg += f"- LinkedIn URLs from visible text: {linkedin_visible_count}\n"
        popup_msg += f"- LinkedIn URLs from PDF hyperlink annotations: {linkedin_pdf_hyperlink_count}\n"
        popup_msg += f"- LinkedIn URLs from DOCX hyperlinks: {linkedin_docx_hyperlink_count}\n"
        popup_msg += f"- LinkedIn URLs not found: {linkedin_not_found_count}"
        
        if stats_summary:
            popup_msg += f"\n\nData Quality Metrics:\n"
            popup_msg += f"- Retention Rate: {stats_summary['retention_rate']:.1f}%\n"
            popup_msg += f"- Country Resolution Rate: {stats_summary['country_resolution_rate']:.1f}%\n"
            popup_msg += f"- Contact Availability Rate: {stats_summary['contact_availability_rate']:.1f}%"
            
        popup_msg += file_stats_msg

        # Keep the full report available on demand; completion never opens a modal.
        self._processing_report = popup_msg
        self._log_activity(f"Processing finished — {self._processed_count} processed")
        self._refresh_run_state()
        if self._table_dirty:
            self._table_dirty = False
            self.filter_table()
        self._apply_default_sort()

    def export_csv(self):
        # Disabled in Phase 2 improvements
        pass

    def show_export_menu(self):
        self.open_final_export_dialog()

    def open_final_export_dialog(self):
        all_rows = self._eligible_rows()
        filtered_rows = self._eligible_rows(filtered=True)
        if not all_rows:
            messagebox.showwarning('Export CSV','No settled, eligible candidates to export. Review Needs attention or enable Export Missing too in Settings.')
            return
        regular = self._pref('regular_export')
        matched = self._batch_matching_enabled
        dialog = ctk.CTkToplevel(self)
        self._batch_windows.append(dialog)
        generation = self._run_gen
        dialog.title('Export CSV')
        dialog.transient(self)
        dialog.resizable(False, False)
        body = ctk.CTkFrame(dialog,fg_color='transparent')
        body.pack(fill='both',expand=True,padx=24,pady=20)
        body.grid_columnconfigure(1,weight=1)
        ctk.CTkLabel(body,text='Regular export' if regular else 'Export CSV',anchor='w',
            font=ctk.CTkFont(size=18,weight='bold')).grid(row=0,column=0,columnspan=2,sticky='w')
        mode = ('Candidate fields only' + (' · Match % included' if matched else '') if regular else 'Standard 13-column format')
        ctk.CTkLabel(body,text=mode+' · Up to 99 rows per file',text_color=MUTED_TEXT,
            anchor='w').grid(row=1,column=0,columnspan=2,sticky='w',pady=(2,12))
        scope = tk.StringVar(value=f'All eligible ({len(all_rows)})')
        options=[f'All eligible ({len(all_rows)})',f'Filtered ({len(filtered_rows)})']
        ctk.CTkLabel(body,text='Include',anchor='w').grid(row=2,column=0,sticky='w')
        ctk.CTkOptionMenu(body,values=options,variable=scope,height=36,width=280).grid(row=2,column=1,padx=(16,0),sticky='ew')
        missing = sum(bool(attention_reasons(r)) for r in all_rows)
        description = (f'Includes {missing} records needing review. Missing values stay blank.' if self._pref('export_missing')
                       else 'Complete candidates with uploaded resumes only.')
        ctk.CTkLabel(body,text=description,text_color=WARNING_TEXT if self._pref('export_missing') else MUTED_TEXT,
            anchor='w',wraplength=480,justify='left').grid(row=3,column=0,columnspan=2,sticky='w',pady=(8,12))
        ctk.CTkLabel(body,text='Job ID'+(' (optional)' if regular else ''),anchor='w').grid(row=4,column=0,sticky='w')
        entry_job_id=ctk.CTkEntry(body,placeholder_text='e.g. JOB_101',width=280,height=36)
        entry_job_id.grid(row=4,column=1,padx=(16,0),sticky='ew')
        recent = list(reversed(self._review_store.history))[:8]
        if recent:
            pick=tk.StringVar(value='Choose recent Job ID')
            def use_recent(value):
                if value=='Choose recent Job ID': return
                entry_job_id.delete(0,tk.END);entry_job_id.insert(0,value)
            ctk.CTkOptionMenu(body,values=['Choose recent Job ID']+recent,variable=pick,
                command=use_recent,height=36,width=280).grid(row=5,column=1,padx=(16,0),sticky='ew',pady=(8,0))
        error=ctk.CTkLabel(body,text='',text_color=DANGER_TEXT,anchor='w',wraplength=450)
        error.grid(row=6,column=0,columnspan=2,sticky='w')
        more=ctk.CTkFrame(body,fg_color='transparent')
        more.grid_columnconfigure(1,weight=1)
        fields={}
        for i,(key,label,hint) in enumerate([('rate','Expected rate','e.g. $50/hr'),('weeks','Availability (weeks)','e.g. 2'),('notes','Notes','Optional')]):
            ctk.CTkLabel(more,text=label,anchor='w').grid(row=i,column=0,sticky='w',pady=4)
            fields[key]=ctk.CTkEntry(more,placeholder_text=hint,height=36)
            fields[key].grid(row=i,column=1,sticky='ew',padx=(16,0),pady=4)
        ctk.CTkLabel(more,text='Full time',anchor='w').grid(row=3,column=0,sticky='w',pady=4)
        fulltime=ctk.CTkSegmentedButton(more,values=['Yes','No'],height=36)
        fulltime.set('Yes');fulltime.grid(row=3,column=1,sticky='w',padx=(16,0),pady=4)
        def toggle_more():
            if more.winfo_manager():
                more.grid_remove();btn_more.configure(text='More options ▸')
            else:
                more.grid(row=8,column=0,columnspan=2,sticky='ew',pady=8);btn_more.configure(text='Fewer options ▾')
        btn_more=secondary_button(body,text='More options ▸',width=150,command=toggle_more)
        if not regular: btn_more.grid(row=7,column=0,columnspan=2,sticky='w')
        def submit(event=None):
            if generation != self._run_gen:
                dialog.destroy();return
            rows = filtered_rows if scope.get()==options[1] else all_rows
            job = entry_job_id.get().strip()
            if not rows:
                error.configure(text='No eligible candidates in the current filter.');return
            if not regular and not job:
                error.configure(text='Enter a Job ID to export.');entry_job_id.focus_set();return
            self.execute_final_export(dialog,rows,job,fields['rate'].get(),fields['weeks'].get(),
                fields['notes'].get(),fulltime.get(),regular=regular,matched=matched)
        ctk.CTkLabel(body,text='CSV generation does not submit candidates to an ATS.',anchor='w',text_color=MUTED_TEXT).grid(row=9,column=0,columnspan=2,sticky='w',pady=(14,0))
        buttons=ctk.CTkFrame(body,fg_color='transparent')
        buttons.grid(row=10,column=0,columnspan=2,sticky='e',pady=(16,0))
        secondary_button(buttons,text='Cancel',width=120,command=dialog.destroy).pack(side='left',padx=(0,8))
        ctk.CTkButton(buttons,text='Generate CSV',width=150,height=36,command=submit).pack(side='left')
        dialog.bind('<Return>',submit);dialog.bind('<Escape>',lambda e:dialog.destroy())
        dialog.update_idletasks()
        dialog.geometry(f'+{self.winfo_rootx()+max(0,(self.winfo_width()-dialog.winfo_reqwidth())//2)}+{self.winfo_rooty()+60}')
        dialog.grab_set()
        animate_window_open(dialog,self._pref('reduce_motion'))
        dialog.after(100,entry_job_id.focus_set)

    def execute_final_export(self, dialog, valid_candidates, job_id, expected_rate, availability_weeks, notes, is_fulltime,
                             regular=None, matched=None):
        if regular is None: regular=ExtractorApp._pref(self,'regular_export') is True
        if matched is None: matched=getattr(self,'_batch_matching_enabled',False) is True
        job_id=job_id.strip()
        if not regular and not job_id:
            messagebox.showerror('Error','Please enter a Job ID before exporting.',parent=dialog);return
        if job_id and not re.fullmatch(r'[\w .-]+',job_id):
            messagebox.showerror('Job ID','Use letters, numbers, spaces, underscores, dots or dashes in the Job ID.',parent=dialog);return
        review=getattr(self,'_review_store',None)
        hashes=[r.get('FileHash') or r.get('SourceFilename') for r in valid_candidates]
        if isinstance(review,ReviewStore) and ExtractorApp._pref(self,'warn_previous_exports'):
            count=review.previous(job_id,hashes)
            if count and not messagebox.askyesno('Previously generated CSV',
                f'{count} candidates already had a CSV generated for {job_id}. Generate another CSV including them?',parent=dialog): return
        kwargs=dict(defaultextension='.csv',filetypes=[('CSV Files','*.csv')],title='Save candidate CSV',initialfile='candidate_data.csv')
        directory=ExtractorApp._pref(self,'export_directory')
        if ExtractorApp._pref(self,'remember_export_folder') and directory: kwargs['initialdir']=directory
        file_path=filedialog.asksaveasfilename(**kwargs)
        if not file_path: return
        try:
            directory=os.path.dirname(file_path)
            batches=[valid_candidates[i:i+99] for i in range(0,len(valid_candidates),99)]
            if not batches: return
            prefix=job_id or os.path.splitext(os.path.basename(file_path))[0]
            paths=[os.path.join(directory,f'{prefix}_candidate_data_batch_{i}.csv') for i in range(1,len(batches)+1)]
            if any(os.path.exists(path) for path in paths):
                if not messagebox.askyesno('Replace existing CSV',f'{len(paths)} CSV files will be generated in {directory}. Existing files with these names will be replaced. Continue?',parent=dialog): return
            for path,batch in zip(paths,batches):
                temporary=path+'.tmp'
                try:
                    with open(temporary,'w',newline='',encoding='utf-8') as f:
                        writer=csv.writer(f);writer.writerow(csv_fields(regular,matched))
                        for row in batch:
                            writer.writerow(csv_values(row,regular,matched,job_id,expected_rate,availability_weeks,notes,is_fulltime))
                    os.replace(temporary,path)
                finally:
                    if os.path.exists(temporary): os.remove(temporary)
            warnings=[]
            if isinstance(review,ReviewStore):
                try: review.record_export(job_id,hashes,time.strftime('%Y-%m-%d %H:%M'))
                except OSError: warnings.append('CSV history could not be saved.')
            prefs=getattr(self,'preferences',None)
            if isinstance(prefs,dict) and prefs['remember_export_folder']:
                prefs['export_directory']=directory
                try: self._preference_store.save(prefs)
                except OSError: warnings.append('Export folder preference could not be saved.')
            dialog.destroy()
            self._export_succeeded(directory,len(valid_candidates),len(batches))
            if warnings:
                self.lbl_status.configure(text=self.lbl_status.cget('text')+' '+' '.join(warnings))
        except Exception as exc:
            messagebox.showerror('Export failed',f'Could not finish CSV generation:\n{exc}',parent=dialog)

    def _export_succeeded(self, directory, rows, files):
        """Report the export in the status line, with a link to the folder instead of a popup."""
        self._last_export_dir = directory
        self._log_activity(f"Exported {rows} candidate{'s' if rows != 1 else ''} to {directory}")
        self.lbl_status.configure(text=f"CSV generated for {rows} candidate{'s' if rows != 1 else ''} to "
                                       f"{files} CSV file{'s' if files != 1 else ''} in "
                                       f"{os.path.basename(directory) or directory}.")
        self.btn_reveal.grid()

    def _reveal_export(self):
        if self._last_export_dir:
            reveal_in_file_manager(self._last_export_dir)

if __name__ == "__main__":
    setup_logging()
    try:
        app = ExtractorApp()
        app.mainloop()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, f"Runtime Error:\n\n{tb}", "Fatal Error", 0x10)
        except Exception:
            pass
        print(f"Runtime Error:\n{tb}")
        sys.exit(1)
