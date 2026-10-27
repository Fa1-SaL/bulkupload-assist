"""Pure review/export policy. No UI, API calls or source-document writes."""
import json
import os
from pathlib import Path

FIELDS = {'Name':'name', 'Email':'email', 'Phone':'phone', 'Country':'country',
          'YearsOfExperience':'years_of_experience'}
EMPTY = {'', 'not found', 'api error', 'read error', 'filtered', 'none', 'n/a',
         'error', 'not evaluated', 'not uploaded', 'failed', 'cancelled', 'pending', 'uploading'}


def clean(value):
    return '' if value is None or str(value).strip().lower() in EMPTY else value


def parsed(row):
    if 'Parsed' in row:
        return row['Parsed'] is True
    status = str(row.get('Status', '')).lower()
    return not any(x in status for x in ('read error', 'extraction failed', 'reading', 'parsing', 'unreadable')) and bool(clean(row.get('Name')))


def attention_reasons(row):
    reasons = []
    if not parsed(row):
        return [row.get('Status') or 'Could not read or extract resume']
    for key, label in [('Name','name'),('Email','email'),('Phone','phone'),('Country','country'),('YearsOfExperience','experience')]:
        if clean(row.get(key)) == '': reasons.append(f'Missing {label}')
    link = row.get('ResumeLink')
    if link == 'Failed': reasons.append('Upload failed' + (': ' + str(row['UploadError']) if row.get('UploadError') else ''))
    if link == 'Cancelled': reasons.append('Upload cancelled')
    if row.get('MatchPercentage') in ('Not evaluated', 'Error') or str(row.get('Summary','')).startswith('Evaluation Failed'):
        reasons.append('Match not evaluated')
    if row.get('ExtractionError') or 'AI unavailable' in row.get('Status',''):
        reasons.append('AI unavailable')
    return reasons


def exportable(row, include_missing=False):
    if not parsed(row) or row.get('ResumeLink') in ('Pending','Uploading'):
        return False
    if include_missing:
        return any(clean(row.get(k)) != '' for k in FIELDS)
    return all(clean(row.get(k)) != '' for k in FIELDS) and bool(clean(
        row.get('ResumeDriveURL') if row.get('ResumeLink') == 'Uploaded' else row.get('ResumeLink')))


def csv_fields(regular=False, matched=False):
    if regular:
        return ['name','email','phone','country','resume_link','years_of_experience','linkedin_url'] + (['match_percentage'] if matched else [])
    return ['name','email','phone','country','job_id','expected_rate','availability_weeks',
            'resume_link','years_of_experience','linkedin_url','notes','is_fulltime','match_percentage']


def csv_values(row, regular=False, matched=False, job_id='', expected_rate='', availability_weeks='', notes='', is_fulltime='Yes'):
    values = {label: clean(row.get(key)) for key,label in FIELDS.items()}
    values.update(job_id=job_id, expected_rate=expected_rate, availability_weeks=availability_weeks,
                  notes=notes, is_fulltime=is_fulltime, linkedin_url=clean(row.get('linkedin_url')),
                  resume_link=clean(row.get('ResumeDriveURL') if row.get('ResumeLink') == 'Uploaded' else row.get('ResumeLink')),
                  match_percentage=clean(row.get('MatchPercentage')))
    return [values[key] for key in csv_fields(regular, matched)]


def merge_duplicates(rows):
    groups = {}
    for i,row in enumerate(rows):
        email = str(clean(row.get('Email'))).strip().lower()
        groups.setdefault(email or ('file',i), []).append(row)
    result = []
    def score(row):
        try: return float(row.get('MatchPercentage',0))
        except (TypeError,ValueError): return 0
    for group in groups.values():
        best = max(group, key=lambda r:(exportable(r), all(clean(r.get(k)) != '' for k in FIELDS),score(r)))
        merged = dict(best)
        merged['MergedSources'] = list(dict.fromkeys(r.get('SourceFilename','') for r in group if r.get('SourceFilename')))
        result.append(merged)
    return result


class ReviewStore:
    """Local manual corrections and explicit per-job CSV history, keyed by content hash."""
    def __init__(self, path):
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text())
            self.corrections = data.get('corrections', {})
            self.history = data.get('history', {})
            if not isinstance(self.corrections,dict) or not isinstance(self.history,dict): raise ValueError()
            self.corrections={h:{k:v for k,v in fields.items() if k in FIELDS and isinstance(v,(str,int,float))}
                              for h,fields in self.corrections.items() if isinstance(h,str) and isinstance(fields,dict)}
            self.history={job:entries for job,entries in self.history.items() if isinstance(job,str) and isinstance(entries,dict)}
        except (OSError,ValueError,TypeError):
            self.corrections, self.history = {}, {}

    def save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix+'.tmp')
        temp.write_text(json.dumps({'corrections':self.corrections,'history':self.history}, indent=2), encoding='utf-8')
        os.replace(temp,self.path)

    def correct(self, file_hash, values):
        if not file_hash: raise ValueError('A source content hash is required to save corrections.')
        self.corrections[file_hash] = {k: v for k,v in values.items() if k in FIELDS}
        self.save()

    def apply(self, row):
        result = dict(row)
        values = self.corrections.get(row.get('FileHash'))
        if values:
            result.update(values)
            result['EditedFields'] = list(values)
        return result

    def record_export(self, job, hashes, date):
        job = job.strip()
        if job:
            self.history.setdefault(job, {}).update({h:date for h in hashes if h})
            self.save()

    def previous(self, job, hashes):
        previous = self.history.get(job.strip(), {})
        return sum(h in previous for h in set(hashes) if h)

    def clear_history(self):
        self.history = {}
        self.save()
