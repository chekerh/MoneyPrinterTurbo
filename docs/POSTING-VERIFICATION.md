# Posting workflow verification — 2026-10-06

| Check | Result |
| --- | --- |
| Python compilation | Passed for `app`, `webui`, `cli.py` and vendored tools |
| Ruff | Passed for changed production modules, tools and relevant tests |
| Daily scheduler shell syntax | Passed with `bash -n` |
| Focused automated suite | 277 passed, 4 skipped; 8,202 subtests passed |
| Posting-package and publishing coverage | 86% combined line/branch coverage; package 91%, publishing 84% |
| Diff whitespace checks | Passed |
| Static type checker | Not run: pyright/mypy are not installed in this environment |
| Live OAuth/uploads and video quality | Left for manual testing |

The suite covers durable copy without a connected account, scheduled-upload
suppression, reuse of saved copy by the upload worker and both tool copies,
archive preservation of posting text and receipts, concurrency protection,
retry after a disconnected account or explicit failure, partial success,
unknown upload outcomes, video/channel identity checks, library copy controls,
native YouTube settings, privacy selection and the thumbnail API endpoint.

Publishing providers and copy generation are mocked in the new tests. Files
are created in temporary directories. No live videos were posted and no OAuth
credentials or channel tokens were changed. The full project suite was not
run; this report covers the generation, archive, publishing, copy and UI areas
affected by this milestone.

See [POSTING-WORKFLOW.md](POSTING-WORKFLOW.md) for the manual check and setup steps.
