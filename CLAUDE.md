# CRMDealDetails — project memory

AWS Lambda (Python) that renders an HTML "Deal Details" page for a PipelineCRM deal.
Single file: `lambda_function.py`. Deployed to the `CRMDealDetails` Lambda (us-east-1)
by a GitHub Action on push to `main`.

## Runtime
- Python 3.12+. The `mailto` links contain backslashes inside f-string expressions,
  which only parse on 3.12+ — syntax-check with `python3.12 -m py_compile lambda_function.py`,
  NOT 3.11 (3.11 reports a false "f-string expression cannot include a backslash").
- Dependencies: standard library + `boto3` only (both in the Lambda runtime). Do NOT add
  third-party imports — the deploy ships a single-file zip with no vendored packages.

## Build & deploy
- No build step. Verify with `python3.12 -m py_compile lambda_function.py`.
- Deploy: commit to `main`. `.github/workflows/deploy.yml` assumes the
  `github-actions-deploy` OIDC role, zips `lambda_function.py`, and runs
  `aws lambda update-function-code --function-name CRMDealDetails` (us-east-1). No manual deploy.
- Git workflow: develop on `claude/fervent-edison-XaHni`, commit, merge to `main`, push
  (auto-deploys). Always commit + merge to main.

## What it does
- `lambda_handler(event, context)` reads `deal_id` from query/path params, fetches the deal +
  company from the PipelineCRM API v3, and returns a full HTML page (200, `text/html`).
- Auth: a JWT read from S3 (`pipeline-token` / `pipeline-jwt.json`) at import time. The module
  CANNOT be imported locally (it hits S3 on import) — test pure functions by extracting them
  with `ast` instead.
- `map_custom_fields` maps PipelineCRM `custom_label_*` ids to names; `map_option_value` maps
  option ids to labels.
- Company news via NewsAPI. `NEWS_SKIP_COMPANIES` (lowercased names) suppresses the News
  section for noisy companies.

## HTML / CSS gotcha
- The entire page is ONE big f-string (`html_content`). Any literal `{`/`}` inside it (all the
  CSS) MUST be doubled `{{ }}`. f-string expression parts (e.g. the spvSection display toggle)
  stay single.
- Shared look comes from `master.css`
  (`https://s3.us-east-1.amazonaws.com/main.css/master.css`); only page-specific layout lives
  in the inline `<style>`. Reuse its CSS variables (`--accent`, `--text`, `--border-strong`,
  `--text-secondary`, …).
- `render_qa_box` builds its HTML with normal f-strings (single braces) — it is a separate
  function, NOT inside `html_content`.

## Questions panel (render_qa_box)
- One shared render path; the catalog is chosen by deal side: `QUESTION_CATALOG_BUYER` on SELL
  orders ("Send Question to Seller"), `QUESTION_CATALOG_SELLER` on BUY orders ("Send Question
  to Buyer").
- The panel is a POST form to the `deal-update-form` Lambda URL with `qa=submit`. CRMDealDetails
  only RENDERS the form. The submit handler, the buyer/seller/internal emails (SES), and the
  `QA_TEXT` dict live in a SEPARATE repo: `chadgracia/deal-update-form`. Any new input
  (bid fields, fee fields, …) needs a matching `q_line` case there to appear in the relayed
  email — otherwise it posts but is "dead."

## Related repo
- `chadgracia/deal-update-form` — the Lambda behind the Questions form URL (submit handling +
  emails). It is usually NOT in this session's scope; changes there must be made in a session
  that includes that repo.
