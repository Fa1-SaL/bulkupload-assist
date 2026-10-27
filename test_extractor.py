import sys
import os
import unittest
import json
import threading
import hashlib
from unittest.mock import MagicMock, patch

# Ensure the main source directory is in the path
sys.path.append(r"c:\Users\faisa_2u122zn\.gemini\antigravity\scratch\turingextractor")

import extractor

_original_thread = threading.Thread

def dummy_md5(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()

# Mock Thread class for synchronous test execution
class SyncThread:
    def __new__(cls, group=None, target=None, name=None, args=(), kwargs=None, *, daemon=None):
        target_name = getattr(target, "__name__", "")
        if "background_upload_thread" in target_name or "extraction_thread" in target_name:
            instance = super().__new__(cls)
            instance.target = target
            instance.args = args
            instance.kwargs = kwargs or {}
            instance.daemon = daemon
            return instance
        else:
            return _original_thread(
                group=group, target=target, name=name,
                args=args, kwargs=kwargs, daemon=daemon
            )

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        if getattr(self, "target", None):
            self.target(*self.args, **self.kwargs)

class TestExtractor(unittest.TestCase):
    def setUp(self):
        import queue
        self.app = MagicMock(spec=extractor.ExtractorApp)
        self.app.lbl_status = MagicMock()
        self.app.progress_bar = MagicMock()
        self.app.btn_extract = MagicMock()
        self.app.btn_export = MagicMock()
        self.app.extracted_data = []
        self.app.upload_queue = queue.Queue()
        self.app.total_uploads = 0
        self.app.completed_uploads = 0
        self.app.failed_uploads = 0
        self.app.cancelled_uploads = 0
        self.app._uploads_cancelled_gen = None
        self.app.uploading_files = set()
        self.app.removal_missing_name = 0
        self.app.removal_missing_contact = 0
        self.app.removal_country_unresolved = 0
        self.app.processing_records = []
        self.app.pdf_count = 0
        self.app.docx_count = 0
        self.app.doc_count = 0
        self.app.unsupported_count = 0
        self.app.skipped_files = []
        self.app.set_matching_progress = MagicMock()
        self.app.update_upload_progress_ui = MagicMock()
        
        extractor.messagebox = MagicMock()
        
        self.app.extraction_thread = extractor.ExtractorApp.extraction_thread.__get__(self.app, extractor.ExtractorApp)
        self.app.async_extraction_pipeline = extractor.ExtractorApp.async_extraction_pipeline.__get__(self.app, extractor.ExtractorApp)
        self.app.process_single_file = extractor.ExtractorApp.process_single_file.__get__(self.app, extractor.ExtractorApp)
        self.app.create_error_row = extractor.ExtractorApp.create_error_row.__get__(self.app, extractor.ExtractorApp)
        self.app.extraction_finished = extractor.ExtractorApp.extraction_finished.__get__(self.app, extractor.ExtractorApp)
        
        # UI queue: background-thread posts run immediately in tests, each followed by
        # a drain tick's coalesced progress/status apply (so tests read applied values)
        self.app._pending_progress = None
        self.app._pending_status = None
        self.app._status_dirty = False
        for name in ("_queue_status_update", "_apply_pending_status"):
            setattr(self.app, name, getattr(extractor.ExtractorApp, name).__get__(self.app, extractor.ExtractorApp))

        def post_and_apply(fn):
            fn()
            self.app._apply_pending_status()
        self.app._post = post_and_apply
        self.app._run_gen = 0
        self.app.candidates_by_file = {}
        self.app._table_dirty = False
        self.app._progress_dirty = False
        self.app._record_upload_counts = extractor.ExtractorApp._record_upload_counts.__get__(self.app, extractor.ExtractorApp)
        self.app.add_or_update_candidate = extractor.ExtractorApp.add_or_update_candidate.__get__(self.app, extractor.ExtractorApp)
        
        def mock_after(delay, func, *args, **kwargs):
            if func:
                func(*args, **kwargs)
        self.app.after = mock_after

        # Isolate cache files in a temp dir so tests never touch real caches
        import tempfile
        import drive_uploader
        self.tmp = tempfile.mkdtemp()
        self._patches = [
            patch.object(extractor, "RESUME_CACHE_FILE", os.path.join(self.tmp, "resume_cache.json")),
            patch.object(extractor, "JD_CACHE_FILE", os.path.join(self.tmp, "jd_cache.json")),
            patch.object(drive_uploader, "DRIVE_CACHE_FILE", os.path.join(self.tmp, "drive_cache.json")),
            patch.object(drive_uploader, "TOKEN_FILE", os.path.join(self.tmp, "token.pickle")),
            patch.object(drive_uploader, "get_drive_service", MagicMock()),
            patch.object(drive_uploader, "get_drive_credentials", MagicMock()),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        import shutil
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch("threading.Thread", SyncThread)
    @patch("extractor.extract_text_from_file")
    @patch("extractor.preprocess_resume_text")
    @patch("extractor.upload_all_resumes")
    @patch("extractor.parse_jd_with_ai")
    @patch("extractor.get_embedding")
    @patch("extractor.get_file_hash")
    @patch("extractor.get_embeddings_batch")
    @patch("extractor.extract_data_with_ai_async")
    @patch("extractor.evaluate_candidate_match_async")
    @patch("extractor.OpenAI")
    @patch("extractor.AsyncOpenAI")
    def test_direct_mode_success(
        self, mock_async_openai, mock_openai, mock_eval_match_async, mock_extract_data_async,
        mock_get_embeddings_batch, mock_get_file_hash, mock_get_embedding, mock_parse_jd,
        mock_upload_resumes, mock_preprocess, mock_extract_file
    ):
        mock_upload_resumes.return_value = {"resume1.pdf": "http://drive/resume1"}
        mock_extract_file.side_effect = lambda path: "text of " + os.path.basename(path) if "corrupt" not in path else None
        mock_preprocess.side_effect = lambda txt: txt
        mock_get_file_hash.side_effect = lambda filepath: dummy_md5(os.path.basename(filepath))
        
        mock_parse_jd.return_value = extractor.ParsedJD(
            required_skills=[], preferred_skills=[], nice_to_have_technologies=[],
            experience_requirements="", minimum_years=0, education_requirements="",
            mandatory_technologies=[], domain_requirements="", seniority_expectations="",
            deal_breakers=[]
        )
        
        mock_get_embedding.return_value = [1.0, 0.0]
        
        async def side_effect_embeddings_batch(texts, client, **kwargs):
            res = []
            for t in texts:
                if "skip" in t:
                    res.append([0.0, 1.0])
                else:
                    res.append([1.0, 0.0])
            return res
        mock_get_embeddings_batch.side_effect = side_effect_embeddings_batch
        
        mock_extract_data_async.return_value = {
            "Name": "Candidate One",
            "Email": "one@example.com",
            "Phone": "123",
            "Country": "US",
            "YearsOfExperience": "5",
            "SourceFilename": "resume1.pdf"
        }
        
        mock_eval_match_async.return_value = {
            "MatchPercentage": 85,
            "TechnicalMatch": 80,
            "ExperienceMatch": 90,
            "DomainMatch": 85,
            "Strengths": ["Code"],
            "Weaknesses": [],
            "MissingRequirements": [],
            "Summary": "Pass",
            "SkillEvidence": []
        }
        
        files = ["resume1.pdf", "skip.pdf", "corrupt.pdf"]
        self.app.extraction_thread("dummy_folder", files, [], "JD", "api-key", "gpt-5")
        
        self.assertEqual(len(self.app.extracted_data), 3)
        
        corrupt_row = next(r for r in self.app.extracted_data if r["SourceFilename"] == "corrupt.pdf")
        self.assertEqual(corrupt_row["Name"], "Read Error")
        self.assertEqual(corrupt_row["ResumeLink"], "Failed")
        
        skip_row = next(r for r in self.app.extracted_data if r["SourceFilename"] == "skip.pdf")
        self.assertEqual(skip_row["Name"], "Filtered")
        self.assertEqual(skip_row["ResumeLink"], "Failed")
        
        success_row = next(r for r in self.app.extracted_data if r["SourceFilename"] == "resume1.pdf")
        self.assertEqual(success_row["Name"], "Candidate One")
        self.assertEqual(success_row["ResumeLink"], "Pending")
        
        # Verify the upload is queued immediately
        self.assertEqual(self.app.upload_queue.qsize(), 1)
        queued_item = self.app.upload_queue.get_nowait()
        self.assertEqual(queued_item, (0, "resume1.pdf", os.path.join("dummy_folder", "resume1.pdf"), dummy_md5("resume1.pdf")))
        
        lbl_calls = self.app.lbl_status.configure.call_args_list
        lbl_texts = [call.kwargs.get("text", "") for call in lbl_calls]
        self.assertTrue(any("Computing similarities..." in t for t in lbl_texts))
        self.assertTrue(any("Extracting candidate data..." in t for t in lbl_texts))

    @patch("extractor.extract_pdf_hyperlinks", return_value=[])
    @patch("extractor.get_file_hash", side_effect=lambda fp: dummy_md5(os.path.basename(fp)))
    @patch("extractor.preprocess_resume_text", side_effect=lambda t: t)
    @patch("extractor.extract_text_from_file")
    @patch("extractor.extract_data_with_ai_async")
    @patch("extractor.OpenAI")
    @patch("extractor.AsyncOpenAI")
    def test_every_parsed_file_is_shown_and_only_valid_uploaded(
        self, _async_openai, _openai, mock_ai, mock_text, _pre, _hash, _links
    ):
        texts = {
            # Name, email and years parse deterministically; the +27 phone does not
            "good.pdf": "Thandeka Ngubeni\nthandeka@x.com | +27 63 541 7584\n2 years of experience",
            "nophone.pdf": "Wiehan Le Roux\nwiehan@x.com\n3 years of experience",
        }
        mock_text.side_effect = lambda path: texts[os.path.basename(path)]
        ai = {
            "good.pdf": {"Name": "Thandeka Ngubeni", "Email": "thandeka@x.com", "Phone": "+27 63 541 7584",
                         "Country": "South Africa", "YearsOfExperience": "2"},
            "nophone.pdf": {"Name": "Wiehan Le Roux", "Email": "wiehan@x.com", "Phone": "Not Found",
                            "Country": "South Africa", "YearsOfExperience": "3"},
        }

        async def fake_ai(text, filename, client, **kw):
            return dict(ai[filename], SourceFilename=filename)
        mock_ai.side_effect = fake_ai

        self.app.extraction_thread("dummy_folder", ["good.pdf", "nophone.pdf"], [], "", "key", "m")

        # A missing phone triggers the AI fallback even when name and years were found
        self.assertEqual(sorted(c.args[1] for c in mock_ai.call_args_list), ["good.pdf", "nophone.pdf"])
        rows = {r["SourceFilename"]: r for r in self.app.extracted_data}
        self.assertEqual(set(rows), {"good.pdf", "nophone.pdf"})
        self.assertEqual(rows["good.pdf"]["Status"], "Parsed")
        self.assertEqual(rows["good.pdf"]["ResumeLink"], "Pending")
        self.assertEqual(rows["nophone.pdf"]["Status"], "Missing Phone")
        self.assertEqual(rows["nophone.pdf"]["Name"], "Wiehan Le Roux")
        self.assertEqual(rows["nophone.pdf"]["ResumeLink"], "Not uploaded")
        queued = [self.app.upload_queue.get_nowait()[1] for _ in range(self.app.upload_queue.qsize())]
        self.assertEqual(queued, ["good.pdf"])
        self.assertEqual([r["filename"] for r in self.app.processing_records], ["good.pdf", "nophone.pdf"])

        # Cached with the current extractor version: reused, no AI call
        mock_ai.reset_mock()
        self.app.extracted_data, self.app.candidates_by_file, self.app.processing_records = [], {}, []
        self.app.extraction_thread("dummy_folder", ["good.pdf", "nophone.pdf"], [], "", "key", "m")
        mock_ai.assert_not_called()

        # Cached by an older extractor: re-extracted
        with open(extractor.RESUME_CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        del cache[dummy_md5("nophone.pdf")]["extract_version"]
        with open(extractor.RESUME_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        self.app.extracted_data, self.app.candidates_by_file, self.app.processing_records = [], {}, []
        self.app.extraction_thread("dummy_folder", ["good.pdf", "nophone.pdf"], [], "", "key", "m")
        self.assertEqual([c.args[1] for c in mock_ai.call_args_list], ["nophone.pdf"])

    @patch("threading.Thread", SyncThread)
    @patch("extractor.extract_text_from_file")
    @patch("extractor.preprocess_resume_text")
    @patch("extractor.upload_all_resumes")
    @patch("extractor.parse_jd_with_ai")
    @patch("extractor.get_embedding")
    @patch("extractor.get_file_hash")
    @patch("extractor.get_embeddings_batch")
    @patch("extractor.extract_data_with_ai_async")
    @patch("extractor.evaluate_candidate_match_async")
    @patch("extractor.OpenAI")
    @patch("extractor.AsyncOpenAI")
    def test_clients_disable_sdk_retries(
        self, mock_async_openai, mock_openai, mock_eval_match_async, mock_extract_data_async,
        mock_get_embeddings_batch, mock_get_file_hash, mock_get_embedding, mock_parse_jd,
        mock_upload_resumes, mock_preprocess, mock_extract_file
    ):
        mock_upload_resumes.return_value = {"resume1.pdf": "http://drive/resume1"}
        mock_extract_file.side_effect = lambda path: "text of " + os.path.basename(path)
        mock_preprocess.side_effect = lambda txt: txt
        mock_get_file_hash.side_effect = lambda filepath: dummy_md5(os.path.basename(filepath))
        mock_parse_jd.return_value = extractor.ParsedJD(
            required_skills=[], preferred_skills=[], nice_to_have_technologies=[],
            experience_requirements="", minimum_years=0, education_requirements="",
            mandatory_technologies=[], domain_requirements="", seniority_expectations="",
            deal_breakers=[]
        )
        mock_get_embedding.return_value = [1.0, 0.0]

        async def side_effect_embeddings_batch(texts, client, **kwargs):
            return [[1.0, 0.0] for _ in texts]
        mock_get_embeddings_batch.side_effect = side_effect_embeddings_batch
        mock_extract_data_async.return_value = {
            "Name": "Candidate One", "Email": "one@example.com", "Phone": "123",
            "Country": "US", "YearsOfExperience": "5", "SourceFilename": "resume1.pdf"
        }
        mock_eval_match_async.return_value = {
            "MatchPercentage": 85, "TechnicalMatch": 80, "ExperienceMatch": 90,
            "DomainMatch": 85, "Strengths": ["Code"], "Weaknesses": [],
            "MissingRequirements": [], "Summary": "Pass", "SkillEvidence": []
        }

        self.app.extraction_thread("dummy_folder", ["resume1.pdf"], [], "JD", "api-key", "gpt-5")

        mock_async_openai.assert_called_with(api_key="api-key", max_retries=0)
        # Sync client keeps SDK default retries (JD parse/embed have no retry of their own)
        mock_openai.assert_called_with(api_key="api-key")

    @patch("threading.Thread", SyncThread)
    @patch("extractor.extract_text_from_file")
    @patch("extractor.preprocess_resume_text")
    @patch("extractor.upload_all_resumes")
    @patch("extractor.parse_jd_with_ai")
    @patch("extractor.get_embedding")
    @patch("extractor.get_file_hash")
    @patch("extractor.get_embeddings_batch")
    @patch("extractor.extract_data_with_ai_async")
    @patch("extractor.evaluate_candidate_match_async")
    @patch("extractor.OpenAI")
    @patch("extractor.AsyncOpenAI")
    def test_empty_jd_embedding_not_cached(
        self, mock_async_openai, mock_openai, mock_eval_match_async, mock_extract_data_async,
        mock_get_embeddings_batch, mock_get_file_hash, mock_get_embedding, mock_parse_jd,
        mock_upload_resumes, mock_preprocess, mock_extract_file
    ):
        mock_upload_resumes.return_value = {"resume1.pdf": "http://drive/resume1"}
        mock_extract_file.side_effect = lambda path: "text of " + os.path.basename(path)
        mock_preprocess.side_effect = lambda txt: txt
        mock_get_file_hash.side_effect = lambda filepath: dummy_md5(os.path.basename(filepath))
        mock_parse_jd.return_value = extractor.ParsedJD(
            required_skills=[], preferred_skills=[], nice_to_have_technologies=[],
            experience_requirements="", minimum_years=0, education_requirements="",
            mandatory_technologies=[], domain_requirements="", seniority_expectations="",
            deal_breakers=[]
        )
        mock_get_embedding.return_value = []

        async def side_effect_embeddings_batch(texts, client, **kwargs):
            return [[1.0, 0.0] for _ in texts]
        mock_get_embeddings_batch.side_effect = side_effect_embeddings_batch
        mock_extract_data_async.return_value = {
            "Name": "Candidate One", "Email": "one@example.com", "Phone": "123",
            "Country": "US", "YearsOfExperience": "5", "SourceFilename": "resume1.pdf"
        }
        mock_eval_match_async.return_value = {
            "MatchPercentage": 85, "TechnicalMatch": 80, "ExperienceMatch": 90,
            "DomainMatch": 85, "Strengths": ["Code"], "Weaknesses": [],
            "MissingRequirements": [], "Summary": "Pass", "SkillEvidence": []
        }

        self.app.extraction_thread("dummy_folder", ["resume1.pdf"], [], "JD", "api-key", "gpt-5")

        self.assertFalse(os.path.exists(extractor.JD_CACHE_FILE))

    @patch("threading.Thread", SyncThread)
    @patch("extractor.extract_text_from_file")
    @patch("extractor.preprocess_resume_text")
    @patch("extractor.upload_all_resumes")
    @patch("extractor.parse_jd_with_ai")
    @patch("extractor.get_embedding")
    @patch("extractor.get_file_hash")
    @patch("extractor.get_embeddings_batch")
    @patch("extractor.extract_data_with_ai_async")
    @patch("extractor.evaluate_candidate_match_async")
    @patch("extractor.OpenAI")
    @patch("extractor.AsyncOpenAI")
    @patch("time.sleep")
    def test_batch_mode_success(
        self, mock_sleep, mock_async_openai, mock_openai, mock_eval_match_async, mock_extract_data_async,
        mock_get_embeddings_batch, mock_get_file_hash, mock_get_embedding, mock_parse_jd,
        mock_upload_resumes, mock_preprocess, mock_extract_file
    ):
        mock_upload_resumes.return_value = {f"resume{i}.pdf": f"http://drive/resume{i}" for i in range(1, 16)}
        mock_extract_file.side_effect = lambda path: "text"
        mock_preprocess.side_effect = lambda txt: txt
        mock_get_file_hash.side_effect = lambda filepath: dummy_md5(os.path.basename(filepath))
        
        mock_parse_jd.return_value = extractor.ParsedJD(
            required_skills=[], preferred_skills=[], nice_to_have_technologies=[],
            experience_requirements="", minimum_years=0, education_requirements="",
            mandatory_technologies=[], domain_requirements="", seniority_expectations="",
            deal_breakers=[]
        )
        mock_get_embedding.return_value = [1.0, 0.0]
        
        async def side_effect_embeddings_batch(texts, client, **kwargs):
            return [[1.0, 0.0] for _ in texts]
        mock_get_embeddings_batch.side_effect = side_effect_embeddings_batch
        
        async def side_effect_extract_data(text, filename, client, **kwargs):
            import re
            num = re.search(r'\d+', filename)
            i = num.group(0) if num else "1"
            return {
                "Name": f"Candidate {i}",
                "Email": f"email{i}@a.com",
                "Phone": "123",
                "Country": "US",
                "YearsOfExperience": "5",
                "SourceFilename": filename
            }
        mock_extract_data_async.side_effect = side_effect_extract_data
        
        mock_eval_match_async.return_value = {
            "MatchPercentage": 85,
            "TechnicalMatch": 80,
            "ExperienceMatch": 90,
            "DomainMatch": 85,
            "Strengths": ["Code"],
            "Weaknesses": [],
            "MissingRequirements": [],
            "Summary": "Pass",
            "SkillEvidence": []
        }
        
        files = [f"resume{i}.pdf" for i in range(1, 16)]
        self.app.extraction_thread("dummy_folder", files, [], "JD", "api-key", "gpt-5")
        
        self.assertEqual(len(self.app.extracted_data), 15)
        row1 = next(r for r in self.app.extracted_data if r["SourceFilename"] == "resume1.pdf")
        self.assertEqual(row1["Name"], "Candidate 1")
        self.assertEqual(row1["ResumeLink"], "Pending")
        
        # Verify all 15 success candidates are queued immediately
        self.assertEqual(self.app.upload_queue.qsize(), 15)
        queued_item = self.app.upload_queue.get_nowait()
        self.assertEqual(queued_item, (0, "resume1.pdf", os.path.join("dummy_folder", "resume1.pdf"), dummy_md5("resume1.pdf")))

    @patch("extractor.OpenAI")
    def test_jd_parse_always_uses_luna(self, mock_openai):
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        for jd in ("short jd", "a" * 2000, "a" * 5000):
            extractor.parse_jd_with_ai(jd, mock_client)
            mock_client.beta.chat.completions.parse.assert_called_with(
                model="gpt-6-luna",
                messages=unittest.mock.ANY,
                response_format=unittest.mock.ANY,
                timeout=60
            )

    def test_luna_is_default_model(self):
        self.assertEqual(extractor.OPENAI_MODELS[0], "gpt-6-luna")
        self.assertEqual(extractor.LUNA_MODEL, "gpt-6-luna")
        self.assertFalse(hasattr(extractor, "DOWNGRADE_MAP"))

    def test_extract_async_defaults_to_luna(self):
        import asyncio
        from unittest.mock import AsyncMock
        parsed = extractor.ResumeData(
            full_name="Jane Doe", email_address="jane@example.com",
            phone_number="5551234", country="USA",
            years_of_experience=7.0, source_filename="f.pdf",
        )
        completion = MagicMock()
        completion.choices = [MagicMock()]
        completion.choices[0].message.parsed = parsed
        client = MagicMock()
        client.beta.chat.completions.parse = AsyncMock(return_value=completion)

        result = asyncio.run(extractor.extract_data_with_ai_async("text", "f.pdf", client))

        self.assertEqual(client.beta.chat.completions.parse.call_args.kwargs["model"], "gpt-6-luna")
        self.assertEqual(result["Name"], "Jane Doe")

class TestDriveUploader(unittest.TestCase):
    def setUp(self):
        import tempfile
        import drive_uploader
        self.tmp = tempfile.mkdtemp()
        self.cache_file = os.path.join(self.tmp, "drive_cache.json")
        self._patches = [
            patch.object(extractor, "RESUME_CACHE_FILE", os.path.join(self.tmp, "resume_cache.json")),
            patch.object(extractor, "JD_CACHE_FILE", os.path.join(self.tmp, "jd_cache.json")),
            patch.object(drive_uploader, "DRIVE_CACHE_FILE", self.cache_file),
            # Never touch the real OAuth token or open a browser flow in tests
            patch.object(drive_uploader, "TOKEN_FILE", os.path.join(self.tmp, "token.pickle")),
            patch.object(drive_uploader, "get_drive_service", MagicMock()),
            patch.object(drive_uploader, "get_drive_credentials", MagicMock()),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        import shutil
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch("drive_uploader.get_file_hash")
    @patch("drive_uploader._upload_single_file")
    @patch("os.listdir")
    @patch("os.path.isfile")
    def test_upload_all_resumes_caching_and_parallel(self, mock_isfile, mock_listdir, mock_upload_resume, mock_get_file_hash):
        import drive_uploader
        mock_listdir.return_value = ["resume1.pdf", "resume2.pdf", "unsupported.xyz"]
        mock_isfile.return_value = True
        
        mock_get_file_hash.side_effect = lambda filepath: dummy_md5(os.path.basename(filepath))
        mock_upload_resume.side_effect = lambda service, path, upload_name=None, should_cancel=None: f"http://drive/{os.path.basename(path)}"
        
        # 1. Cache empty: should upload all supported
        res = drive_uploader.upload_all_resumes("dummy_folder")
        self.assertEqual(res, {
            "resume1.pdf": "http://drive/resume1.pdf",
            "resume2.pdf": "http://drive/resume2.pdf"
        })
        self.assertEqual(mock_upload_resume.call_count, 2)
        # One credential fetch per run; every worker-thread service is built from it
        drive_uploader.get_drive_credentials.assert_called_once_with(force_reauth=False)
        creds = drive_uploader.get_drive_credentials.return_value
        self.assertGreaterEqual(drive_uploader.get_drive_service.call_count, 1)
        for c in drive_uploader.get_drive_service.call_args_list:
            self.assertIs(c.kwargs.get("creds"), creds)
        
        self.assertTrue(os.path.exists(self.cache_file))
        with open(self.cache_file, "r") as f:
            cached_data = json.load(f)
        self.assertEqual(cached_data, {
            dummy_md5("resume1.pdf"): "http://drive/resume1.pdf",
            dummy_md5("resume2.pdf"): "http://drive/resume2.pdf"
        })
        
        # 2. Cache hit: should upload 0 files
        mock_upload_resume.reset_mock()
        res2 = drive_uploader.upload_all_resumes("dummy_folder")
        self.assertEqual(res2, {
            "resume1.pdf": "http://drive/resume1.pdf",
            "resume2.pdf": "http://drive/resume2.pdf"
        })
        self.assertEqual(mock_upload_resume.call_count, 0)
        
        # 3. Cache partially hit: upload only new file
        mock_listdir.return_value = ["resume1.pdf", "resume2.pdf", "resume3.pdf"]
        mock_upload_resume.reset_mock()
        res3 = drive_uploader.upload_all_resumes("dummy_folder")
        self.assertEqual(res3, {
            "resume1.pdf": "http://drive/resume1.pdf",
            "resume2.pdf": "http://drive/resume2.pdf",
            "resume3.pdf": "http://drive/resume3.pdf"
        })
        self.assertEqual(mock_upload_resume.call_count, 1)

class TestNameNormalization(unittest.TestCase):
    def test_normalize_name(self):
        from extractor import normalize_name
        self.assertEqual(normalize_name("ALOK PATHAK"), "Alok Pathak")
        self.assertEqual(normalize_name("DIVYA AGNIHOTRI"), "Divya Agnihotri")
        self.assertEqual(normalize_name("R A V I N D E R   J A S S A L"), "Ravinder Jassal")
        self.assertEqual(normalize_name("McDonald"), "McDonald")
        self.assertEqual(normalize_name("Filtered"), "Filtered")
        self.assertEqual(normalize_name("Read Error"), "Read Error")
        self.assertEqual(normalize_name(""), "")
        self.assertEqual(normalize_name(None), None)

class TestDataQuality(unittest.TestCase):
    def test_standardize_phone_and_infer_country(self):
        from extractor import standardize_phone_and_infer_country
        
        # Test prefixes
        self.assertEqual(standardize_phone_and_infer_country("+1 613-710-0722"), ("6137100722", "Canada"))
        self.assertEqual(standardize_phone_and_infer_country("+1 415-555-0199"), ("4155550199", "United States"))
        self.assertEqual(standardize_phone_and_infer_country("+91 9876543210"), ("9876543210", "India"))
        self.assertEqual(standardize_phone_and_infer_country("+353 892695710"), ("892695710", "Ireland"))
        self.assertEqual(standardize_phone_and_infer_country("+44 7890 123456"), ("7890123456", "United Kingdom"))
        self.assertEqual(standardize_phone_and_infer_country("+971 50 123 4567"), ("501234567", "United Arab Emirates"))
        
        # Test 00 prefix
        self.assertEqual(standardize_phone_and_infer_country("0091 9876543210"), ("9876543210", "India"))
        self.assertEqual(standardize_phone_and_infer_country("001 613-710-0722"), ("6137100722", "Canada"))

        # Test local numbers without prefix (based on length matching country prefix)
        self.assertEqual(standardize_phone_and_infer_country("919876543210"), ("9876543210", "India"))
        self.assertEqual(standardize_phone_and_infer_country("16137100722"), ("6137100722", "Canada"))

    def test_normalize_country_name(self):
        from extractor import normalize_country_name
        self.assertEqual(normalize_country_name("USA"), "United States")
        self.assertEqual(normalize_country_name("US"), "United States")
        self.assertEqual(normalize_country_name("U.S.A."), "United States")
        self.assertEqual(normalize_country_name("United States of America"), "United States")
        self.assertEqual(normalize_country_name("UK"), "United Kingdom")
        self.assertEqual(normalize_country_name("GB"), "United Kingdom")
        self.assertEqual(normalize_country_name("Great Britain"), "United Kingdom")
        self.assertEqual(normalize_country_name("UAE"), "United Arab Emirates")
        self.assertEqual(normalize_country_name("IN"), "India")
        self.assertEqual(normalize_country_name("CA"), "Canada")
        self.assertEqual(normalize_country_name("IE"), "Ireland")
        self.assertEqual(normalize_country_name("AE"), "United Arab Emirates")

    def test_infer_country_from_text_boundaries(self):
        from extractor import infer_country_from_text
        # Avoid matching 'india' in 'indiana'
        self.assertEqual(infer_country_from_text("Based in Indiana, USA"), "United States")
        self.assertEqual(infer_country_from_text("Based in Delhi, India"), "India")

    def test_normalize_and_validate_candidate(self):
        from extractor import normalize_and_validate_candidate
        
        # Valid candidate
        cand = {"Name": "ALOK PATHAK", "Email": "alok@a.com", "Phone": "+91 9876543210", "Country": "IN", "YearsOfExperience": "5"}
        res, status = normalize_and_validate_candidate(cand, "")
        self.assertEqual(status, "Valid")
        self.assertEqual(res["Name"], "Alok Pathak")
        self.assertEqual(res["Phone"], "9876543210")
        self.assertEqual(res["Country"], "India")
        
        # Missing Name
        cand_no_name = {"Name": "Not Found", "Email": "alok@a.com", "Phone": "123", "Country": "IN", "YearsOfExperience": "5"}
        res, status = normalize_and_validate_candidate(cand_no_name, "")
        self.assertIsNotNone(res)
        self.assertEqual(status, "Missing Name")
        
        # Missing Contact Information - Email missing
        cand_no_email = {"Name": "Alok", "Email": "Not Found", "Phone": "123", "Country": "IN", "YearsOfExperience": "5"}
        res, status = normalize_and_validate_candidate(cand_no_email, "")
        self.assertIsNotNone(res)
        self.assertEqual(status, "Missing Email")
        
        # Missing Contact Information - Phone missing
        cand_no_phone = {"Name": "Alok", "Email": "alok@a.com", "Phone": "Not Found", "Country": "IN", "YearsOfExperience": "5"}
        res, status = normalize_and_validate_candidate(cand_no_phone, "")
        self.assertIsNotNone(res)
        self.assertEqual(status, "Missing Phone")
        
        # Unresolved Country
        cand_no_country = {"Name": "Alok", "Email": "alok@a.com", "Phone": "123", "Country": "Not Found", "YearsOfExperience": "5"}
        res, status = normalize_and_validate_candidate(cand_no_country, "")
        self.assertIsNotNone(res)
        self.assertEqual(status, "Country Unresolved")

        # Missing Years of Experience
        cand_no_yoe = {"Name": "Alok", "Email": "alok@a.com", "Phone": "123", "Country": "IN", "YearsOfExperience": "Not Found"}
        res, status = normalize_and_validate_candidate(cand_no_yoe, "")
        self.assertIsNotNone(res)
        self.assertEqual(status, "Missing Years of Experience")

    def test_country_resolution_order(self):
        from extractor import normalize_and_validate_candidate
        
        # 1. AI extracted country exists
        cand = {"Name": "Alok", "Email": "alok@a.com", "Phone": "+91 9876543210", "Country": "Ireland", "YearsOfExperience": "5"}
        res, status = normalize_and_validate_candidate(cand, "Based in USA")
        self.assertEqual(res["Country"], "Ireland")
        
        # 2. Phone number country inference
        cand = {"Name": "Alok", "Email": "alok@a.com", "Phone": "+91 9876543210", "Country": "Not Found", "YearsOfExperience": "5"}
        res, status = normalize_and_validate_candidate(cand, "Based in USA")
        self.assertEqual(res["Country"], "India")
        self.assertEqual(status, "Valid")
        
        # Phone placeholder inferences (user regression examples)
        cand_uk_placeholder = {"Name": "Alok", "Email": "alok@a.com", "Phone": "+44 xxxx", "Country": "Not Found", "YearsOfExperience": "5"}
        res, status = normalize_and_validate_candidate(cand_uk_placeholder, "")
        self.assertEqual(res["Country"], "United Kingdom")
        self.assertEqual(status, "Valid")

        cand_ca_placeholder = {"Name": "Alok", "Email": "alok@a.com", "Phone": "+1 647 xxx xxxx", "Country": "Not Found", "YearsOfExperience": "5"}
        res, status = normalize_and_validate_candidate(cand_ca_placeholder, "")
        self.assertEqual(res["Country"], "Canada")
        self.assertEqual(status, "Valid")
        
        # 3. Resume address/location section (SUMMARY)
        cand = {"Name": "Alok", "Email": "alok@a.com", "Phone": "123", "Country": "Not Found", "YearsOfExperience": "5"}
        resume_text = "### [SUMMARY] ###\nBased in Dublin\n### [EXPERIENCE] ###\nWorked in Delhi, India"
        res, status = normalize_and_validate_candidate(cand, resume_text)
        self.assertEqual(res["Country"], "Ireland")
        
        # 4. Employer location (EXPERIENCE)
        cand = {"Name": "Alok", "Email": "alok@a.com", "Phone": "123", "Country": "Not Found", "YearsOfExperience": "5"}
        resume_text = "### [SUMMARY] ###\nNo address details\n### [EXPERIENCE] ###\nWorked in Delhi, India\n### [EDUCATION] ###\nDegree from Toronto, Canada"
        res, status = normalize_and_validate_candidate(cand, resume_text)
        self.assertEqual(res["Country"], "India")

        # 5. University/education location (EDUCATION)
        cand = {"Name": "Alok", "Email": "alok@a.com", "Phone": "123", "Country": "Not Found", "YearsOfExperience": "5"}
        resume_text = "### [SUMMARY] ###\nNo address details\n### [EXPERIENCE] ###\nWorked in non-descript location\n### [EDUCATION] ###\nDegree from Toronto, Canada"
        res, status = normalize_and_validate_candidate(cand, resume_text)
        self.assertEqual(res["Country"], "Canada")

        # Test CountryResolutionMethod values
        cand_ai = {"Name": "Alok", "Email": "alok@a.com", "Phone": "123", "Country": "IN", "YearsOfExperience": "5"}
        res, _ = normalize_and_validate_candidate(cand_ai, "")
        self.assertEqual(res["CountryResolutionMethod"], "AI Extraction")
        
        cand_phone = {"Name": "Alok", "Email": "alok@a.com", "Phone": "+91 9876543210", "Country": "Not Found", "YearsOfExperience": "5"}
        res, _ = normalize_and_validate_candidate(cand_phone, "")
        self.assertEqual(res["CountryResolutionMethod"], "Phone Inference")
        
        cand_loc = {"Name": "Alok", "Email": "alok@a.com", "Phone": "123", "Country": "Not Found", "YearsOfExperience": "5"}
        res, _ = normalize_and_validate_candidate(cand_loc, "### [SUMMARY] ###\nBased in Dublin")
        self.assertEqual(res["CountryResolutionMethod"], "Location Inference")

class TestAnalyticsAndCSVReport(unittest.TestCase):
    def test_csv_report_generation(self):
        import csv
        import tempfile
        from extractor import ExtractorApp
        
        app = ExtractorApp()
        app.processing_records = [
            {
                "filename": "res1.pdf",
                "status": "valid",
                "reason": "Valid",
                "resolution_method": "AI Extraction",
                "email_found": "Yes",
                "phone_found": "Yes"
            },
            {
                "filename": "res2.pdf",
                "status": "removed",
                "reason": "Country Unresolved",
                "resolution_method": "Unresolved",
                "email_found": "Yes",
                "phone_found": "No"
            }
        ]
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # We mock writing CSV to our temp dir
            report_path = os.path.join(tmpdir, "processing_summary.csv")
            
            with open(report_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Candidate Filename", "Status", "Reason", 
                    "Country Resolution Method", "Email Found", "Phone Found"
                ])
                for rec in app.processing_records:
                    writer.writerow([
                        rec["filename"],
                        rec["status"],
                        rec["reason"],
                        rec["resolution_method"],
                        rec["email_found"],
                        rec["phone_found"]
                    ])
            
            # Verify file exists
            self.assertTrue(os.path.exists(report_path))
            
            # Verify file contents
            with open(report_path, "r", encoding="utf-8") as f:
                reader = list(csv.reader(f))
                self.assertEqual(len(reader), 3) # Header + 2 rows
                self.assertEqual(reader[0], ["Candidate Filename", "Status", "Reason", "Country Resolution Method", "Email Found", "Phone Found"])
                self.assertEqual(reader[1], ["res1.pdf", "valid", "Valid", "AI Extraction", "Yes", "Yes"])
                self.assertEqual(reader[2], ["res2.pdf", "removed", "Country Unresolved", "Unresolved", "Yes", "No"])

class TestFinalCSVExport(unittest.TestCase):
    def test_is_valid_candidate(self):
        # 1. Valid candidate
        cand = {
            "Name": "Alok Pathak",
            "Email": "alok@example.com",
            "Phone": "9876543210",
            "Country": "India",
            "YearsOfExperience": "5",
            "ResumeLink": "Uploaded"
        }
        self.assertTrue(extractor.is_valid_candidate(cand))

        # 2. Invalid candidate - Filtered name
        cand_filtered = cand.copy()
        cand_filtered["Name"] = "Filtered"
        self.assertFalse(extractor.is_valid_candidate(cand_filtered))

        # 3. Invalid candidate - Missing Name
        cand_no_name = cand.copy()
        cand_no_name["Name"] = ""
        self.assertFalse(extractor.is_valid_candidate(cand_no_name))

        # 4. Invalid candidate - Missing Contact (Email missing)
        cand_no_email = cand.copy()
        cand_no_email["Email"] = "Not Found"
        self.assertFalse(extractor.is_valid_candidate(cand_no_email))

        # 5. Invalid candidate - Phone missing
        cand_no_phone = cand.copy()
        cand_no_phone["Phone"] = "Not Found"
        self.assertFalse(extractor.is_valid_candidate(cand_no_phone))

        # 6. Invalid candidate - Missing Country
        cand_no_country = cand.copy()
        cand_no_country["Country"] = "API Error"
        self.assertFalse(extractor.is_valid_candidate(cand_no_country))

        # 7. Invalid candidate - Missing YearsOfExperience
        cand_no_yoe = cand.copy()
        cand_no_yoe["YearsOfExperience"] = ""
        self.assertFalse(extractor.is_valid_candidate(cand_no_yoe))

        # 8. Invalid candidate - Missing ResumeLink
        cand_no_link = cand.copy()
        cand_no_link["ResumeLink"] = "Failed"
        self.assertFalse(extractor.is_valid_candidate(cand_no_link))

        # 9. Intermediate state validation
        cand_pending = cand.copy()
        cand_pending["ResumeLink"] = "Pending"
        self.assertFalse(extractor.is_valid_candidate(cand_pending, allow_pending=False))
        self.assertTrue(extractor.is_valid_candidate(cand_pending, allow_pending=True))

    @patch("extractor.filedialog.asksaveasfilename")
    @patch("extractor.messagebox.showinfo")
    @patch("extractor.messagebox.showerror")
    def test_execute_final_export(self, mock_error, mock_info, mock_save):
        import tempfile
        import csv
        
        # Setup mock app
        app = MagicMock(spec=extractor.ExtractorApp)
        app.btn_export = MagicMock()
        
        # Setup mock dialog
        dialog = MagicMock()
        
        # Create some valid candidates
        valid_candidates = [
            {
                "Name": "Alok Pathak",
                "Email": "alok@example.com",
                "Phone": "9876543210",
                "Country": "India",
                "YearsOfExperience": "5",
                "linkedin_url": "https://www.linkedin.com/in/alokpathak",
                "MatchPercentage": "85",
                "ResumeLink": "Uploaded",
                "ResumeDriveURL": "https://drive.google.com/resume1"
            },
            {
                "Name": "Divya Agnihotri",
                "Email": "divya@example.com",
                "Phone": "9876543211",
                "Country": "United States",
                "YearsOfExperience": "2",
                "linkedin_url": "",
                "MatchPercentage": "70",
                "ResumeLink": "Pending",
                "ResumeDriveURL": ""
            }
        ]
        
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test_final.csv")
            mock_save.return_value = file_path
            
            # Execute final export via unbound method
            extractor.ExtractorApp.execute_final_export(
                app, dialog, valid_candidates,
                job_id="JOB_123", expected_rate="50", availability_weeks="4",
                notes="Awesome candidate", is_fulltime="Yes"
            )
            
            # Verify batch file exists
            batch_path = os.path.join(tmpdir, "JOB_123_candidate_data_batch_1.csv")
            self.assertTrue(os.path.exists(batch_path))
            
            # Verify CSV contents
            with open(batch_path, "r", encoding="utf-8") as f:
                reader = list(csv.reader(f))
                self.assertEqual(len(reader), 3) # Header + 2 rows
                
                # Check header exactly as required
                expected_headers = [
                    "name", "email", "phone", "country", "job_id", "expected_rate",
                    "availability_weeks", "resume_link", "years_of_experience",
                    "linkedin_url", "notes", "is_fulltime", "match_percentage"
                ]
                self.assertEqual(reader[0], expected_headers)
                
                # Check first row
                self.assertEqual(reader[1], [
                    "Alok Pathak", "alok@example.com", "9876543210", "India",
                    "JOB_123", "50", "4", "https://drive.google.com/resume1",
                    "5", "https://www.linkedin.com/in/alokpathak", "Awesome candidate", "Yes", "85"
                ])
                
                # Check second row (resume_link should be status if not uploaded)
                self.assertEqual(reader[2], [
                    "Divya Agnihotri", "divya@example.com", "9876543211", "United States",
                    "JOB_123", "50", "4", "Pending",
                    "2", "", "Awesome candidate", "Yes", "70"
                ])
                
            # Verify dialog was destroyed and success is reported in the status line
            app._export_succeeded.assert_called_once_with(tmpdir, 2, 1)
            mock_info.assert_not_called()
            dialog.destroy.assert_called_once()

    @patch("extractor.filedialog.asksaveasfilename")
    @patch("extractor.messagebox.showinfo")
    @patch("extractor.messagebox.showerror")
    def test_final_export_batching_splitting(self, mock_error, mock_info, mock_save):
        import tempfile
        import csv
        
        # Setup mock app
        app = MagicMock(spec=extractor.ExtractorApp)
        app.btn_export = MagicMock()
        
        # Setup mock dialog
        dialog = MagicMock()
        
        def create_candidates(n):
            return [
                {
                    "Name": f"Candidate {i}",
                    "Email": f"cand{i}@example.com",
                    "Phone": f"Phone {i}",
                    "Country": "United States",
                    "YearsOfExperience": "5",
                    "linkedin_url": "",
                    "MatchPercentage": "80",
                    "ResumeLink": "Uploaded",
                    "ResumeDriveURL": "https://drive.google.com/resume"
                }
                for i in range(1, n + 1)
            ]
            
        test_cases = [
            (98, 1, [98]),
            (99, 1, [99]),
            (100, 2, [99, 1]),
            (198, 2, [99, 99]),
            (247, 3, [99, 99, 49])
        ]
        
        for num_candidates, expected_files, expected_batch_sizes in test_cases:
            candidates = create_candidates(num_candidates)
            with tempfile.TemporaryDirectory() as tmpdir:
                file_path = os.path.join(tmpdir, "test_final.csv")
                mock_save.return_value = file_path
                
                extractor.ExtractorApp.execute_final_export(
                    app, dialog, candidates,
                    job_id="77874", expected_rate="50", availability_weeks="4",
                    notes="Test", is_fulltime="Yes"
                )
                
                # Check file existence and batch sizes
                for idx, expected_size in enumerate(expected_batch_sizes, 1):
                    batch_path = os.path.join(tmpdir, f"77874_candidate_data_batch_{idx}.csv")
                    self.assertTrue(os.path.exists(batch_path), f"File {batch_path} not found for case {num_candidates}")
                    
                    with open(batch_path, "r", encoding="utf-8") as f:
                        reader = list(csv.reader(f))
                        self.assertEqual(len(reader), expected_size + 1, f"Unexpected rows in batch {idx} for case {num_candidates}")
                        
                        # Verify filename is correct and not without job_id
                        self.assertIn("77874_candidate_data_batch_", os.path.basename(batch_path))

# Mock COM modules if not present to allow tests to run on any environment
import sys
from unittest.mock import MagicMock
if "pythoncom" not in sys.modules:
    sys.modules["pythoncom"] = MagicMock()
if "comtypes" not in sys.modules:
    sys.modules["comtypes"] = MagicMock()
if "comtypes.client" not in sys.modules:
    sys.modules["comtypes.client"] = MagicMock()

class TestLegacyDocSupport(unittest.TestCase):
    def assert_documents_open(self, mock_word, path, count=1):
        """Word's Application has no Open(): documents open via word.Documents.Open."""
        mock_word.Open.assert_not_called()
        self.assertEqual(mock_word.Documents.Open.call_count, count)
        call = mock_word.Documents.Open.call_args
        self.assertEqual(call.args, (os.path.abspath(path),))
        self.assertEqual(call.kwargs, {"ConfirmConversions": False, "ReadOnly": True,
                                       "AddToRecentFiles": False, "PasswordDocument": "x"})

    @patch("pythoncom.CoInitialize")
    @patch("pythoncom.CoUninitialize")
    def test_extract_text_from_doc_com_success(self, mock_couninit, mock_coinit):
        # COM success
        import comtypes.client
        mock_word = MagicMock()
        mock_doc = MagicMock()
        mock_word.Documents.Open.return_value = mock_doc
        mock_doc.Content.Text = "Text content from legacy Word file."
        comtypes.client.CreateObject = MagicMock(return_value=mock_word)

        text = extractor.extract_text_from_doc("dummy.doc")
        self.assertEqual(text, "Text content from legacy Word file.")
        self.assert_documents_open(mock_word, "dummy.doc")
        mock_doc.Close.assert_called_once_with(SaveChanges=0)
        mock_word.Quit.assert_called_once()
        mock_coinit.assert_called_once()
        mock_couninit.assert_called_once()

    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=unittest.mock.mock_open, read_data="Text from LibreOffice fallback.")
    @patch("os.remove")
    def test_extract_text_from_doc_libreoffice_success(self, mock_remove, mock_file_open, mock_exists, mock_run):
        # COM fails, LibreOffice succeeds
        import comtypes.client
        comtypes.client.CreateObject = MagicMock(side_effect=Exception("COM failure"))
        mock_exists.return_value = True
        mock_run.return_value.returncode = 0

        text = extractor.extract_text_from_doc("dummy.doc")
        self.assertEqual(text, "Text from LibreOffice fallback.")
        mock_run.assert_called_once()
        mock_remove.assert_called_once()

    @patch("subprocess.run", side_effect=Exception("LibreOffice failure"))
    def test_extract_text_from_doc_all_fail(self, mock_run):
        # Both fail, returns None
        import comtypes.client
        comtypes.client.CreateObject = MagicMock(side_effect=Exception("COM failure"))
        
        text = extractor.extract_text_from_doc("dummy.doc")
        self.assertIsNone(text)

    def _fake_com_modules(self, create_object):
        pythoncom = MagicMock()
        client = MagicMock()
        client.CreateObject = create_object
        comtypes = MagicMock()
        comtypes.client = client
        mods = {"pythoncom": pythoncom, "comtypes": comtypes, "comtypes.client": client}
        return pythoncom, mods

    def test_doc_batch_creates_word_once(self):
        mock_word = MagicMock()
        mock_word.Documents.Open.return_value.Content.Text = "doc text"
        create = MagicMock(return_value=mock_word)
        pythoncom, mods = self._fake_com_modules(create)
        paths = [os.path.join("folder", "r%d.doc" % i) for i in range(5)]
        with patch.dict(sys.modules, mods):
            result = extractor.extract_doc_batch(paths)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(mock_word.Quit.call_count, 1)
        self.assertEqual(pythoncom.CoInitialize.call_count, 1)
        self.assertEqual(pythoncom.CoUninitialize.call_count, 1)
        self.assert_documents_open(mock_word, paths[-1], count=5)
        self.assertEqual(mock_word.Documents.Open.return_value.Close.call_count, 5)
        for c in mock_word.Documents.Open.return_value.Close.call_args_list:
            self.assertEqual(c.kwargs, {"SaveChanges": 0})
        self.assertEqual(mock_word.DisplayAlerts, 0)
        self.assertEqual(set(result.keys()), set(paths))
        self.assertTrue(all(v == "doc text" for v in result.values()))

    def test_doc_batch_falls_back_without_com(self):
        create = MagicMock(side_effect=Exception("COM failure"))
        pythoncom, mods = self._fake_com_modules(create)
        paths = ["a.doc", "b.doc", "c.doc"]
        with patch.dict(sys.modules, mods), \
                patch("extractor.extract_text_from_doc", side_effect=lambda p, word=None: "T:" + p) as mock_single:
            result = extractor.extract_doc_batch(paths)
        self.assertEqual(mock_single.call_count, 3)
        for call in mock_single.call_args_list:
            self.assertEqual(len(call.args), 1)
            self.assertNotIn("word", call.kwargs)
        self.assertEqual(result, {p: "T:" + p for p in paths})
        pythoncom.CoUninitialize.assert_called_once()

    def test_doc_with_shared_word_does_not_quit_or_coinit(self):
        mock_word = MagicMock()
        mock_word.Documents.Open.return_value.Content.Text = "shared text"
        pythoncom, mods = self._fake_com_modules(MagicMock())
        with patch.dict(sys.modules, mods):
            text = extractor.extract_text_from_doc("x.doc", word=mock_word)
        self.assertEqual(text, "shared text")
        self.assert_documents_open(mock_word, "x.doc")
        mock_word.Documents.Open.return_value.Close.assert_called_once_with(SaveChanges=0)
        mock_word.Quit.assert_not_called()
        pythoncom.CoInitialize.assert_not_called()

    @patch("subprocess.run")
    @patch("os.path.exists", return_value=True)
    @patch("builtins.open", new_callable=unittest.mock.mock_open, read_data="LO text")
    @patch("os.remove")
    def test_doc_shared_word_error_falls_back_to_libreoffice(self, mock_remove, mock_open_, mock_exists, mock_run):
        mock_word = MagicMock()
        mock_word.Documents.Open.side_effect = Exception("corrupt doc")
        mock_run.return_value.returncode = 0
        text = extractor.extract_text_from_doc("bad.doc", word=mock_word)
        self.assertEqual(text, "LO text")
        mock_run.assert_called_once()
        mock_word.Quit.assert_not_called()

    def test_convert_docx_to_pdf_uses_documents_open(self):
        mock_word = MagicMock()
        doc = mock_word.Documents.Open.return_value
        pythoncom, mods = self._fake_com_modules(MagicMock(return_value=mock_word))
        with patch.dict(sys.modules, mods):
            out = extractor.convert_docx_to_pdf("cv.docx")
        self.assertEqual(out, "cv_converted.pdf")
        self.assert_documents_open(mock_word, "cv.docx")
        doc.SaveAs.assert_called_once_with(os.path.abspath("cv_converted.pdf"), FileFormat=17)
        doc.Close.assert_called_once_with(SaveChanges=0)
        mock_word.Quit.assert_called_once()

    @patch("os.path.exists")
    @patch("os.listdir")
    @patch("os.path.isfile")
    @patch("extractor.messagebox.showinfo")
    def test_file_discovery_with_doc_and_skipped(self, mock_showinfo, mock_isfile, mock_listdir, mock_exists):
        mock_exists.return_value = True
        app = MagicMock()
        app.sv_dir = MagicMock()
        app.sv_dir.get.return_value = "dummy_folder"
        app.sv_model = MagicMock()
        app.sv_model.get.return_value = "gpt-5"
        app.txt_jd = MagicMock()
        app.txt_jd.get.return_value = "JD text"
        app.tree = MagicMock()
        app.tree.get_children.return_value = []
        
        mock_listdir.return_value = [
            "resume1.pdf", "resume2.docx", "resume3.doc", "resume4.txt",
            "image.png", "data.csv"
        ]
        mock_isfile.return_value = True

        # Discovery runs in a worker; apply its posted result synchronously here.
        app._closing = False
        app._post = lambda fn: fn()
        app._begin_extraction = extractor.ExtractorApp._begin_extraction.__get__(app)
        extractor.ExtractorApp._discover_resume_folder(app, "dummy_folder", "JD text", "key", "gpt-5", False)

        # Verify partitioning
        self.assertEqual(app.pdf_count, 1)
        self.assertEqual(app.docx_count, 1)
        self.assertEqual(app.doc_count, 1)
        self.assertEqual(app.unsupported_count, 2)
        
        # Verify skipped files reason
        self.assertEqual(len(app.skipped_files), 2)
        self.assertEqual(set(app.skipped_files), {("image.png", "Unsupported file format"),
                                                  ("data.csv", "Unsupported file format")})

class TestPhase1Improvements(unittest.TestCase):
    def test_linkedin_url_extraction(self):
        from extractor import normalize_and_validate_candidate
        
        # Candidate with valid linkedin URL (https://www.linkedin.com/in/alokpathak)
        cand1 = {"Name": "Alok Pathak", "Email": "alok@a.com", "Phone": "12345", "Country": "India", "YearsOfExperience": "5"}
        res1, _ = normalize_and_validate_candidate(cand1, "Some resume text https://www.linkedin.com/in/alokpathak more text")
        self.assertEqual(res1.get("linkedin_url"), "https://www.linkedin.com/in/alokpathak")
        
        # Candidate with linkedin.com/pub/... format
        cand2 = cand1.copy()
        res2, _ = normalize_and_validate_candidate(cand2, "Profile: linkedin.com/pub/some-user/1/2/3")
        self.assertEqual(res2.get("linkedin_url"), "https://www.linkedin.com/pub/some-user/1/2/3")
        
        # Candidate with no linkedin URL
        cand3 = cand1.copy()
        res3, _ = normalize_and_validate_candidate(cand3, "Resume text without linkedin profile.")
        self.assertEqual(res3.get("linkedin_url"), "")

    def test_email_deduplication(self):
        from extractor import get_deduplicated_candidates
        
        candidates = [
            # Duplicates with different match percentages
            {"Name": "Alok A", "Email": "ALOK@gmail.com ", "Phone": "123", "Country": "India", "YearsOfExperience": "5", "MatchPercentage": 45},
            {"Name": "Alok B", "Email": " alok@gmail.com", "Phone": "123", "Country": "India", "YearsOfExperience": "5", "MatchPercentage": 78},
            # Single candidate
            {"Name": "Divya", "Email": "divya@gmail.com", "Phone": "456", "Country": "India", "YearsOfExperience": "3", "MatchPercentage": 60}
        ]
        
        deduped = get_deduplicated_candidates(candidates)
        self.assertEqual(len(deduped), 2)
        
        # Verify it kept the one with the highest MatchPercentage (Alok B with 78)
        alok_retained = next(c for c in deduped if c["Email"].strip().lower() == "alok@gmail.com")
        self.assertEqual(alok_retained["Name"], "Alok B")
        self.assertEqual(alok_retained["MatchPercentage"], 78)

    def test_statistics_reporting(self):
        from extractor import ExtractorApp
        
        # Setup mock app and mock messagebox
        app = MagicMock(spec=ExtractorApp)
        app.extracted_data = [
            # Valid, unique candidate
            {"Name": "Alok", "Email": "alok@gmail.com", "Phone": "123", "Country": "India", "YearsOfExperience": "5", "ResumeLink": "Uploaded", "MatchPercentage": 80},
            # Duplicate (lower score)
            {"Name": "Alok Duplicate", "Email": "alok@gmail.com", "Phone": "123", "Country": "India", "YearsOfExperience": "5", "ResumeLink": "Uploaded", "MatchPercentage": 40},
            # Missing required field (Phone is invalid/Not Found)
            {"Name": "Divya", "Email": "divya@gmail.com", "Phone": "Not Found", "Country": "India", "YearsOfExperience": "3", "ResumeLink": "Uploaded", "MatchPercentage": 60},
            # Corrupt file (Read Error - should not count as extracted candidate)
            {"Name": "Read Error", "Email": "Read Error", "Phone": "Read Error", "Country": "Read Error", "ResumeLink": "Failed"}
        ]
        app.pdf_count = 0
        app.docx_count = 0
        app.doc_count = 0
        app.unsupported_count = 0
        app.skipped_files = []
        app.set_matching_progress = MagicMock()
        app.lbl_status = MagicMock()
        app.btn_export = MagicMock()
        app.btn_extract = MagicMock()
        
        # Save print output to check console logs
        import io
        import sys
        captured_output = io.StringIO()
        sys.stdout = captured_output
        
        try:
            app._table_dirty = False
            app.after = lambda delay, fn, *a: fn(*a)
            with patch("extractor.messagebox.showinfo") as mock_showinfo:
                ExtractorApp.extraction_finished(app, processed_count=4, folder="dummy", non_filtered_filenames=[], start_time=0)
                
                # Check popup contents
                mock_showinfo.assert_not_called()
                popup_text = app._processing_report
                
                # Assert counts in popup text
                self.assertIn("Total Candidates Extracted: 3", popup_text)
                self.assertIn("Duplicates Removed: 1", popup_text)
                self.assertIn("Candidates Removed For Missing Required Fields: 1", popup_text)
                self.assertIn("Final Valid Candidates: 1", popup_text)
        finally:
            sys.stdout = sys.__stdout__
            
        log_text = captured_output.getvalue()
        self.assertIn("Total Candidates Extracted:                      3", log_text)
        self.assertIn("Duplicates Removed:                              1", log_text)
        self.assertIn("Candidates Removed For Missing Required Fields:  1", log_text)
        self.assertIn("Final Valid Candidates:                          1", log_text)

    @patch("extractor.filedialog.asksaveasfilename")
    @patch("extractor.messagebox.showinfo")
    @patch("extractor.messagebox.showerror")
    def test_linkedin_url_export_and_deduplication(self, mock_error, mock_info, mock_save):
        from extractor import ExtractorApp, get_deduplicated_candidates
        import csv
        import tempfile
        
        # 1. Verify deduplication retains linkedin_url from candidate with highest MatchPercentage
        candidates = [
            {"Name": "Alok A", "Email": "alok@example.com", "Phone": "12345", "Country": "India", "YearsOfExperience": "5", "MatchPercentage": 45, "linkedin_url": "https://linkedin.com/in/alok-a", "ResumeLink": "Uploaded"},
            {"Name": "Alok B", "Email": "alok@example.com", "Phone": "12345", "Country": "India", "YearsOfExperience": "5", "MatchPercentage": 85, "linkedin_url": "https://linkedin.com/in/alok-b", "ResumeLink": "Uploaded"}
        ]
        deduped = get_deduplicated_candidates(candidates)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["linkedin_url"], "https://linkedin.com/in/alok-b")
        
        # 2. Verify raw CSV export includes linkedin_url (with and without LinkedIn URL)
        app = MagicMock(spec=ExtractorApp)
        app.extracted_data = [
            {"Name": "Alok Pathak", "Email": "alok@example.com", "Phone": "9876543210", "Country": "India", "YearsOfExperience": "5", "linkedin_url": "https://www.linkedin.com/in/alokpathak", "MatchPercentage": "85", "ResumeLink": "Uploaded", "ResumeDriveURL": "https://drive.google.com/resume1"},
            {"Name": "Divya Agnihotri", "Email": "divya@example.com", "Phone": "9876543211", "Country": "United States", "YearsOfExperience": "2", "linkedin_url": "", "MatchPercentage": "70", "ResumeLink": "Uploaded", "ResumeDriveURL": "https://drive.google.com/resume2"}
        ]
        
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test_raw_export.csv")
            mock_save.return_value = file_path
            
            # Run execute_final_export via unbound method
            dialog = MagicMock()
            ExtractorApp.execute_final_export(
                app, dialog, app.extracted_data,
                job_id="JOB_123", expected_rate="50", availability_weeks="4",
                notes="Awesome candidate", is_fulltime="Yes"
            )
            
            # Verify batch file exists
            batch_path = os.path.join(tmpdir, "JOB_123_candidate_data_batch_1.csv")
            self.assertTrue(os.path.exists(batch_path))
            
            # Verify CSV contents
            with open(batch_path, "r", encoding="utf-8") as f:
                reader = list(csv.reader(f))
                self.assertEqual(len(reader), 3) # Header + 2 rows
                
                # Check header contains "linkedin_url"
                self.assertIn("linkedin_url", reader[0])
                linkedin_idx = reader[0].index("linkedin_url")
                
                # Verify values match candidate dictionary values
                self.assertEqual(reader[1][linkedin_idx], "https://www.linkedin.com/in/alokpathak")
                self.assertEqual(reader[2][linkedin_idx], "")

    def test_docx_hyperlink_extraction_and_normalization(self):
        from extractor import normalize_linkedin_url, extract_docx_hyperlinks, normalize_and_validate_candidate, ExtractorApp
        import tempfile
        import zipfile
        import os
        
        # 1. Test normalize_linkedin_url
        self.assertEqual(normalize_linkedin_url("linkedin.com/in/johndoe"), "https://www.linkedin.com/in/johndoe")
        self.assertEqual(normalize_linkedin_url("[www.linkedin.com/in/johndoe/](http://www.linkedin.com/in/johndoe/)"), "https://www.linkedin.com/in/johndoe")
        self.assertEqual(normalize_linkedin_url("https://linkedin.com/in/johndoe?trk=abc"), "https://www.linkedin.com/in/johndoe")
        self.assertEqual(normalize_linkedin_url("linkedin.com/pub/some-user/1/2/3"), "https://www.linkedin.com/pub/some-user/1/2/3")
        self.assertEqual(normalize_linkedin_url("linkedin.com/company/google"), "https://www.linkedin.com/company/google")
        
        # 2. Test extract_docx_hyperlinks with a mock zip file
        rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://www.linkedin.com/in/john-docx-hyper" TargetMode="External"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
</Relationships>"""
        
        with tempfile.TemporaryDirectory() as tmpdir:
            docx_path = os.path.join(tmpdir, "test_resume.docx")
            with zipfile.ZipFile(docx_path, 'w') as z:
                z.writestr("word/_rels/document.xml.rels", rels_xml)
                z.writestr("word/document.xml", "<document></document>")
                
            links = extract_docx_hyperlinks(docx_path)
            self.assertIn("https://www.linkedin.com/in/john-docx-hyper", links)
            
            # 3. Test normalize_and_validate_candidate merging and priority logic
            cand = {"Name": "Alok", "Email": "alok@a.com", "Phone": "123", "Country": "IN", "YearsOfExperience": "5"}
            
            # Case A: Hyperlink has /in/ and Visible text has /pub/ -> Prefer /in/ (from hyperlink)
            res, _ = normalize_and_validate_candidate(cand, "Visible text has linkedin.com/pub/visible-user", docx_path)
            self.assertEqual(res.get("linkedin_url"), "https://www.linkedin.com/in/john-docx-hyper")
            self.assertEqual(res.get("linkedin_source"), "DOCX Hyperlink")
            
            # Case B: Visible text has /in/ and Hyperlink has /pub/ -> Prefer /in/ (from visible text)
            rels_pub_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://www.linkedin.com/pub/john-docx-pub" TargetMode="External"/>
</Relationships>"""
            docx_pub_path = os.path.join(tmpdir, "test_pub.docx")
            with zipfile.ZipFile(docx_pub_path, 'w') as z:
                z.writestr("word/_rels/document.xml.rels", rels_pub_xml)
                z.writestr("word/document.xml", "<document></document>")
                
            res, _ = normalize_and_validate_candidate(cand, "Visible has linkedin.com/in/visible-in-user", docx_pub_path)
            self.assertEqual(res.get("linkedin_url"), "https://www.linkedin.com/in/visible-in-user")
            self.assertEqual(res.get("linkedin_source"), "Visible Text")
            
        # 4. Test extraction_finished popup stats for LinkedIn sources
        app = MagicMock(spec=ExtractorApp)
        app.extracted_data = [
            {"Name": "A", "Email": "a@a.com", "Phone": "123", "Country": "IN", "YearsOfExperience": "5", "linkedin_source": "Visible Text"},
            {"Name": "B", "Email": "b@b.com", "Phone": "123", "Country": "IN", "YearsOfExperience": "5", "linkedin_source": "DOCX Hyperlink"},
            {"Name": "C", "Email": "c@c.com", "Phone": "123", "Country": "IN", "YearsOfExperience": "5", "linkedin_source": "Not Found"}
        ]
        app.pdf_count = 0
        app.docx_count = 0
        app.doc_count = 0
        app.unsupported_count = 0
        app.skipped_files = []
        app.set_matching_progress = MagicMock()
        app.lbl_status = MagicMock()
        app.btn_export = MagicMock()
        app.btn_extract = MagicMock()
        
        import io
        import sys
        captured_output = io.StringIO()
        sys.stdout = captured_output
        try:
            app._table_dirty = False
            app.after = lambda delay, fn, *a: fn(*a)
            with patch("extractor.messagebox.showinfo") as mock_showinfo:
                ExtractorApp.extraction_finished(app, processed_count=3, folder="dummy", non_filtered_filenames=[], start_time=0)
                mock_showinfo.assert_not_called()
                popup_text = app._processing_report
                
                # Check that LinkedIn source counts are in popup text
                self.assertIn("LinkedIn URLs from visible text: 1", popup_text)
                self.assertIn("LinkedIn URLs from PDF hyperlink annotations: 0", popup_text)
                self.assertIn("LinkedIn URLs from DOCX hyperlinks: 1", popup_text)
                self.assertIn("LinkedIn URLs not found: 1", popup_text)
        finally:
            sys.stdout = sys.__stdout__
            
        log_text = captured_output.getvalue()
        self.assertIn("LinkedIn URLs from visible text:                 1", log_text)
        self.assertIn("LinkedIn URLs from PDF hyperlink annotations:    0", log_text)
        self.assertIn("LinkedIn URLs from DOCX hyperlinks:              1", log_text)
        self.assertIn("LinkedIn URLs not found:                         1", log_text)

    @patch("extractor.os.path.exists")
    @patch("extractor.fitz.open")
    def test_pdf_hyperlink_extraction_and_normalization(self, mock_fitz_open, mock_exists):
        from extractor import extract_pdf_hyperlinks, normalize_and_validate_candidate, ExtractorApp
        
        mock_exists.return_value = True
        
        # 1. Test extract_pdf_hyperlinks using mock fitz doc
        mock_page = MagicMock()
        mock_page.get_links.return_value = [
            {"kind": 2, "uri": "https://www.linkedin.com/in/pdf-user-in"},
            {"kind": 2, "uri": "https://www.linkedin.com/company/ignored-co"},
            {"kind": 1, "uri": "http://other-kind-ignored"}
        ]
        mock_doc = MagicMock()
        mock_doc.__iter__.return_value = [mock_page]
        mock_fitz_open.return_value = mock_doc
        
        links = extract_pdf_hyperlinks("dummy.pdf")
        self.assertIn("https://www.linkedin.com/in/pdf-user-in", links)
        self.assertIn("https://www.linkedin.com/company/ignored-co", links)
        
        # 2. Test candidate normalization & filtering (Stage 3 & 4)
        cand = {"Name": "Alok", "Email": "alok@a.com", "Phone": "123", "Country": "IN", "YearsOfExperience": "5"}
        
        # PDF filepath with candidate-only filtering (ignores company, jobs, etc.)
        # In this case: /in/ pdf-user-in should be extracted, company is ignored
        res, _ = normalize_and_validate_candidate(cand, "Plain resume text", "dummy.pdf")
        self.assertEqual(res.get("linkedin_url"), "https://www.linkedin.com/in/pdf-user-in")
        self.assertEqual(res.get("linkedin_source"), "PDF Hyperlink")
        
        # Test ignore non-candidate profiles from PDF (like /company/)
        mock_page.get_links.return_value = [
            {"kind": 2, "uri": "https://www.linkedin.com/company/ignored-company"},
            {"kind": 2, "uri": "https://www.linkedin.com/jobs/view/123"}
        ]
        res, _ = normalize_and_validate_candidate(cand, "Plain resume text", "dummy.pdf")
        self.assertEqual(res.get("linkedin_url"), "")
        self.assertEqual(res.get("linkedin_source"), "Not Found")

        # Test priority ranking: /in/ (PDF) vs /pub/ (PDF) -> Prefer /in/
        mock_page.get_links.return_value = [
            {"kind": 2, "uri": "https://www.linkedin.com/pub/pdf-pub-user/1/2/3"},
            {"kind": 2, "uri": "https://www.linkedin.com/in/pdf-in-user"}
        ]
        res, _ = normalize_and_validate_candidate(cand, "Plain resume text", "dummy.pdf")
        self.assertEqual(res.get("linkedin_url"), "https://www.linkedin.com/in/pdf-in-user")
        self.assertEqual(res.get("linkedin_source"), "PDF Hyperlink")
        
        # Test priority source order: Visible Text > PDF Hyperlink
        mock_page.get_links.return_value = [
            {"kind": 2, "uri": "https://www.linkedin.com/in/pdf-in-user"}
        ]
        res, _ = normalize_and_validate_candidate(cand, "Visible text contains linkedin.com/in/visible-in-user", "dummy.pdf")
        self.assertEqual(res.get("linkedin_url"), "https://www.linkedin.com/in/visible-in-user")
        self.assertEqual(res.get("linkedin_source"), "Visible Text")
        
        # 3. Test extraction_finished stats for PDF hyperlinks
        app = MagicMock(spec=ExtractorApp)
        app.extracted_data = [
            {"Name": "A", "Email": "a@a.com", "Phone": "123", "Country": "IN", "YearsOfExperience": "5", "linkedin_source": "Visible Text"},
            {"Name": "B", "Email": "b@b.com", "Phone": "123", "Country": "IN", "YearsOfExperience": "5", "linkedin_source": "PDF Hyperlink"},
            {"Name": "C", "Email": "c@c.com", "Phone": "123", "Country": "IN", "YearsOfExperience": "5", "linkedin_source": "DOCX Hyperlink"},
            {"Name": "D", "Email": "d@d.com", "Phone": "123", "Country": "IN", "YearsOfExperience": "5", "linkedin_source": "Not Found"}
        ]
        app.pdf_count = 1
        app.docx_count = 1
        app.doc_count = 0
        app.unsupported_count = 0
        app.skipped_files = []
        app.set_matching_progress = MagicMock()
        app.lbl_status = MagicMock()
        app.btn_export = MagicMock()
        app.btn_extract = MagicMock()
        
        import io
        import sys
        captured_output = io.StringIO()
        sys.stdout = captured_output
        try:
            app._table_dirty = False
            app.after = lambda delay, fn, *a: fn(*a)
            with patch("extractor.messagebox.showinfo") as mock_showinfo:
                ExtractorApp.extraction_finished(app, processed_count=4, folder="dummy", non_filtered_filenames=[], start_time=0)
                mock_showinfo.assert_not_called()
                popup_text = app._processing_report
                
                # Check that LinkedIn source counts are in popup text
                self.assertIn("LinkedIn URLs from visible text: 1", popup_text)
                self.assertIn("LinkedIn URLs from PDF hyperlink annotations: 1", popup_text)
                self.assertIn("LinkedIn URLs from DOCX hyperlinks: 1", popup_text)
                self.assertIn("LinkedIn URLs not found: 1", popup_text)
        finally:
            sys.stdout = sys.__stdout__
            
        log_text = captured_output.getvalue()
        self.assertIn("LinkedIn URLs from visible text:                 1", log_text)
        self.assertIn("LinkedIn URLs from PDF hyperlink annotations:    1", log_text)
        self.assertIn("LinkedIn URLs from DOCX hyperlinks:              1", log_text)
        self.assertIn("LinkedIn URLs not found:                         1", log_text)

class TestConcurrency(unittest.TestCase):
    def test_semaphore_caps_inflight(self):
        import asyncio
        state = {"cur": 0, "peak": 0}

        async def fake():
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
            await asyncio.sleep(0.01)
            state["cur"] -= 1
            return "ok"

        async def run():
            sem = asyncio.Semaphore(32)
            return await asyncio.gather(
                *(extractor.call_openai_with_retry(fake, semaphore=sem) for _ in range(100))
            )

        results = asyncio.run(run())
        self.assertEqual(len(results), 100)
        self.assertEqual(state["peak"], 32)

    def test_llm_concurrency_constant(self):
        self.assertEqual(extractor.LLM_CONCURRENCY, 32)

    def test_retry_delay_is_jittered(self):
        with patch("extractor.random.uniform", side_effect=[0.5, 1.5]):
            self.assertEqual(extractor.retry_delay(2.0, Exception()), 1.0)
            self.assertEqual(extractor.retry_delay(2.0, Exception()), 3.0)

    def test_retry_delay_honors_retry_after(self):
        exc = Exception()
        exc.response = MagicMock()
        exc.response.headers = {"retry-after": "7"}
        self.assertGreaterEqual(extractor.retry_delay(1.0, exc), 7.0)

    def test_retry_delay_ignores_non_numeric_retry_after(self):
        exc = Exception()
        exc.response = MagicMock()
        exc.response.headers = {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}
        self.assertLessEqual(extractor.retry_delay(1.0, exc), 1.5)

    def test_retry_delay_rejects_non_finite_and_caps(self):
        exc = Exception()
        exc.response = MagicMock()
        with patch("extractor.random.uniform", return_value=1.0):
            exc.response.headers = {"retry-after": "inf"}
            self.assertEqual(extractor.retry_delay(1.0, exc), 1.0)
            exc.response.headers = {"retry-after": "nan"}
            self.assertEqual(extractor.retry_delay(1.0, exc), 1.0)
            exc.response.headers = {"retry-after": "600"}
            self.assertEqual(extractor.retry_delay(1.0, exc), 60.0)

    def test_retry_semaphore_released_during_sleep(self):
        import asyncio
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("boom")
            return "ok"

        slept = []

        async def run():
            sem = asyncio.Semaphore(1)
            real_sleep = asyncio.sleep

            async def fake_sleep(d):
                slept.append((d, sem.locked()))
                await real_sleep(0)

            with patch("extractor.asyncio.sleep", fake_sleep):
                return await extractor.call_openai_with_retry(flaky, semaphore=sem)

        self.assertEqual(asyncio.run(run()), "ok")
        self.assertEqual(len(slept), 2)
        self.assertTrue(all(not locked for _, locked in slept))

    def test_no_semaphore_still_works(self):
        import asyncio

        async def fake():
            return 5

        self.assertEqual(asyncio.run(extractor.call_openai_with_retry(fake)), 5)

    def test_embeddings_and_extract_pass_semaphore(self):
        import asyncio
        from unittest.mock import AsyncMock
        sem = asyncio.Semaphore(1)
        with patch("extractor.call_openai_with_retry", new=AsyncMock(return_value=[[1.0]])) as m:
            asyncio.run(extractor.get_embeddings_batch(["a"], MagicMock(), semaphore=sem))
            self.assertIs(m.call_args.kwargs["semaphore"], sem)
        with patch("extractor.call_openai_with_retry", new=AsyncMock(return_value={"Name": "x"})) as m:
            asyncio.run(extractor.extract_data_with_ai_async("t", "f", MagicMock(), semaphore=sem))
            self.assertIs(m.call_args.kwargs["semaphore"], sem)


class TestMatchCacheAndAtomicWrites(unittest.TestCase):
    # Reuse TestExtractor's fixture (isolated cache files, mocked app) without
    # inheriting (and re-running) its tests.
    setUp = TestExtractor.setUp
    tearDown = TestExtractor.tearDown

    def _pipeline_mocks(self, stack, eval_result=None):
        import contextlib
        mocks = {}
        for name in ("extract_text_from_file", "preprocess_resume_text", "upload_all_resumes",
                     "parse_jd_with_ai", "get_embedding", "get_file_hash", "get_embeddings_batch",
                     "extract_data_with_ai_async", "evaluate_candidate_match_async",
                     "OpenAI", "AsyncOpenAI"):
            mocks[name] = stack.enter_context(patch("extractor." + name))
        stack.enter_context(patch("threading.Thread", SyncThread))
        mocks["upload_all_resumes"].return_value = {}
        mocks["extract_text_from_file"].side_effect = lambda path: "text of " + os.path.basename(path)
        mocks["preprocess_resume_text"].side_effect = lambda txt: txt
        mocks["get_file_hash"].side_effect = lambda fp: dummy_md5(os.path.basename(fp))
        mocks["parse_jd_with_ai"].return_value = extractor.ParsedJD(
            required_skills=[], preferred_skills=[], nice_to_have_technologies=[],
            experience_requirements="", minimum_years=0, education_requirements="",
            mandatory_technologies=[], domain_requirements="", seniority_expectations="",
            deal_breakers=[]
        )
        mocks["get_embedding"].return_value = [1.0, 0.0]

        async def emb(texts, client, **kwargs):
            return [[1.0, 0.0] for _ in texts]
        mocks["get_embeddings_batch"].side_effect = emb

        async def extract(text, filename, *a, **kw):
            return {"Name": "Candidate One", "Email": "one@example.com", "Phone": "123",
                    "Country": "US", "YearsOfExperience": "5", "SourceFilename": filename}
        mocks["extract_data_with_ai_async"].side_effect = extract
        mocks["evaluate_candidate_match_async"].return_value = eval_result or {
            "MatchPercentage": 85, "TechnicalMatch": 80, "ExperienceMatch": 90,
            "DomainMatch": 85, "Strengths": ["Code"], "Weaknesses": [],
            "MissingRequirements": [], "Summary": "Pass", "SkillEvidence": []
        }
        return mocks

    def _run(self, jd="JD", model="gpt-5"):
        self.app.extraction_thread("dummy_folder", ["resume1.pdf"], [], jd, "api-key", model)

    def test_atomic_write_json_preserves_old_on_failure(self):
        import drive_uploader
        path = os.path.join(self.tmp, "x.json")
        drive_uploader.atomic_write_json(path, {"a": 1})
        with self.assertRaises(TypeError):
            drive_uploader.atomic_write_json(path, {"b": object()})
        with open(path, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": 1})
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_atomic_write_json_fsyncs_before_replace(self):
        import drive_uploader
        path = os.path.join(self.tmp, "z.json")
        order = []
        real_fsync, real_replace = os.fsync, os.replace
        with patch.object(drive_uploader.os, "fsync", side_effect=lambda fd: (order.append("fsync"), real_fsync(fd))), \
                patch.object(drive_uploader.os, "replace", side_effect=lambda a, b: (order.append("replace"), real_replace(a, b))), \
                patch.object(drive_uploader.json, "dump", side_effect=AssertionError("serialise with json.dumps")):
            drive_uploader.atomic_write_json(path, {"a": [1, 2]})
        self.assertEqual(order, ["fsync", "replace"])
        with open(path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"a":[1,2]}')

    def test_atomic_write_json_fsync_failure_keeps_old(self):
        import drive_uploader
        path = os.path.join(self.tmp, "w.json")
        drive_uploader.atomic_write_json(path, {"a": 1})
        with patch.object(drive_uploader.os, "fsync", side_effect=OSError("io error")):
            with self.assertRaises(OSError):
                drive_uploader.atomic_write_json(path, {"b": 2})
        with open(path, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": 1})
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_atomic_write_json_is_compact(self):
        import drive_uploader
        path = os.path.join(self.tmp, "y.json")
        drive_uploader.atomic_write_json(path, {"a": 1, "b": [1, 2], "c": {"d": "e"}})
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("\n", raw)
        self.assertNotIn(": ", raw)
        self.assertEqual(json.loads(raw), {"a": 1, "b": [1, 2], "c": {"d": "e"}})

    def test_match_cache_skips_second_run(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            self._run()
            self._run()
            self.assertEqual(mocks["evaluate_candidate_match_async"].call_count, 1)
        row = next(r for r in self.app.extracted_data if r["SourceFilename"] == "resume1.pdf")
        self.assertEqual(row["MatchPercentage"], 85)
        self.assertEqual(row["Summary"], "Pass")

    def test_match_cache_misses_on_model_change(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            self._run(model="gpt-5")
            self._run(model="gpt-4.1")
            self.assertEqual(mocks["evaluate_candidate_match_async"].call_count, 2)

    def test_match_cache_misses_on_jd_change(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            self._run(jd="JD")
            self._run(jd="JD2")
            self.assertEqual(mocks["evaluate_candidate_match_async"].call_count, 2)

    def test_failed_evaluation_not_cached(self):
        import contextlib
        failed = {"MatchPercentage": 0, "TechnicalMatch": 0, "ExperienceMatch": 0,
                  "DomainMatch": 0, "Strengths": [], "Weaknesses": [],
                  "MissingRequirements": [], "Summary": "Evaluation Failed: boom",
                  "SkillEvidence": []}
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack, eval_result=failed)
            self._run()
            self._run()
            self.assertEqual(mocks["evaluate_candidate_match_async"].call_count, 2)

    def test_old_indented_cache_loads(self):
        import contextlib
        fh = dummy_md5("resume1.pdf")
        old = {fh: {
            "clean_text": "text of resume1.pdf",
            "res_embedding": [1.0, 0.0],
            "sections": extractor.parse_resume_sections("text of resume1.pdf"),
            "candidate_data": {"Name": "Candidate One", "Email": "one@example.com", "Phone": "123",
                               "Country": "US", "YearsOfExperience": "5", "SourceFilename": "resume1.pdf"},
        }}
        with open(extractor.RESUME_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(old, f, indent=4)
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            self._run()
            mocks["get_embeddings_batch"].assert_not_called()
            self.assertEqual(mocks["evaluate_candidate_match_async"].call_count, 1)
        with open(extractor.RESUME_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertIn(extractor.match_cache_key(fh, dummy_md5("JD"), "gpt-5"), json.loads(raw)["match_cache"])
        self.assertNotIn("\n", raw)

    def test_resume_cache_not_rewritten_when_clean(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            self._pipeline_mocks(stack)
            self._run()
            mock_write = stack.enter_context(patch("extractor.atomic_write_json"))
            self._run()
            for call in mock_write.call_args_list:
                self.assertNotEqual(call.args[0], extractor.RESUME_CACHE_FILE)

    def test_upload_queued_before_match(self):
        import contextlib
        seen = {}
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            result = dict(mocks["evaluate_candidate_match_async"].return_value)

            async def eval_side_effect(*a, **kw):
                seen["qsize"] = self.app.upload_queue.qsize()
                row = self.app.candidates_by_file.get("resume1.pdf")
                seen["row_pct"] = row.get("MatchPercentage") if row else None
                seen["row_link"] = row.get("ResumeLink") if row else None
                return dict(result)
            mocks["evaluate_candidate_match_async"].side_effect = eval_side_effect
            self._run()
        self.assertEqual(seen["qsize"], 1)
        self.assertEqual(seen["row_pct"], "Scoring\u2026")
        self.assertEqual(seen["row_link"], "Pending")
        row = next(r for r in self.app.extracted_data if r["SourceFilename"] == "resume1.pdf")
        self.assertEqual(row["MatchPercentage"], 85)

    def test_upload_success_before_match_result_is_kept(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            result = dict(mocks["evaluate_candidate_match_async"].return_value)
            handle = extractor.ExtractorApp.handle_upload_success.__get__(self.app, extractor.ExtractorApp)

            async def eval_side_effect(*a, **kw):
                handle("resume1.pdf", "http://drive/resume1", 0)
                return dict(result)
            mocks["evaluate_candidate_match_async"].side_effect = eval_side_effect
            self._run()
        row = next(r for r in self.app.extracted_data if r["SourceFilename"] == "resume1.pdf")
        self.assertEqual(row["ResumeLink"], "Uploaded")
        self.assertEqual(row["ResumeDriveURL"], "http://drive/resume1")
        self.assertEqual(row["MatchPercentage"], 85)
        self.assertEqual(row["Summary"], "Pass")

    def test_stale_cached_upload_state_never_overwrites_upload(self):
        import contextlib
        fh = dummy_md5("resume1.pdf")
        old = {fh: {
            "clean_text": "text of resume1.pdf",
            "res_embedding": [1.0, 0.0],
            "sections": extractor.parse_resume_sections("text of resume1.pdf"),
            "candidate_data": {"Name": "Candidate One", "Email": "one@example.com", "Phone": "123",
                               "Country": "US", "YearsOfExperience": "5", "SourceFilename": "resume1.pdf",
                               "ResumeDriveURL": "", "ResumeLink": "Pending"},
        }}
        with open(extractor.RESUME_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(old, f)
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            result = dict(mocks["evaluate_candidate_match_async"].return_value)
            handle = extractor.ExtractorApp.handle_upload_success.__get__(self.app, extractor.ExtractorApp)

            async def eval_side_effect(*a, **kw):
                handle("resume1.pdf", "http://drive/x", 0)
                return dict(result)
            mocks["evaluate_candidate_match_async"].side_effect = eval_side_effect
            self._run()
        row = next(r for r in self.app.extracted_data if r["SourceFilename"] == "resume1.pdf")
        self.assertEqual(row["ResumeLink"], "Uploaded")
        self.assertEqual(row["ResumeDriveURL"], "http://drive/x")
        self.assertEqual(row["MatchPercentage"], 85)
        self.assertEqual(row["Summary"], "Pass")

    def test_hash_computed_once_per_file(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            mocks["extract_text_from_file"].side_effect = (
                lambda path: None if "corrupt" in path else "text of " + os.path.basename(path))
            self.app.extraction_thread("dummy_folder", ["a.pdf", "b.pdf", "corrupt.pdf"], [], "JD", "api-key", "gpt-5")
            self.assertEqual(mocks["get_file_hash"].call_count, 3)

    def test_retry_failed_uploads_passes_none_hash(self):
        import queue
        app = self.app
        app.sv_dir = MagicMock()
        app.sv_dir.get.return_value = "dummy_folder"
        app.extracted_data = [{"SourceFilename": "f.pdf", "ResumeLink": "Failed"}]
        app.upload_queue = queue.Queue()
        app.failed_uploads = 1
        app._run_gen = 3  # retry enqueues with the CURRENT run generation
        app._drive_creds_lock = threading.Lock()
        app.retry_failed_uploads = extractor.ExtractorApp.retry_failed_uploads.__get__(app, extractor.ExtractorApp)
        app._drive_creds = "OLD-CREDS"
        app.retry_failed_uploads()
        self.assertEqual(app.upload_queue.get_nowait(),
                         (3, "f.pdf", os.path.join("dummy_folder", "f.pdf"), None))
        self.assertIsNone(app._drive_creds)  # Retry signs in again (recovers a revoked token)

    def test_match_cache_key_format(self):
        self.assertEqual(extractor.MATCH_PROMPT_VERSION, 1)
        self.assertEqual(extractor.match_cache_key("f", "j", "m"), "f:j:m:v1")
        with patch.object(extractor, "MATCH_PROMPT_VERSION", 2):
            self.assertEqual(extractor.match_cache_key("f", "j", "m"), "f:j:m:v2")


class TestUiQueue(unittest.TestCase):
    def setUp(self):
        import queue
        app = MagicMock(spec=extractor.ExtractorApp)
        app.ui_events = queue.Queue()
        app.extracted_data = []
        app.candidates_by_file = {}
        app._table_dirty = False
        app._progress_dirty = False
        app.total_uploads = 0
        app.completed_uploads = 0
        app.failed_uploads = 0
        app.cancelled_uploads = 0
        app._uploads_cancelled_gen = None
        app.uploading_files = set()
        app.filter_table = MagicMock()
        app.update_upload_progress_ui = MagicMock()
        app.after = MagicMock()
        app._closing = False
        app._run_gen = 0
        for name in ("_post", "_drain_ui_events", "_record_upload_counts", "add_or_update_candidate",
                     "handle_upload_starting", "handle_upload_success", "handle_upload_failure"):
            setattr(app, name, getattr(extractor.ExtractorApp, name).__get__(app, extractor.ExtractorApp))
        self.app = app

    def test_many_updates_one_render(self):
        app = self.app
        for i in range(50):
            app._post(lambda i=i: app.add_or_update_candidate({"SourceFilename": f"f{i}.pdf", "Name": f"N{i}"}))
        app._drain_ui_events()
        self.assertEqual(app.filter_table.call_count, 1)
        self.assertEqual(len(app.extracted_data), 50)
        self.assertEqual(len(app.candidates_by_file), 50)

    def test_update_reuses_index(self):
        app = self.app
        app._post(lambda: app.add_or_update_candidate({"SourceFilename": "a.pdf", "Name": "Old", "ResumeLink": "Pending"}))
        app._post(lambda: app.add_or_update_candidate({"SourceFilename": "a.pdf", "Name": "New", "MatchPercentage": 80}))
        app._drain_ui_events()
        self.assertEqual(len(app.extracted_data), 1)
        self.assertEqual(app.extracted_data[0]["Name"], "New")
        self.assertEqual(app.candidates_by_file["a.pdf"]["MatchPercentage"], 80)
        self.assertIs(app.candidates_by_file["a.pdf"], app.extracted_data[0])

    def test_late_upload_event_after_reset_ignored(self):
        app = self.app
        app.add_or_update_candidate({"SourceFilename": "old.pdf", "Name": "Old"})
        app.extracted_data = []
        app.candidates_by_file = {}
        app._post(lambda: app.handle_upload_success("old.pdf", "url", 0))
        app._drain_ui_events()
        self.assertEqual(app.completed_uploads, 0)

    def test_old_gen_upload_events_ignored_after_new_run(self):
        app = self.app
        # Run 0 queued a.pdf; a new run (gen 1) started and has its own a.pdf row
        app._run_gen = 1
        app.add_or_update_candidate({"SourceFilename": "a.pdf", "Name": "New", "ResumeLink": "Pending"})
        app._table_dirty = app._progress_dirty = False
        app._post(lambda: app.handle_upload_starting("a.pdf", 0))
        app._post(lambda: app.handle_upload_success("a.pdf", "http://drive/old-folder", 0))
        app._post(lambda: app.handle_upload_failure("a.pdf", RuntimeError("old"), 0))
        app._drain_ui_events()
        row = app.candidates_by_file["a.pdf"]
        self.assertEqual(row["ResumeLink"], "Pending")
        self.assertNotIn("ResumeDriveURL", row)
        self.assertEqual((app.completed_uploads, app.failed_uploads), (0, 0))
        self.assertEqual(app.uploading_files, set())
        # current-gen events still apply
        app._post(lambda: app.handle_upload_success("a.pdf", "http://drive/new", 1))
        app._drain_ui_events()
        self.assertEqual(row["ResumeLink"], "Uploaded")
        self.assertEqual(row["ResumeDriveURL"], "http://drive/new")
        self.assertEqual(app.completed_uploads, 1)

    def test_start_extraction_bumps_gen_and_passes_it_to_pipeline(self):
        app = MagicMock()
        app._run_gen = 4
        app.sv_api_key.get.return_value = "key"
        app.sv_dir.get.return_value = "dummy_folder"
        app.sv_model.get.return_value = "gpt-5"
        app.txt_jd.get.return_value = "JD"
        app.tree.get_children.return_value = []
        app.uploading_files = set()
        with patch("os.path.exists", return_value=True), \
                patch("os.listdir", return_value=["a.pdf"]), \
                patch("os.path.isfile", return_value=True), \
                patch("threading.Thread") as fake_thread:
            extractor.ExtractorApp._begin_extraction(app, "dummy_folder", ["a.pdf"], [], "JD", "key", "gpt-5")
        self.assertEqual(app._run_gen, 5)
        kwargs = fake_thread.call_args.kwargs
        self.assertEqual(kwargs["kwargs"], {"run_gen": 5})

    def _bind_status(self):
        app = self.app
        app.set_matching_progress = MagicMock()
        app.lbl_status = MagicMock()
        app._pending_progress = None
        app._pending_status = None
        app._status_dirty = False
        for name in ("_queue_status_update", "_apply_pending_status"):
            setattr(app, name, getattr(extractor.ExtractorApp, name).__get__(app, extractor.ExtractorApp))
        return app

    def test_status_updates_coalesced_per_tick(self):
        app = self._bind_status()
        for i in range(1, 51):
            app._post(lambda i=i: app._queue_status_update(progress=i / 100))
            app._post(lambda i=i: app._queue_status_update(text=f"Evaluating matches ({i}/50)..."))
        app.set_matching_progress.assert_not_called()  # nothing applied off-tick
        app._drain_ui_events()
        app.set_matching_progress.assert_called_once_with(0.5)
        app.lbl_status.configure.assert_called_once_with(text="Evaluating matches (50/50)...")
        self.assertFalse(app._status_dirty)
        app._drain_ui_events()  # clean tick: nothing re-applied
        self.assertEqual(app.set_matching_progress.call_count, 1)
        self.assertEqual(app.lbl_status.configure.call_count, 1)

    def test_extraction_finished_discards_pending_status(self):
        app = self._bind_status()
        app.extraction_finished = extractor.ExtractorApp.extraction_finished.__get__(app, extractor.ExtractorApp)
        app.btn_extract = MagicMock()
        app.btn_export = MagicMock()
        app.processing_records = []
        app.removal_missing_name = app.removal_missing_contact = app.removal_country_unresolved = 0
        app.pdf_count = app.docx_count = app.doc_count = app.unsupported_count = 0
        app.skipped_files = []
        app._post(lambda: app._queue_status_update(progress=0.99, text="Evaluating matches (9/10)..."))
        app._post(lambda: app.extraction_finished(0, "dummy", [], 0))
        with patch("extractor.messagebox"):
            app._drain_ui_events()
        self.assertEqual(app.set_matching_progress.call_args_list[-1].args, (1.0,))
        self.assertIsNone(app._pending_status)
        self.assertFalse(app._status_dirty)
        self.assertTrue(app._pipeline_finished)
        app._refresh_run_state.assert_called_once()

    def test_drain_reschedules(self):
        app = self.app
        app._drain_ui_events()
        app.after.assert_called_with(100, app._drain_ui_events)

    def test_callable_error_does_not_stop_drain(self):
        app = self.app
        seen = []

        def boom():
            raise RuntimeError("boom")
        app._post(boom)
        app._post(lambda: seen.append("ok"))
        app._drain_ui_events()
        self.assertEqual(seen, ["ok"])

    def _setup_finish(self):
        app = self.app
        for name in ("extraction_finished", "async_extraction_pipeline", "_pipeline_stopped", "_pipeline_failed", "_start_button_text"):
            setattr(app, name, getattr(extractor.ExtractorApp, name).__get__(app, extractor.ExtractorApp))
        app.chk_jd_matching = MagicMock()
        app.bv_jd_matching = MagicMock()
        app.bv_jd_matching.get.return_value = True
        app.lbl_status = MagicMock()
        app.btn_extract = MagicMock()
        app.btn_export = MagicMock()
        app.set_matching_progress = MagicMock()
        app.processing_records = []
        app.removal_missing_name = 0
        app.removal_missing_contact = 0
        app.removal_country_unresolved = 0
        app.pdf_count = app.docx_count = app.doc_count = app.unsupported_count = 0
        app.skipped_files = []
        return app

    def _zero_delay_calls(self):
        return [c for c in self.app.after.call_args_list if c.args and c.args[0] == 0]

    def test_extraction_finished_does_not_block_drain(self):
        app = self._setup_finish()
        app._post(lambda: app.add_or_update_candidate({"SourceFilename": "a.pdf", "Name": "A"}))
        app._post(lambda: app.extraction_finished(1, "dummy", [], 0))
        with patch("extractor.messagebox") as mb:
            app._drain_ui_events()
            mb.showinfo.assert_not_called()
            # Rows render and the activity report updates without a completion modal.
            self.assertGreaterEqual(app.filter_table.call_count, 1)
            zero = self._zero_delay_calls()
            self.assertEqual(len(zero), 0)
            app.after.assert_called_with(100, app._drain_ui_events)
            self.assertIn("Total Candidates Extracted", app._processing_report)

    def test_extraction_finished_renders_dirty_table_first(self):
        app = self._setup_finish()
        app._table_dirty = True
        with patch("extractor.messagebox") as mb:
            app.extraction_finished(0, "dummy", [], 0)
            mb.showinfo.assert_not_called()
        app.filter_table.assert_called_once()
        self.assertFalse(app._table_dirty)
        self.assertEqual(len(self._zero_delay_calls()), 0)

    def test_pipeline_error_popup_does_not_block_drain(self):
        import asyncio
        app = self._setup_finish()
        with patch("extractor.messagebox") as mb, \
             patch("extractor.OpenAI", side_effect=RuntimeError("no client")), \
             patch("traceback.print_exc"):
            asyncio.run(app.async_extraction_pipeline("dummy", [], [], "", "key", "m"))
            app._drain_ui_events()
            mb.showerror.assert_not_called()
            app.btn_extract.configure.assert_called_with(state="normal", text="Process resumes")
            zero = self._zero_delay_calls()
            self.assertEqual(len(zero), 1)
            app.after.assert_called_with(100, app._drain_ui_events)
            zero[0].args[1]()
            mb.showerror.assert_called_once_with("Error", "Pipeline failed: no client")

    def test_pipeline_submits_doc_batch_before_per_file_reads(self):
        import asyncio
        import concurrent.futures
        app = self._setup_finish()
        order = []
        real_cls = concurrent.futures.ThreadPoolExecutor

        class RecordingExecutor(real_cls):
            def submit(self, fn, *args, **kwargs):
                order.append(getattr(fn, "__name__", str(fn)))
                return super().submit(fn, *args, **kwargs)

        with patch("extractor.messagebox"), \
             patch("extractor.OpenAI", MagicMock()), \
             patch("extractor.AsyncOpenAI", MagicMock()), \
             patch("extractor.concurrent.futures.ThreadPoolExecutor", RecordingExecutor), \
             patch("extractor.get_file_hash", return_value="h"), \
             patch("extractor.extract_text_from_file", return_value="pdf text"), \
             patch("extractor.extract_doc_batch", return_value={}) as mock_batch, \
             patch("extractor.parse_resume_sections", side_effect=RuntimeError("stop after read")), \
             patch("traceback.print_exc"):
            asyncio.run(app.async_extraction_pipeline(
                "dummy", ["a.pdf", "b.doc", "c.pdf", "d.docx", "e.doc"], [], "", "key", "m"))
        mock_batch.assert_called_once()
        self.assertEqual(len(order), 4)
        self.assertEqual(order[0], "doc_batch_worker")
        self.assertEqual(order[1:], ["read_worker"] * 3)

    def test_log_redirector_uses_reentrant_lock(self):
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp()
        try:
            redirector = extractor.LogRedirector(os.path.join(tmp, "app.log"))
            done = threading.Event()

            def reenter():
                with redirector._lock:      # e.g. a write that triggers another write
                    redirector.write("nested\n")
                    redirector.flush()
                done.set()
            t = threading.Thread(target=reenter, daemon=True)
            t.start()
            t.join(2)
            self.assertTrue(done.is_set(), "LogRedirector deadlocked on re-entry")
            redirector.file.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_log_redirector_line_buffered(self):
        import re
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "app.log")
            redirector = extractor.LogRedirector(path)

            def writer(i):
                for n in range(200):
                    redirector.write(f"t{i}-{n}\n")
            threads = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            redirector.flush()
            redirector.file.close()
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), 400)
            for line in lines:
                self.assertRegex(line, r"^t\d-\d+$")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _tk_available():
    try:
        import tkinter as _tk
        r = _tk.Tk()
        r.withdraw()
        r.destroy()
        return True
    except Exception:
        return False


@unittest.skipUnless(_tk_available(), "Tk is not available")
class TestTableState(unittest.TestCase):
    def setUp(self):
        import types
        import tkinter as tk
        from tkinter import ttk
        self.root = tk.Tk()
        self.root.withdraw()
        cols = ("Name", "Status", "Email", "Phone", "Country", "YearsExp",
                "MatchPct", "MissingSkills", "ResumeLink")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings")
        self.tree.pack()
        app = MagicMock(spec=extractor.ExtractorApp)
        app.tree = self.tree
        app.extracted_data = []
        app.candidates_by_file = {}
        app._sort_state = None
        app.sv_search = MagicMock()
        app.sv_search.get.return_value = ""
        app.sv_min_score = MagicMock()
        app.sv_min_score.get.return_value = ""
        app.bv_jd_matching = MagicMock()
        app.bv_jd_matching.get.return_value = True
        for name in ("add_to_tree", "filter_table", "sort_table",
                     "_apply_sort", "on_row_double_click"):
            setattr(app, name, types.MethodType(
                getattr(extractor.ExtractorApp, name), app))
        self.app = app

    def tearDown(self):
        self.root.destroy()

    def _add(self, fname, pct, email=None):
        row = {
            "SourceFilename": fname,
            "Name": fname.split(".")[0],
            "Email": email or f"{fname}@x.com",
            "Phone": "123", "Country": "US", "YearsOfExperience": "5",
            "ResumeLink": "http://x", "MatchPercentage": pct,
            "MissingRequirements": [],
        }
        self.app.extracted_data.append(row)
        self.app.candidates_by_file[fname] = row
        return row

    def _pcts(self):
        return [self.tree.set(c, "MatchPct") for c in self.tree.get_children("")]

    def test_incomplete_and_parsing_rows_are_shown(self):
        self._add("a.pdf", 10)
        incomplete = {"SourceFilename": "b.pdf", "Name": "Bea", "Email": "Not Found", "Phone": "Not Found",
                      "Status": "Missing Email", "ResumeLink": "Not uploaded", "MatchPercentage": "N/A"}
        parsing = {"SourceFilename": "c.pdf", "Name": "c.pdf", "Status": "Parsing\u2026", "ResumeLink": ""}
        for row in (incomplete, parsing):
            self.app.extracted_data.append(row)
            self.app.candidates_by_file[row["SourceFilename"]] = row
        self.app.filter_table()
        self.assertEqual(set(self.tree.get_children("")), {"a.pdf", "b.pdf", "c.pdf"})
        self.assertEqual(self.tree.set("b.pdf", "Status"), "Missing Email")

    def test_duplicate_email_prefers_valid_row(self):
        self._add("a.pdf", "N/A", email="same@x.com")["Country"] = "Not Found"
        self._add("b.pdf", "N/A", email="same@x.com")
        self.app.filter_table()
        self.assertEqual(list(self.tree.get_children("")), ["b.pdf"])

    def test_sort_survives_refresh(self):
        self._add("a.pdf", 10)
        self._add("b.pdf", 90)
        self._add("c.pdf", 50)
        self.app.filter_table()
        self.app.sort_table("MatchPct", True)
        self.assertEqual(self._pcts(), ["90", "50", "10"])
        self._add("d.pdf", 70)
        self.app.filter_table()
        self.assertEqual(self._pcts(), ["90", "70", "50", "10"])

    def test_sort_toggles_same_column(self):
        self._add("a.pdf", 10)
        self._add("b.pdf", 90)
        self.app.filter_table()
        self.app.sort_table("MatchPct", True)
        self.assertEqual(self._pcts(), ["90", "10"])
        self.app.sort_table("MatchPct", True)
        self.assertEqual(self._pcts(), ["10", "90"])
        self.app.sort_table("Name", False)
        self.assertEqual(self.app._sort_state, ("Name", False, True))

    def test_selection_survives_refresh(self):
        self._add("a.pdf", 10)
        self._add("b.pdf", 90)
        self.app.filter_table()
        self.tree.selection_set("b.pdf")
        self.app.filter_table()
        self.assertEqual(self.tree.selection(), ("b.pdf",))

    def test_scoring_placeholder_does_not_crash(self):
        self.app.sv_min_score.get.return_value = "0"
        self._add("a.pdf", "Scoring\u2026")
        self.app.filter_table()
        self.assertIn("a.pdf", self.tree.get_children(""))

    def test_double_click_uses_iid(self):
        self._add("a.pdf", 10, email="same@x.com")
        second = self._add("b.pdf", 20, email="same@x.com")
        self.app.add_to_tree(self.app.extracted_data[0])
        self.app.add_to_tree(second)
        self.tree.selection_set("b.pdf")
        self.app.show_candidate_details = MagicMock()
        self.app.on_row_double_click(None)
        self.app.show_candidate_details.assert_called_once_with(second)


class TestUploadPool(unittest.TestCase):
    """Parallel upload workers (UPLOAD_WORKERS threads) sharing one queue, creds and cache."""

    def setUp(self):
        import tempfile
        import drive_uploader
        self.du = drive_uploader
        self.tmp = tempfile.mkdtemp()
        self.posted = []
        self.posted_lock = threading.Lock()
        self._patches = [
            patch.object(drive_uploader, "DRIVE_CACHE_FILE", os.path.join(self.tmp, "drive_cache.json")),
            patch.object(drive_uploader, "TOKEN_FILE", os.path.join(self.tmp, "token.pickle")),
            patch.object(drive_uploader, "get_drive_service", MagicMock()),
            patch.object(drive_uploader, "get_drive_credentials", MagicMock()),
            patch.object(drive_uploader, "_upload_single_file",
                         MagicMock(side_effect=lambda service, path, upload_name=None, should_cancel=None:
                                   "http://drive/" + os.path.basename(path))),
            patch.object(extractor, "atomic_write_json", MagicMock()),
        ]
        for p in self._patches:
            p.start()

        app = MagicMock(spec=extractor.ExtractorApp)

        def post(fn):
            with self.posted_lock:
                self.posted.append(fn)
        app._post = post
        app._run_gen = 0
        app._uploads_cancelled_gen = None
        app.cancelled_uploads = 0
        for name in ("start_upload_worker", "upload_worker_loop", "_claim_upload", "_release_upload_claim",
                     "_get_shared_drive_creds", "_save_drive_cache"):
            setattr(app, name, getattr(extractor.ExtractorApp, name).__get__(app, extractor.ExtractorApp))
        self.app = app
        if self._testMethodName in self.DEFERRED_START:
            # Build the pool without starting its threads; the test enqueues every
            # job first and then calls _start_pool(), so counts do not depend on
            # how worker gets interleave with the test's puts.
            with patch.object(threading.Thread, "start", lambda t: None):
                self.app.start_upload_worker()
        else:
            self.app.start_upload_worker()

    DEFERRED_START = {"test_drive_cache_batched", "test_each_worker_has_own_service"}

    def _start_pool(self):
        for t in self.app._upload_workers:
            t.start()

    def tearDown(self):
        import shutil
        started = [t for t in self.app._upload_workers if t.ident is not None]
        for _ in started:
            self.app.upload_queue.put(None)
        for t in started:
            t.join(2)
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _enqueue(self, n, prefix="r"):
        for i in range(n):
            name = f"{prefix}{i}.pdf"
            self.app.upload_queue.put((0, name, os.path.join(self.tmp, name), dummy_md5(name)))

    def _join(self, timeout=5.0):
        """upload_queue.join() with a guard so a bug cannot hang the suite."""
        t = threading.Thread(target=self.app.upload_queue.join, daemon=True)
        t.start()
        t.join(timeout)
        self.assertFalse(t.is_alive(), "upload queue did not drain")

    def _wait_for(self, cond, timeout=2.0):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.01)
        return cond()

    def _run_posted(self):
        with self.posted_lock:
            fns, self.posted[:] = list(self.posted), []
        for fn in fns:
            fn()

    def test_cancelled_run_jobs_skipped(self):
        self.app._uploads_cancelled_gen = 0
        self._enqueue(3)
        self._join()
        self._run_posted()
        self.du._upload_single_file.assert_not_called()
        self.assertEqual(self.app.handle_upload_cancelled.call_count, 3)
        self.app.handle_upload_failure.assert_not_called()

    def test_upload_cancelled_mid_file_reported_as_cancel(self):
        self.du._upload_single_file.side_effect = self.du.UploadCancelled("r0.pdf")
        self._enqueue(1)
        self._join()
        self._run_posted()
        self.app.handle_upload_cancelled.assert_called_once_with("r0.pdf", 0)
        self.app.handle_upload_failure.assert_not_called()

    def test_should_cancel_follows_cancelled_gen(self):
        seen = []

        def capture(service, path, upload_name=None, should_cancel=None):
            seen.append(should_cancel)
            return "http://drive/" + os.path.basename(path)
        self.du._upload_single_file.side_effect = capture
        self._enqueue(1)
        self._join()
        self.assertFalse(seen[0]())
        self.app._uploads_cancelled_gen = 0
        self.assertTrue(seen[0]())
        self.app._uploads_cancelled_gen = 1  # another run cancelled: not this job
        self.assertFalse(seen[0]())

    def test_upload_workers_constants(self):
        self.assertEqual(extractor.UPLOAD_WORKERS, 4)
        self.assertEqual(extractor.DRIVE_CACHE_SAVE_EVERY, 10)
        self.assertEqual(len(self.app._upload_workers), extractor.UPLOAD_WORKERS)

    def test_start_upload_worker_idempotent(self):
        workers = list(self.app._upload_workers)
        q = self.app.upload_queue
        self.app.start_upload_worker()
        self.assertEqual(self.app._upload_workers, workers)
        self.assertIs(self.app.upload_queue, q)

    def test_upload_workers_run_concurrently(self):
        import time

        def slow(service, path, upload_name=None, should_cancel=None):
            time.sleep(0.2)
            return "http://drive/" + os.path.basename(path)
        self.du._upload_single_file.side_effect = slow
        start = time.time()
        self._enqueue(8)
        self._join()
        elapsed = time.time() - start
        self.assertLess(elapsed, 0.8)  # serial would be 1.6s
        self._run_posted()
        self.assertEqual(self.app.handle_upload_success.call_count, 8)

    def test_each_worker_has_own_service(self):
        import time

        def slow(service, path, upload_name=None, should_cancel=None):
            time.sleep(0.1)
            return "http://drive/" + os.path.basename(path)
        self.du._upload_single_file.side_effect = slow
        self._enqueue(8)
        self._start_pool()
        self._join()
        self.assertEqual(self.du.get_drive_service.call_count, extractor.UPLOAD_WORKERS)
        creds = self.du.get_drive_credentials.return_value
        for c in self.du.get_drive_service.call_args_list:
            self.assertIs(c.kwargs.get("creds"), creds)

    def test_credentials_fetched_once(self):
        import time

        def slow_creds(*a, **kw):
            time.sleep(0.1)
            return "CREDS"
        self.du.get_drive_credentials.side_effect = slow_creds
        self._enqueue(8)
        self._join()
        self.assertEqual(self.du.get_drive_credentials.call_count, 1)
        self.assertIs(self.app._drive_creds, "CREDS")

    def test_credentials_failure_fails_job_and_retries(self):
        self.du.get_drive_credentials.side_effect = [RuntimeError("auth down"), "CREDS"]
        self.app.upload_queue.put((0, "a.pdf", os.path.join(self.tmp, "a.pdf"), dummy_md5("a.pdf")))
        self._join()
        self._run_posted()
        self.app.handle_upload_failure.assert_called_once()
        self.assertEqual(self.app.handle_upload_failure.call_args.args[0], "a.pdf")
        self.assertIsNone(self.app._drive_creds)
        self.assertTrue(self.app._drive_auth_failed)
        # Circuit breaker open: the next cache miss fails without a new sign-in attempt
        self.app.upload_queue.put((0, "b.pdf", os.path.join(self.tmp, "b.pdf"), dummy_md5("b.pdf")))
        self._join()
        self._run_posted()
        self.assertEqual(self.app.handle_upload_failure.call_count, 2)
        self.assertEqual(self.du.get_drive_credentials.call_count, 1)
        # Once reset (as retry/start do), credentials are fetched again and memoised
        self.app._drive_auth_failed = False
        self.app.upload_queue.put((0, "c.pdf", os.path.join(self.tmp, "c.pdf"), dummy_md5("c.pdf")))
        self._join()
        self._run_posted()
        self.app.handle_upload_success.assert_called_once_with("c.pdf", "http://drive/c.pdf", 0)
        self.assertEqual(self.du.get_drive_credentials.call_count, 2)

    def test_auth_circuit_breaker_one_attempt_for_many_jobs(self):
        self.du.get_drive_credentials.side_effect = RuntimeError("auth down")
        self._enqueue(8)
        self._join()
        self._run_posted()
        self.assertEqual(self.du.get_drive_credentials.call_count, 1)
        self.assertEqual(self.app.handle_upload_failure.call_count, 8)
        self.assertEqual({c.args[0] for c in self.app.handle_upload_failure.call_args_list},
                         {f"r{i}.pdf" for i in range(8)})
        errors = [str(c.args[1]) for c in self.app.handle_upload_failure.call_args_list]
        self.assertEqual(sum("Google Drive sign-in" in e for e in errors), 7)
        self.du._upload_single_file.assert_not_called()
        # Cache hits still succeed while the breaker is open
        h = dummy_md5("hit.pdf")
        self.app._drive_cache[h] = "http://drive/old"
        self.app.upload_queue.put((0, "hit.pdf", os.path.join(self.tmp, "hit.pdf"), h))
        self._join()
        self._run_posted()
        self.app.handle_upload_success.assert_called_once_with("hit.pdf", "http://drive/old", 0)
        self.assertEqual(self.du.get_drive_credentials.call_count, 1)

    def test_auth_breaker_fast_path_no_traceback(self):
        import io
        import contextlib
        self.app._drive_auth_failed = True
        out = io.StringIO()
        with patch("traceback.print_exc") as pe, contextlib.redirect_stdout(out):
            self._enqueue(3)
            self._join()
        self._run_posted()
        pe.assert_not_called()
        self.du.get_drive_credentials.assert_not_called()
        self.assertEqual(self.app.handle_upload_failure.call_count, 3)
        self.app.handle_upload_starting.assert_not_called()
        self.assertTrue(all("Google Drive sign-in" in str(c.args[1])
                            for c in self.app.handle_upload_failure.call_args_list))
        skip_lines = [l for l in out.getvalue().splitlines() if l.startswith("[Upload] Skipped ")]
        self.assertEqual(len(skip_lines), 3, out.getvalue())

    def test_retry_failed_uploads_resets_auth_breaker(self):
        self.du.get_drive_credentials.side_effect = [RuntimeError("auth down"), "CREDS"]
        self.app.upload_queue.put((0, "a.pdf", os.path.join(self.tmp, "a.pdf"), dummy_md5("a.pdf")))
        self._join()
        self._run_posted()
        self.assertTrue(self.app._drive_auth_failed)
        self.app.extracted_data = [{"SourceFilename": "a.pdf", "ResumeLink": "Failed"}]
        self.app.sv_dir = MagicMock()
        self.app.sv_dir.get.return_value = self.tmp
        self.app.failed_uploads = 1
        self.app.retry_failed_uploads = extractor.ExtractorApp.retry_failed_uploads.__get__(
            self.app, extractor.ExtractorApp)
        self.app.retry_failed_uploads()
        self._join()
        self._run_posted()
        self.assertEqual(self.du.get_drive_credentials.call_count, 2)
        self.assertFalse(self.app._drive_auth_failed)
        self.app.handle_upload_success.assert_called_once_with("a.pdf", "http://drive/a.pdf", 0)

    def _bind_retry(self, failed_names):
        self.app.extracted_data = [{"SourceFilename": n, "ResumeLink": "Failed"} for n in failed_names]
        self.app.sv_dir = MagicMock()
        self.app.sv_dir.get.return_value = self.tmp
        self.app.failed_uploads = len(failed_names)
        self.app.retry_failed_uploads = extractor.ExtractorApp.retry_failed_uploads.__get__(
            self.app, extractor.ExtractorApp)

    def test_retry_rebuilds_worker_service_with_new_creds(self):
        # Leave one worker so the same thread (and its cached service) handles every job
        for _ in range(extractor.UPLOAD_WORKERS - 1):
            self.app.upload_queue.put(None)
        self._join()
        self.du.get_drive_credentials.side_effect = ["C1", "C2"]
        self.du.get_drive_service.side_effect = lambda force_reauth=False, creds=None: "svc-" + creds
        for n in ("a.pdf", "b.pdf"):
            self.app.upload_queue.put((0, n, os.path.join(self.tmp, n), dummy_md5(n)))
            self._join()
        self._bind_retry(["c.pdf"])
        self.app.retry_failed_uploads()
        self._join()
        services = [c.args[0] for c in self.du._upload_single_file.call_args_list]
        self.assertEqual(services, ["svc-C1", "svc-C1", "svc-C2"])
        self.assertEqual(self.du.get_drive_credentials.call_count, 2)
        self.assertEqual(self.du.get_drive_service.call_count, 2)

    def test_retry_does_not_block_on_inflight_sign_in(self):
        gate, entered = threading.Event(), threading.Event()

        def slow_creds(*a, **kw):
            entered.set()
            gate.wait(5)
            return "CREDS"
        self.du.get_drive_credentials.side_effect = slow_creds
        self.app.upload_queue.put((0, "a.pdf", os.path.join(self.tmp, "a.pdf"), dummy_md5("a.pdf")))
        self.assertTrue(entered.wait(2))
        self._bind_retry(["x.pdf"])
        t = threading.Thread(target=self.app.retry_failed_uploads, daemon=True)
        t.start()
        t.join(1)
        blocked = t.is_alive()
        gate.set()
        t.join(2)
        self._join()
        self.assertFalse(blocked, "Retry (main thread) blocked behind an in-progress Drive sign-in")
        self.assertEqual(self.du._upload_single_file.call_count, 2)

    def test_drive_cache_batched(self):
        self._enqueue(25)
        self._start_pool()
        self._join()
        write = extractor.atomic_write_json
        self.assertTrue(self._wait_for(lambda: write.call_count >= 3))
        self._wait_for(lambda: write.call_count > 3, timeout=0.2)
        self.assertEqual(write.call_count, 3)  # at 10, at 20, then on drain
        path, data = write.call_args.args
        self.assertEqual(path, self.du.DRIVE_CACHE_FILE)
        self.assertEqual(len(data), 25)
        self.assertEqual(len(self.app._drive_cache), 25)

    def test_cache_hits_do_not_write_cache(self):
        self.app._drive_cache[dummy_md5("hit.pdf")] = "http://drive/old"
        self.app.upload_queue.put((0, "hit.pdf", os.path.join(self.tmp, "hit.pdf"), dummy_md5("hit.pdf")))
        self._join()
        self._wait_for(lambda: extractor.atomic_write_json.call_count > 0, timeout=0.2)
        extractor.atomic_write_json.assert_not_called()

    def test_cache_write_error_does_not_kill_workers(self):
        extractor.atomic_write_json.side_effect = OSError("disk full")
        self._enqueue(12)
        self._join()
        self._enqueue(4, prefix="s")
        self._join()
        self._run_posted()
        self.assertEqual(self.app.handle_upload_success.call_count, 16)
        self.assertTrue(all(t.is_alive() for t in self.app._upload_workers))

    def test_worker_survives_job_exception(self):
        self.du._upload_single_file.side_effect = [RuntimeError("boom")] + [
            "http://drive/ok"] * 10
        self.app.upload_queue.put(("bad-job",))  # malformed job tuple
        self._enqueue(5)
        self._join()
        self._run_posted()
        self.assertEqual(self.app.handle_upload_failure.call_count, 1)
        self.assertEqual(self.app.handle_upload_success.call_count, 4)
        self.assertTrue(all(t.is_alive() for t in self.app._upload_workers))

    def test_none_hash_is_computed(self):
        with patch.object(self.du, "get_file_hash", MagicMock(return_value="h1")) as gfh:
            self.app.upload_queue.put((0, "n.pdf", os.path.join(self.tmp, "n.pdf"), None))
            self._join()
        gfh.assert_called_once_with(os.path.join(self.tmp, "n.pdf"))
        self.assertEqual(self.app._drive_cache.get("h1"), "http://drive/n.pdf")

    def test_stale_gen_job_skipped(self):
        self.app._run_gen = 2
        self.app.upload_queue.put((1, "old.pdf", os.path.join(self.tmp, "old.pdf"), dummy_md5("old.pdf")))
        self.app.upload_queue.put((2, "new.pdf", os.path.join(self.tmp, "new.pdf"), dummy_md5("new.pdf")))
        self._join()
        self._run_posted()
        self.assertEqual(self.du._upload_single_file.call_count, 1)
        self.assertEqual(self.du._upload_single_file.call_args.args[1], os.path.join(self.tmp, "new.pdf"))
        self.app.handle_upload_success.assert_called_once_with("new.pdf", "http://drive/new.pdf", 2)
        self.app.handle_upload_starting.assert_called_once_with("new.pdf", 2)
        self.app.handle_upload_failure.assert_not_called()

    def _bind_real_handlers(self, names):
        app = self.app
        app.candidates_by_file = {n: {"SourceFilename": n, "ResumeLink": "Pending"} for n in names}
        app.uploading_files = set()
        app.completed_uploads = app.failed_uploads = 0
        for name in ("handle_upload_starting", "handle_upload_success", "handle_upload_failure"):
            setattr(app, name, getattr(extractor.ExtractorApp, name).__get__(app, extractor.ExtractorApp))

    def test_same_hash_in_flight_uploaded_once(self):
        import time
        started = threading.Event()

        def slow(service, path, upload_name=None, should_cancel=None):
            started.set()
            time.sleep(0.2)
            return "http://drive/shared"
        self.du._upload_single_file.side_effect = slow
        self._bind_real_handlers(["a.pdf", "b.pdf"])
        h = dummy_md5("same content")
        self.app.upload_queue.put((0, "a.pdf", os.path.join(self.tmp, "a.pdf"), h))
        self.assertTrue(started.wait(2))
        self.app.upload_queue.put((0, "b.pdf", os.path.join(self.tmp, "b.pdf"), h))
        self._join()
        self._run_posted()
        self.assertEqual(self.du._upload_single_file.call_count, 1)
        for n in ("a.pdf", "b.pdf"):
            row = self.app.candidates_by_file[n]
            self.assertEqual(row["ResumeLink"], "Uploaded")
            self.assertEqual(row["ResumeDriveURL"], "http://drive/shared")
        self.assertEqual(self.app.completed_uploads, 2)
        self.assertEqual(self.app._inflight_hashes, {})

    def test_same_hash_waiter_uploads_itself_if_first_fails(self):
        import time
        started = threading.Event()
        calls = []

        def first_fails(service, path, upload_name=None, should_cancel=None):
            calls.append(path)
            if len(calls) == 1:
                started.set()
                time.sleep(0.2)
                raise RuntimeError("network")
            return "http://drive/second"
        self.du._upload_single_file.side_effect = first_fails
        self._bind_real_handlers(["a.pdf", "b.pdf"])
        h = dummy_md5("same content")
        with patch("traceback.print_exc"):
            self.app.upload_queue.put((0, "a.pdf", os.path.join(self.tmp, "a.pdf"), h))
            self.assertTrue(started.wait(2))
            self.app.upload_queue.put((0, "b.pdf", os.path.join(self.tmp, "b.pdf"), h))
            self._join()
        self._run_posted()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.app.candidates_by_file["a.pdf"]["ResumeLink"], "Failed")
        self.assertEqual(self.app.candidates_by_file["b.pdf"]["ResumeLink"], "Uploaded")
        self.assertEqual(self.app.candidates_by_file["b.pdf"]["ResumeDriveURL"], "http://drive/second")
        self.assertEqual(self.app._inflight_hashes, {})

    def test_no_global_upload_lock(self):
        self.assertFalse(hasattr(self.du, "upload_lock"))

    def test_cache_hit_skips_upload(self):
        h = dummy_md5("cached.pdf")
        self.app._drive_cache[h] = "url"
        self.app.upload_queue.put((0, "cached.pdf", os.path.join(self.tmp, "cached.pdf"), h))
        self._join()
        self._run_posted()
        self.du._upload_single_file.assert_not_called()
        self.app.handle_upload_success.assert_called_once_with("cached.pdf", "url", 0)
        self.app.handle_upload_starting.assert_not_called()

    def test_drive_cache_loaded_at_start_tolerates_corrupt_file(self):
        with open(self.du.DRIVE_CACHE_FILE, "w", encoding="utf-8") as f:
            f.write("{not json")
        app = MagicMock(spec=extractor.ExtractorApp)
        app.start_upload_worker = extractor.ExtractorApp.start_upload_worker.__get__(app, extractor.ExtractorApp)
        with patch("threading.Thread") as fake_thread:
            app.start_upload_worker()
        self.assertEqual(app._drive_cache, {})
        self.assertEqual(fake_thread.call_count, extractor.UPLOAD_WORKERS)
        with open(self.du.DRIVE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"abc": "http://drive/x"}, f)
        app2 = MagicMock(spec=extractor.ExtractorApp)
        app2.start_upload_worker = extractor.ExtractorApp.start_upload_worker.__get__(app2, extractor.ExtractorApp)
        with patch("threading.Thread"):
            app2.start_upload_worker()
        self.assertEqual(app2._drive_cache, {"abc": "http://drive/x"})


class TestCancelUploadsAndJdToggle(unittest.TestCase):
    """Cancel Uploads, retry of cancelled uploads, the JD matching checkbox and upload status text."""

    def setUp(self):
        import queue
        app = MagicMock(spec=extractor.ExtractorApp)
        app.upload_queue = queue.Queue()
        app.extracted_data = []
        app.candidates_by_file = {}
        app.uploading_files = set()
        app.total_uploads = app.completed_uploads = app.failed_uploads = app.cancelled_uploads = 0
        app._table_dirty = app._progress_dirty = False
        app._run_gen = 2
        app._uploads_cancelled_gen = None
        app._pipeline_running = False
        app._drive_creds_lock = threading.Lock()
        app.lbl_status = MagicMock()
        for name in ("cancel_uploads", "handle_upload_cancelled", "retry_failed_uploads",
                     "add_or_update_candidate", "update_upload_progress_ui", "_start_button_text",
                     "on_jd_matching_toggled", "start_extraction"):
            setattr(app, name, getattr(extractor.ExtractorApp, name).__get__(app, extractor.ExtractorApp))
        self.app = app

    def _row(self, name, link):
        row = {"SourceFilename": name, "Name": name, "ResumeLink": link}
        self.app.extracted_data.append(row)
        self.app.candidates_by_file[name] = row
        return row

    def test_cancel_drops_queued_jobs_and_marks_rows(self):
        a, b = self._row("a.pdf", "Pending"), self._row("b.pdf", "Pending")
        self.app.upload_queue.put((2, "a.pdf", "/x/a.pdf", "ha"))
        self.app.upload_queue.put((2, "b.pdf", "/x/b.pdf", "hb"))
        self.app.upload_queue.put(None)  # a worker stop sentinel survives the drain
        self.app.cancel_uploads()
        self.assertEqual(self.app._uploads_cancelled_gen, 2)
        self.assertEqual((a["ResumeLink"], b["ResumeLink"]), ("Cancelled", "Cancelled"))
        self.assertEqual(self.app.cancelled_uploads, 2)
        self.assertIsNone(self.app.upload_queue.get_nowait())
        self.assertTrue(self.app.upload_queue.empty())

    def test_cancel_counts_each_row_once(self):
        self._row("a.pdf", "Pending")
        self.app.handle_upload_cancelled("a.pdf", 2)
        self.app.handle_upload_cancelled("a.pdf", 2)
        self.app.handle_upload_cancelled("a.pdf", 1)  # stale run
        self.assertEqual(self.app.cancelled_uploads, 1)

    def test_cancel_with_nothing_in_progress(self):
        self._row("a.pdf", "Uploaded")
        with patch("extractor.messagebox") as mb:
            self.app.cancel_uploads()
        mb.showinfo.assert_called_once()
        self.assertIsNone(self.app._uploads_cancelled_gen)

    def test_retry_requeues_cancelled_and_failed(self):
        self.app.sv_dir = MagicMock()
        self.app.sv_dir.get.return_value = "folder"
        self._row("a.pdf", "Cancelled")
        self._row("b.pdf", "Failed")
        self._row("c.pdf", "Uploaded")
        self.app.cancelled_uploads, self.app.failed_uploads = 1, 1
        self.app._uploads_cancelled_gen = 2
        self.app.retry_failed_uploads()
        self.assertIsNone(self.app._uploads_cancelled_gen)
        self.assertEqual((self.app.cancelled_uploads, self.app.failed_uploads), (0, 0))
        queued = [self.app.upload_queue.get_nowait()[1] for _ in range(2)]
        self.assertEqual(sorted(queued), ["a.pdf", "b.pdf"])
        self.assertTrue(self.app.upload_queue.empty())

    def test_parsed_rows_visible_whatever_upload_state(self):
        cand = {"Name": "A", "Email": "a@x.com", "Phone": "1", "Country": "India", "YearsOfExperience": "3"}
        for link in ("Pending", "Uploading", "Failed", "Cancelled", "Uploaded"):
            row = dict(cand, ResumeLink=link)
            self.assertTrue(extractor.is_valid_candidate(row, allow_pending=True), link)
            self.assertEqual(extractor.is_valid_candidate(row), link == "Uploaded", link)

    def test_not_uploaded_rows_never_exported(self):
        row = {"Name": "A", "Email": "a@x.com", "Phone": "1", "Country": "India", "YearsOfExperience": "3",
               "ResumeLink": "Not uploaded"}
        self.assertFalse(extractor.is_valid_candidate(row))
        self.assertFalse(extractor.is_valid_candidate(row, allow_pending=True))

    def _progress_widgets(self):
        for name in ("lbl_upload_pct", "lbl_uploaded_cnt", "lbl_uploading_cnt",
                     "lbl_failed_cnt", "lbl_cancelled_cnt", "lbl_total_cnt", "lbl_curr_upload"):
            setattr(self.app, name, MagicMock())

    def test_upload_status_when_nothing_to_upload(self):
        self._progress_widgets()
        self.app._pipeline_running = True
        self.app.update_upload_progress_ui()
        self.app.lbl_curr_upload.configure.assert_called_with(text="Waiting for parsed resumes\u2026")
        self.app._pipeline_running = False
        self.app.update_upload_progress_ui()
        self.app.lbl_curr_upload.configure.assert_called_with(text="Nothing to upload (no valid candidates)")
        self.app.lbl_upload_pct.configure.assert_called_with(text="")

    def test_cancelled_counts_toward_upload_progress(self):
        self._progress_widgets()
        self.app.total_uploads, self.app.completed_uploads, self.app.cancelled_uploads = 4, 2, 2
        self.app.update_upload_progress_ui()
        self.app._render_progress.assert_called()
        self.assertEqual(extractor.run_progress(1.0, 4, 4, False, True), 1.0)
        self.app.lbl_curr_upload.configure.assert_called_with(text="Currently uploading: None")

    def test_jd_toggle_disables_jd_inputs(self):
        self.app.bv_jd_matching = MagicMock()
        self.app.bv_jd_matching.get.return_value = False
        self.app.txt_jd, self.app.btn_upload_jd, self.app.btn_extract = MagicMock(), MagicMock(), MagicMock()
        self.app.on_jd_matching_toggled()
        self.app.txt_jd.configure.assert_called_with(state="disabled")
        self.app.btn_upload_jd.configure.assert_called_with(state="disabled")
        self.app.btn_extract.configure.assert_called_with(text="Process resumes")

    def test_start_without_jd_matching_passes_empty_jd(self):
        import tempfile
        import shutil
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        open(os.path.join(folder, "a.pdf"), "w").close()
        app = self.app
        app._closing = False
        app.sv_api_key, app.sv_dir, app.sv_model = MagicMock(), MagicMock(), MagicMock()
        app.sv_api_key.get.return_value = "key"
        app.sv_dir.get.return_value = folder
        app.sv_model.get.return_value = "m"
        app.txt_jd = MagicMock()
        app.txt_jd.get.return_value = "Senior Python engineer"
        app.bv_jd_matching = MagicMock()
        app.tree = MagicMock()
        app.tree.get_children.return_value = []
        for name in ("btn_extract", "btn_export", "chk_jd_matching"):
            setattr(app, name, MagicMock())
        for enabled, expected in ((False, ""), (True, "Senior Python engineer")):
            app.bv_jd_matching.get.return_value = enabled
            app._pipeline_running = False
            with patch("extractor.threading.Thread") as thread:
                app.start_extraction()
            self.assertEqual(thread.call_args.kwargs["args"][1], expected)


class TestDriveUploadChunksAndFallback(unittest.TestCase):
    def setUp(self):
        import tempfile
        import drive_uploader
        self.du = drive_uploader
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "r.pdf")
        with open(self.path, "wb") as f:
            f.write(b"%PDF-1.4 test")
        self._root = patch.object(drive_uploader, "_use_root_parent", False)
        self._root.start()

    def tearDown(self):
        import shutil
        self._root.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _service(self, chunks):
        service = MagicMock()
        request = service.files.return_value.create.return_value
        request.next_chunk.side_effect = chunks
        return service, request

    def test_chunked_upload_returns_link(self):
        service, request = self._service([(MagicMock(), None), (None, {"id": "abc"})])
        link = self.du._upload_single_file_internal(service, self.path)
        self.assertEqual(link, "https://drive.google.com/file/d/abc/view")
        self.assertEqual(request.next_chunk.call_count, 2)
        request.execute.assert_not_called()

    def test_should_cancel_stops_before_next_chunk(self):
        service, request = self._service([(MagicMock(), None), (None, {"id": "abc"})])
        calls = []

        def should_cancel():
            calls.append(1)
            return len(calls) > 1
        with self.assertRaises(self.du.UploadCancelled):
            self.du._upload_single_file(service, self.path, should_cancel=should_cancel)
        self.assertEqual(request.next_chunk.call_count, 1)
        service.permissions.assert_not_called()

    def test_parent_permission_fallback_is_remembered(self):
        parents = []

        def fake_internal(service, path, upload_name, parent_id, should_cancel=None):
            parents.append(parent_id)
            if parent_id != "root":
                raise RuntimeError("insufficientParentPermissions")
            return "http://drive/x"
        with patch.object(self.du, "_upload_single_file_internal", side_effect=fake_internal), \
             patch("builtins.print"), patch("traceback.print_exc"):
            self.du._upload_single_file(MagicMock(), self.path)
            self.du._upload_single_file(MagicMock(), self.path)
        self.assertEqual(parents, [self.du.DRIVE_FOLDER_ID, "root", "root"])

    def test_drive_service_has_request_timeout(self):
        with patch.object(self.du, "build") as build:
            self.du.get_drive_service(creds=MagicMock())
        http = build.call_args.kwargs["http"]
        self.assertEqual(http.http.timeout, self.du.HTTP_TIMEOUT_S)

    def test_oauth_sign_in_times_out(self):
        flow = MagicMock()
        with patch.object(self.du, "TOKEN_FILE", os.path.join(self.tmp, "token.pickle")), \
             patch.object(self.du.InstalledAppFlow, "from_client_secrets_file", return_value=flow), \
             patch.object(self.du.pickle, "dump"):
            self.du.get_drive_credentials(force_reauth=True)
        flow.run_local_server.assert_called_once_with(port=0, timeout_seconds=self.du.OAUTH_TIMEOUT_S)


class TestStability(unittest.TestCase):
    """Task 9: per-candidate failure isolation, phased progress, cache checkpoints, clean close."""
    setUp = TestExtractor.setUp
    tearDown = TestExtractor.tearDown
    _pipeline_mocks = TestMatchCacheAndAtomicWrites._pipeline_mocks

    def _run_files(self, files, jd="JD", model="gpt-5"):
        self.app.extraction_thread("dummy_folder", files, [], jd, "api-key", model)

    def _row(self, fname):
        return next(r for r in self.app.extracted_data if r["SourceFilename"] == fname)

    def test_match_task_exception_isolated(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            good = dict(mocks["evaluate_candidate_match_async"].return_value)

            def fake_eval(profile, jd, cand_data, *a, **kw):
                if cand_data.get("SourceFilename") == "a.pdf":
                    raise RuntimeError("boom")
                return dict(good)
            mocks["evaluate_candidate_match_async"].side_effect = fake_eval
            finished = MagicMock(wraps=self.app.extraction_finished)
            self.app.extraction_finished = finished
            pe = stack.enter_context(patch("traceback.print_exc"))
            self._run_files(["a.pdf", "b.pdf"])
        finished.assert_called_once()
        pe.assert_called()
        a, b = self._row("a.pdf"), self._row("b.pdf")
        self.assertEqual(a["MatchPercentage"], 0)
        self.assertTrue(a["Summary"].startswith("Evaluation Failed"))
        self.assertIn("boom", a["Summary"])
        self.assertEqual(b["MatchPercentage"], 85)
        # a failed evaluation never reaches the match cache
        with open(extractor.RESUME_CACHE_FILE, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["match_cache"]), 1)

    def test_phase_progress(self):
        self.assertEqual(extractor.PROGRESS_PHASES, {
            "read": (0.0, 0.20), "embed": (0.20, 0.30),
            "extract": (0.30, 0.40), "match": (0.40, 1.00)})
        self.assertAlmostEqual(extractor.phase_progress("match", 0, 10), 0.40)
        self.assertAlmostEqual(extractor.phase_progress("match", 5, 10), 0.70)
        self.assertAlmostEqual(extractor.phase_progress("match", 10, 10), 1.00)
        self.assertAlmostEqual(extractor.phase_progress("embed", 0, 0), 0.20)

    def test_progress_monotonic(self):
        import contextlib
        values = []
        self.app.set_matching_progress = MagicMock(side_effect=values.append)
        with contextlib.ExitStack() as stack:
            self._pipeline_mocks(stack)
            self._run_files([f"r{i}.pdf" for i in range(6)])
        self.assertTrue(values)
        self.assertEqual(values, sorted(values), values)
        self.assertEqual(values[-1], 1.0)
        self.assertTrue(any(0.40 < v < 1.0 for v in values), values)
        lbl_texts = [c.kwargs.get("text", "") for c in self.app.lbl_status.configure.call_args_list]
        self.assertTrue(any("Evaluating matches (3/6)..." in t for t in lbl_texts), lbl_texts)

    def test_cache_checkpointed_midrun(self):
        import contextlib
        import copy
        self.assertEqual(extractor.CACHE_CHECKPOINT_EVERY, 25)
        self.assertEqual(extractor.STAGE2_TOP_N, 15)
        self.assertEqual(extractor.STAGE2_THRESHOLD, 0.35)
        writes = []

        def recorder(path, data):
            writes.append((path, copy.deepcopy(data)))
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            stack.enter_context(patch.object(extractor, "STAGE2_TOP_N", 60))
            stack.enter_context(patch.object(extractor, "STAGE2_THRESHOLD", 2.0))  # only TOP_N selects
            stack.enter_context(patch.object(extractor, "atomic_write_json", side_effect=recorder))
            mocks["evaluate_candidate_match_async"].side_effect = self._gated_eval(writes)
            self._run_files([f"r{i:02d}.pdf" for i in range(60)])
        self.assertEqual(mocks["evaluate_candidate_match_async"].call_count, 60)
        cache_writes = [d for p, d in writes if p == extractor.RESUME_CACHE_FILE]
        # post-extract checkpoint, match checkpoints at 25 and 50, final
        self.assertGreaterEqual(len(cache_writes), 4)
        self.assertEqual(cache_writes[0]["match_cache"], {})
        self.assertEqual(len([h for h in cache_writes[0] if h != "match_cache"]), 60)
        self.assertGreaterEqual(len(cache_writes[2]["match_cache"]), 50)
        self.assertEqual(len(cache_writes[-1]["match_cache"]), 60)

    def _gated_eval(self, writes, delay_after_gate=0.05, base_writes=1):
        """Eval mock: candidate rNN waits until base_writes (the post-extract checkpoint)
        + NN // 25 resume-cache writes are done (then a short settle), so each match
        checkpoint finishes before the next batch."""
        import asyncio
        import time
        good = {"MatchPercentage": 85, "TechnicalMatch": 80, "ExperienceMatch": 90,
                "DomainMatch": 85, "Strengths": ["Code"], "Weaknesses": [],
                "MissingRequirements": [], "Summary": "Pass", "SkillEvidence": []}

        async def fake_eval(profile, jd, cand_data, *a, **kw):
            idx = int(cand_data["SourceFilename"][1:3])
            need = idx // extractor.CACHE_CHECKPOINT_EVERY
            if need:
                need += base_writes
                deadline = time.monotonic() + 5
                while len(writes) < need and time.monotonic() < deadline:
                    await asyncio.sleep(0.002)
                await asyncio.sleep(delay_after_gate)
            return dict(good)
        return fake_eval

    def test_checkpoint_does_not_block_event_loop(self):
        import asyncio
        import contextlib
        import time
        spans, eval_done = [], []

        def slow_write(path, data):
            start = time.monotonic()
            time.sleep(0.2)
            spans.append((path, start, time.monotonic()))

        async def fake_eval(profile, jd, cand_data, *a, **kw):
            if int(cand_data["SourceFilename"][1:3]) >= extractor.CACHE_CHECKPOINT_EVERY:
                await asyncio.sleep(0.01)
            eval_done.append(time.monotonic())
            return {"MatchPercentage": 85, "TechnicalMatch": 80, "ExperienceMatch": 90,
                    "DomainMatch": 85, "Strengths": [], "Weaknesses": [],
                    "MissingRequirements": [], "Summary": "Pass", "SkillEvidence": []}
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            stack.enter_context(patch.object(extractor, "STAGE2_TOP_N", 30))
            stack.enter_context(patch.object(extractor, "STAGE2_THRESHOLD", 2.0))
            stack.enter_context(patch.object(extractor, "atomic_write_json", side_effect=slow_write))
            mocks["evaluate_candidate_match_async"].side_effect = fake_eval
            self._run_files([f"r{i:02d}.pdf" for i in range(30)])
        cache_spans = [(a, b) for p, a, b in spans if p == extractor.RESUME_CACHE_FILE]
        self.assertGreaterEqual(len(cache_spans), 3)  # post-extract + match checkpoint + final
        start, end = cache_spans[1]  # the mid-match checkpoint
        during = [t for t in eval_done if start < t < end]
        self.assertGreaterEqual(len(during), 1, "event loop stalled during the checkpoint write")

    def test_failed_checkpoint_keeps_dirty_and_final_writes(self):
        import contextlib
        import copy
        attempts = []

        def flaky(path, data):
            attempts.append((path, copy.deepcopy(data)))
            if path == extractor.RESUME_CACHE_FILE and len(
                    [p for p, _ in attempts if p == extractor.RESUME_CACHE_FILE]) == 1:
                raise OSError("disk full")
        with contextlib.ExitStack() as stack:
            self._pipeline_mocks(stack)
            stack.enter_context(patch.object(extractor, "STAGE2_TOP_N", 25))
            stack.enter_context(patch.object(extractor, "STAGE2_THRESHOLD", 2.0))
            stack.enter_context(patch.object(extractor, "atomic_write_json", side_effect=flaky))
            # 25 candidates: the checkpoint coincides with the last task, so only the
            # dirty flag restored by the failed write can trigger the final write.
            self._run_files([f"r{i:02d}.pdf" for i in range(25)])
        cache_attempts = [d for p, d in attempts if p == extractor.RESUME_CACHE_FILE]
        self.assertEqual(len(cache_attempts), 2)
        self.assertEqual(len(cache_attempts[-1]["match_cache"]), 25)

    def test_snapshot_isolated_from_later_candidate_updates(self):
        """The checkpoint snapshot copies per-file entries and candidate_data, so
        candidates scored on the loop while the write runs never leak into it."""
        import asyncio
        import contextlib
        import time
        payloads = []

        def slow_capture(path, data):
            if path == extractor.RESUME_CACHE_FILE:
                if len(payloads) == 1:  # the mid-match checkpoint (after post-extract)
                    time.sleep(0.2)  # later candidates get scored meanwhile
                payloads.append(json.dumps(data, sort_keys=True))  # serialise at write time

        async def fake_eval(profile, jd, cand_data, *a, **kw):
            if int(cand_data["SourceFilename"][1:3]) >= extractor.CACHE_CHECKPOINT_EVERY:
                await asyncio.sleep(0.01)
            return {"MatchPercentage": 85, "TechnicalMatch": 80, "ExperienceMatch": 90,
                    "DomainMatch": 85, "Strengths": [], "Weaknesses": [],
                    "MissingRequirements": [], "Summary": "Pass", "SkillEvidence": []}
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            stack.enter_context(patch.object(extractor, "STAGE2_TOP_N", 30))
            stack.enter_context(patch.object(extractor, "STAGE2_THRESHOLD", 2.0))
            stack.enter_context(patch.object(extractor, "atomic_write_json", side_effect=slow_capture))
            mocks["evaluate_candidate_match_async"].side_effect = fake_eval
            self._run_files([f"r{i:02d}.pdf" for i in range(30)])
        self.assertEqual(json.loads(payloads[0])["match_cache"], {})  # post-extract checkpoint
        first = json.loads(payloads[1])
        self.assertEqual(len(first["match_cache"]), 25)
        scored = [h for h, e in first.items() if h != "match_cache"
                  and "MatchPercentage" in (e.get("candidate_data") or {})]
        self.assertEqual(len(scored), 25)
        self.assertEqual(len(json.loads(payloads[-1])["match_cache"]), 30)

    def test_doc_batch_failure_logged_with_traceback(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            self._pipeline_mocks(stack)
            stack.enter_context(patch.object(extractor, "extract_doc_batch",
                                             side_effect=RuntimeError("word exploded")))
            pe = stack.enter_context(patch("traceback.print_exc"))
            stack.enter_context(contextlib.redirect_stdout(out))
            self._run_files(["a.doc", "b.doc"])
        pe.assert_called_once()
        self.assertIn("[-] .doc batch extraction failed (2 file(s)): word exploded", out.getvalue())
        self.assertEqual(self._row("a.doc")["Name"], "Read Error")

    def test_resume_cache_checkpointed_before_matching(self):
        import contextlib
        import copy
        writes = []
        seen = {}

        def recorder(path, data):
            writes.append((path, copy.deepcopy(data)))
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            stack.enter_context(patch.object(extractor, "atomic_write_json", side_effect=recorder))
            good = dict(mocks["evaluate_candidate_match_async"].return_value)

            async def eval_side_effect(*a, **kw):
                seen.setdefault("cache_writes", [d for p, d in writes if p == extractor.RESUME_CACHE_FILE])
                return dict(good)
            mocks["evaluate_candidate_match_async"].side_effect = eval_side_effect
            self._run_files(["a.pdf", "b.pdf"])
        before = seen["cache_writes"]
        self.assertEqual(len(before), 1, "expected one checkpoint after extraction, before matching")
        for name in ("a.pdf", "b.pdf"):
            entry = before[0][dummy_md5(name)]
            self.assertEqual(entry["res_embedding"], [1.0, 0.0])
            self.assertEqual(entry["candidate_data"]["Name"], "Candidate One")
        self.assertEqual(before[0]["match_cache"], {})
        # the final write still carries the match results on the candidate data
        final = [d for p, d in writes if p == extractor.RESUME_CACHE_FILE][-1]
        self.assertEqual(len(final["match_cache"]), 2)
        self.assertEqual(final[dummy_md5("a.pdf")]["candidate_data"]["MatchPercentage"], 85)

    def test_pipeline_status_applied_once_per_tick(self):
        import contextlib
        import queue
        app = self.app
        app.ui_events = queue.Queue()
        app._post = extractor.ExtractorApp._post.__get__(app, extractor.ExtractorApp)
        app._closing = True  # no reschedule from the manual drain below
        app._pending_progress = app._pending_status = None
        app._status_dirty = False
        for name in ("_drain_ui_events", "_queue_status_update", "_apply_pending_status"):
            setattr(app, name, getattr(extractor.ExtractorApp, name).__get__(app, extractor.ExtractorApp))
        app.filter_table = MagicMock()
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            # stop right before extraction_finished so the pending status survives
            app.extraction_finished = MagicMock()
            self._run_files([f"r{i}.pdf" for i in range(6)])
            self.assertGreater(app.ui_events.qsize(), 20)
            app.set_matching_progress.assert_not_called()
            app.lbl_status.configure.assert_not_called()
            app._drain_ui_events()
        app.set_matching_progress.assert_called_once_with(1.0)
        app.lbl_status.configure.assert_called_once_with(text="Evaluating matches (6/6)...")
        app.extraction_finished.assert_called_once()

    def test_no_candidates_sets_progress_once(self):
        import contextlib
        values = []
        self.app.set_matching_progress = MagicMock(side_effect=values.append)
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            mocks["extract_text_from_file"].side_effect = lambda path: None  # every read fails
            self._run_files(["a.pdf", "b.pdf"])
        self.assertEqual(values.count(1.0), 1, values)
        self.assertEqual(values[-1], 1.0)

    def test_on_close_flushes_and_stops_drain(self):
        import queue
        app = MagicMock(spec=extractor.ExtractorApp)
        app.ui_events = queue.Queue()
        app._table_dirty = False
        app._progress_dirty = False
        app._closing = False
        app.after = MagicMock()
        for name in ("_post", "_drain_ui_events", "on_close"):
            setattr(app, name, getattr(extractor.ExtractorApp, name).__get__(app, extractor.ExtractorApp))
        app.on_close()
        app._save_drive_cache.assert_called_once_with(force=True)
        app.destroy.assert_called_once()
        self.assertTrue(app._closing)
        app._drain_ui_events()
        app.after.assert_not_called()

    def test_on_close_survives_cache_save_error(self):
        app = MagicMock(spec=extractor.ExtractorApp)
        app._save_drive_cache.side_effect = OSError("disk full")
        app.on_close = extractor.ExtractorApp.on_close.__get__(app, extractor.ExtractorApp)
        app.on_close()
        app.destroy.assert_called_once()

    def test_match_timeout_60(self):
        import asyncio
        from unittest.mock import AsyncMock
        parsed = MagicMock(match_percentage=70, technical_match=70, experience_match=70,
                           domain_match=70, strengths=[], weaknesses=[],
                           missing_requirements=[], summary="ok", skill_evidence=[])
        completion = MagicMock()
        completion.choices = [MagicMock()]
        completion.choices[0].message.parsed = parsed
        client = MagicMock()
        client.beta.chat.completions.parse = AsyncMock(return_value=completion)
        jd = extractor.ParsedJD(
            required_skills=[], preferred_skills=[], nice_to_have_technologies=[],
            experience_requirements="", minimum_years=0, education_requirements="",
            mandatory_technologies=[], domain_requirements="", seniority_expectations="",
            deal_breakers=[])
        result = asyncio.run(extractor.evaluate_candidate_match_async("profile", jd, {}, client, "gpt-5"))
        self.assertEqual(result["MatchPercentage"], 70)
        self.assertEqual(client.beta.chat.completions.parse.call_args.kwargs["timeout"], 60)

    def test_skipped_files_truncated(self):
        import re
        many = [(f"skipped{i:03d}.xyz", "Unsupported file format") for i in range(300)]
        out = extractor.format_skipped_files(many)
        self.assertEqual(len(re.findall(r"skipped\d{3}\.xyz", out)), 20)
        self.assertIn("skipped000.xyz\nUnsupported file format", out)
        self.assertIn("\u2026and 280 more (see processing_summary.csv)", out)
        self.assertTrue(out.startswith("Skipped Files\n"))
        few = many[:5]
        out = extractor.format_skipped_files(few)
        self.assertEqual(len(re.findall(r"skipped\d{3}\.xyz", out)), 5)
        self.assertNotIn("more (see", out)
        self.assertEqual(extractor.format_skipped_files(many[:20]).count("more (see"), 0)

    def test_extraction_finished_report_truncates_skipped(self):
        app = self.app
        app.skipped_files = [(f"skipped{i:03d}.xyz", "Unsupported file format") for i in range(300)]
        app.filter_table = MagicMock()
        with patch("extractor.messagebox") as mb:
            app.extraction_finished(0, "dummy", [], 0)
        mb.showinfo.assert_not_called()
        msg = app._processing_report
        self.assertIn("\u2026and 280 more (see processing_summary.csv)", msg)
        self.assertNotIn("skipped020.xyz", msg)

    def test_init_registers_close_protocol(self):
        import inspect
        src = inspect.getsource(extractor.ExtractorApp.__init__)
        self.assertIn('self.protocol("WM_DELETE_WINDOW", self.on_close)', src)
        self.assertLess(src.index("self._closing = False"), src.index("self.after(100, self._drain_ui_events)"))


class TestUiHelpers(unittest.TestCase):
    def test_run_progress_combines_processing_and_uploads(self):
        self.assertEqual(extractor.run_progress(0.8, 0, 0, True, False), 0.0)
        self.assertEqual(extractor.run_progress(0.5, 0, 0, True, True), 0.25)
        self.assertEqual(extractor.run_progress(1.0, 1, 4, False, True), 0.625)
        self.assertEqual(extractor.run_progress(1.0, 0, 0, False, True), 1.0)

    def test_upload_status_display(self):
        self.assertEqual(extractor.upload_status_display("Uploaded"), ("✓ Uploaded", ()))
        self.assertEqual(extractor.upload_status_display("Pending..."), ("• Pending", ()))
        self.assertEqual(extractor.upload_status_display("Failed"), ("✕ Failed", ("problem",)))
        self.assertEqual(extractor.upload_status_display("Missing Email"), ("Missing Email", ()))

    def test_as_percent(self):
        self.assertEqual(extractor.as_percent(85), 85.0)
        self.assertEqual(extractor.as_percent("72%"), 72.0)
        self.assertIsNone(extractor.as_percent("N/A"))
        self.assertIsNone(extractor.as_percent(None))


if __name__ == "__main__":
    unittest.main()
