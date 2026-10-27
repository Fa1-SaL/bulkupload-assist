import tempfile
import unittest
from pathlib import Path
from workflow_policy import (exportable, csv_fields, csv_values, attention_reasons,
                             merge_duplicates, ReviewStore)


def candidate(**changes):
    row = dict(Name='Ada', Email='ada@example.test', Phone='1234567890', Country='India',
               YearsOfExperience='5', ResumeLink='Uploaded', ResumeDriveURL='https://drive.test/a',
               SourceFilename='ada.pdf', Parsed=True, MatchPercentage=83)
    row.update(changes)
    return row


class ExportPolicyTests(unittest.TestCase):
    def test_defaults_require_complete_uploaded_candidate(self):
        self.assertTrue(exportable(candidate()))
        self.assertFalse(exportable(candidate(Email='Not Found')))
        self.assertFalse(exportable(candidate(ResumeLink='Failed')))

    def test_missing_opt_in_preserves_incomplete_parsed_record_only(self):
        self.assertTrue(exportable(candidate(Email='Not Found', ResumeLink='Not uploaded'), True))
        self.assertFalse(exportable(candidate(Name='Read Error', Parsed=False, Status='Read Error'), True))
        self.assertFalse(exportable(candidate(Parsed=False, Status='Extraction Failed'), True))
        self.assertFalse(exportable(candidate(ResumeLink='Pending'), True))

    def test_parsing_placeholder_is_never_exported_after_pipeline_failure(self):
        row={'SourceFilename':'resume.pdf','Name':'resume.pdf','Status':'Parsing…','ResumeLink':''}
        self.assertFalse(exportable(row,True))

    def test_regular_columns_follow_batch_matching_not_live_checkbox(self):
        self.assertEqual(csv_fields(True, False), ['name','email','phone','country','resume_link',
                                                  'years_of_experience','linkedin_url'])
        self.assertEqual(csv_fields(True, True)[-1], 'match_percentage')
        self.assertEqual(len(csv_fields(False, False)), 13)

    def test_missing_values_are_blanks_not_placeholders_or_fake_zero(self):
        vals = csv_values(candidate(Email='Not Found', ResumeLink='Failed', MatchPercentage='Not evaluated'),
                          True, True)
        self.assertEqual(vals[1], '')
        self.assertEqual(vals[4], '')
        self.assertEqual(vals[-1], '')

    def test_attention_has_specific_reasons_without_mistaking_zero_experience(self):
        self.assertEqual(attention_reasons(candidate(YearsOfExperience=0)), [])
        self.assertIn('Missing email', attention_reasons(candidate(Email='Not Found')))
        self.assertIn('Upload failed: denied', attention_reasons(candidate(ResumeLink='Failed', UploadError='denied')))
        self.assertIn('Match not evaluated', attention_reasons(candidate(MatchPercentage='Not evaluated')))

    def test_duplicates_expose_sources_and_prefer_uploaded_without_mutating(self):
        a = candidate(SourceFilename='first.pdf', ResumeLink='Failed', MatchPercentage=99)
        b = candidate(SourceFilename='second.pdf', MatchPercentage=60)
        rows = merge_duplicates([a,b])
        self.assertEqual(rows[0]['SourceFilename'], 'second.pdf')
        self.assertEqual(rows[0]['MergedSources'], ['first.pdf','second.pdf'])
        self.assertNotIn('MergedSources', a)

    def test_corrections_are_hash_scoped_and_history_is_job_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp)/'review.json')
            store.correct('hash-a', {'Name':'Edited Ada'})
            store.record_export('J1', ['hash-a'], '2026-10-28')
            loaded = ReviewStore(Path(tmp)/'review.json')
            self.assertEqual(loaded.apply(candidate(FileHash='hash-a'))['Name'], 'Edited Ada')
            self.assertEqual(loaded.apply(candidate(FileHash='hash-b'))['Name'], 'Ada')
            self.assertEqual(loaded.previous('J1', ['hash-a']), 1)
            self.assertEqual(loaded.previous('J2', ['hash-a']), 0)
            loaded.clear_history()
            self.assertEqual(loaded.previous('J1', ['hash-a']), 0)
            self.assertEqual(loaded.apply(candidate(FileHash='hash-a'))['Name'], 'Edited Ada')

if __name__ == '__main__': unittest.main()
