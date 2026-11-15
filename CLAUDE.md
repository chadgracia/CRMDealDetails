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
- No company news section (the NewsAPI feature and its key were removed). The catalyst
  "Recent Development" block (`custom_label_3999603`) is unrelated and stays.

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

## Unconfirmed rule (unconfirmed_status)
- Returns `(is_unconfirmed, effective_date_or_None)`; `render_unconfirmed_notice` shows
  "last confirmed <effective date>" or "not recently confirmed" when there is none.
- Never Unconfirmed: Hold stage (2094373) and closed/archived stages; Refresh field
  `custom_label_3994687` = 0. Threshold = Refresh days, else 30 for Seller Role 7020357, else 60.
- Effective date = the newer of Last Confirmed (`custom_label_4012491`, "YYYY/MM/DD" or
  "YYYY-MM-DD") and the UTC date of the deal's `updated_at` — any Pipeline edit counts as
  confirmation. Unconfirmed = no effective date, or older than the threshold.
- Restore exception: stale-deal-scanner logs restores to
  `s3://full-pipeline-cache/obsolete-review/log.json` (`{"entries": [{deal_id, action, at}]}`).
  If the deal's LATEST entry is `"restored"`, `updated_at` counts only when it is > 15 min after
  that entry's `at`; otherwise Last Confirmed alone. So the restore's own write doesn't make an
  old deal look confirmed, but later edits do.
- The log is parsed to `{deal_id: latest entry}` and cached module-level for 10 min
  (`_restore_log_cache`). Missing/unreadable/malformed log = no restores. Fail-soft: any other
  error returns `(False, None)`.

## Deal gate (shared block, identical in chadgracia/trades and chadgracia/CRMDealDetails)
- Code between `# --- Deal gate (shared verbatim ...` and `# --- end deal gate shared block ---`
  is the same in both repos; change both together. Tests: `tests/test_deal_gate.py`.
- Switch: `s3://full-pipeline-cache/deal-gate.json` `{"enabled": bool, "updated_at": iso}`, cached 60 s;
  missing/unreadable keeps the container's last value, else OFF. Switch OFF = non-admin output
  unchanged (golden files in `tests/fixtures/` were rendered by the pre-gate code; keep them passing).
  Admin page + On/Off: trades `/?view=deal-gate`. Admin preview anywhere: `&gate_preview=anon|pending|blocked|cleared|confirm|confirmed`.
- Viewer state from people-slim.json (`_people_slim_status_index`, never people.json), loaded only
  when the request carries gg_id / gg_view / gp. Investor Level `custom_label_3923758` (ids hardcoded):
  cleared ONLY Substantive 7162165, Accredited Investor 6950563, Qualified Client 7209227, Qualified Purchaser
  6950564; blocked Non-Accredited 6950562; pending everything else (Hold 7161646, Self-Confirmed 7223459,
  Unknown 6950561, blank, other ids, unmatched gg_id email). Index unreadable: identified viewer = cleared.
- A valid personal link (gg_view / gp) clears whatever the level, unless that record is hard-blocked or
  Non-Accredited. `s3://full-pipeline-cache/deal-gate/approved.json` `{pid: iso}` (written by daily-brief Approve):
  a pid approved in the last 3 h is cleared unless hard-blocked (cached 60 s, missing = {}).
- Details form ("One more step to see offer terms") replaces the confirm button: shown to a signed-in pending
  viewer with no CRM record or a blank / Unknown / Hold record (`dg_self_confirm_token` -> ("details", adf1 token
  bound to `dg_email_tag(email)`)). First / Last name (required), Country (`DG_COUNTRIES` select), Employer
  (optional), radio "I am an accredited investor" (yes) / "I'm not sure" (unsure). POST trades `/?view=access-details`
  (`_dg_access_details`): same-site, honeypot, 10/IP/hour, 5/address/day, signed form token; email from the
  session only; anonymous/cleared/blocked = 403. Fields capped at 80 chars, control chars stripped
  (`dg_clean_field`), escaped on render. Saves first_name, last_name, country, employer, accredited, details_at,
  from (path) on the queue item (`dg_queue_details`; declined addresses not re-added). Pipeline (existing person
  only): first/last name only where blank or a placeholder (`dg_name_is_placeholder`); an accredited "yes" on a
  blank / Unknown / Hold record = Investor Level Self-Confirmed 7223459 + note, exactly like the confirm button.
  Never creates people or companies. One email to Chad per address per 24 h (`deal-gate/requests/accessdetails/sent/`),
  Review link only when trades has BRIEF_PAGE_URL + BRIEF_PAGE_KEY. NEVER clears the gate. Afterwards the viewer
  sees "Thanks, <first>. We'll review your details..." (yes) or "...Please complete the Investor Qualification
  Form..." (unsure), from the queue item (cached 60 s) or the `gg_ad` cookie (answer only, 7 d).
  The old `POST /?view=self-confirm` route still works; Self-Confirmed viewers without details see `DG_SC_THANKS_TEXT`.
  Previews: `&gate_preview=confirm` (form), `confirmed`, `details-yes`, `details-unsure`.
- Access queue `s3://full-pipeline-cache/deal-gate/queue.json` `{"items": {email: {email, person_id, reason,
  first_at, last_at}}, "declined": {email: iso}}`: written via the shared `dg_queue_add` / `dg_queue_signup` by
  trades (self-confirm; first gated grid load) AND CRMDealDetails (first gated deal-page load): switch ON, a
  signed-in viewer with no record or a Hold record = "signup", once per email; never anonymous; declined emails
  only return by self-confirming. Fail-soft, one log line, no address logged.
  daily-brief reads it and Approves (Substantive + approved.json) / Declines (Hold + declined).
- Hard block (beats every Investor Level, cleared matches, personal links and self-confirm; viewer just sees
  the neutral "access is being reviewed" box): a people-slim record is hard-blocked (rec[5]) if any email is
  @rainmakersecurities.com / @rainmakerapac.com (or subdomain) or in `DG_HARD_EMAILS` (= daily-brief
  MAILER_BLOCK_EMAILS), or Transactor Type `custom_label_3759163` has 6577160 / 6888332 / 6888333.
  There is NO Source = RMS rule (removed; Chad handles RMS-sourced contacts manually).
  The gg_id email itself is checked against the email rules even with no record or an unreadable index.
  `dg_record_state` -> "hard"; `dg_combine`: any "hard" -> "blocked".
- Personal links: env `VIEW_LINK_SECRET` (empty = links off). `?v=<pid>.<exp>.<sig>` (sig over
  `view1|pid|exp`, 183 d) -> 302 + cookie `gg_view` (sig over `viewc1|pid|exp`, 183 d; Max-Age 183 d).
  `dg_verify_token` also refuses any token whose remaining life exceeds its prefix's policy TTL (+300 s), so
  pre-change 12-month gg_view cookies no longer work. gg_view is VIEW-ONLY: never signed in, never gg_id,
  never an SSO token.
- Redirects: every deal-gate redirect is absolute on DG_SITE and drops DG_STRIP_PARAMS. Deal pages sit behind
  API Gateway "Trades" (HTTP proxy -> Function URL): the Lambda sees rawPath "/" and ?deal_id=, so
  CRMDealDetails passes `public_path="/deal/{id}"`, `drop=("deal_id",)` to dg_link_redirect / dg_current_path.
  Never rebuild a public URL from the event path in CRMDealDetails.
- Anonymous gate box: "Offer terms are shown to investors we work with"; "Already a client? Enter your email for
  a one-click link." + Send link; "New here? Create a login and we'll review your access." + Request access button
  (`.gate-btn`, Cognito sign-up URL); after Send link: "If that address is on file, a link is on its way. Not a
  client yet?" + the same button.
- Gated output uses fixed placeholders in `<span class="gated">`; real values must not appear anywhere
  in the response. Request logs go through `dg_redacted_event` (no cookies, body, or link/key params; header values
  such as Referer pass through `dg_redact_url`, which blanks DG_STRIP_PARAMS values).

## Related repo
- `chadgracia/deal-update-form` — the Lambda behind the Questions form URL (submit handling +
  emails). It is usually NOT in this session's scope; changes there must be made in a session
  that includes that repo.

## SECURITY RULES — MANDATORY (read before any change)

This repo is PUBLIC, and so are its GitHub Actions logs. Secrets have leaked from Chad's repos 5-6 times. Never again.

1. No secret value ever goes in code, tests, docs, comments, commit messages, PR text or logs. That includes API keys, admin keys, HMAC/signing secrets, tokens, passwords, and any URL containing `key=`, `token=` or `sso=`. Secrets come from `os.environ[...]` (Lambda environment variables) or `s3://pipeline-token/` only. Tests use obvious fakes like `"test-key"`.
2. Every `aws lambda ...` command in a workflow must end with `> /dev/null`. Lambda API responses include every environment variable, and the logs are public. Never print the environment (`printenv`, `env`, `set -x`, or `print(os.environ)`).
3. Never log or print a secret, even partially, or a request URL that carries one.
4. `.github/scripts/secret_guard.py` runs automatically before every `git commit` and `git push` (via `.claude/settings.json`), and again in CI. If it blocks you, remove the secret and move the value to an environment variable. NEVER bypass, weaken, delete or skip the guard, and never use `--no-verify` or `secret-guard: allow` to get past it without Chad's explicit approval.
5. A new secret needs Chad to add a Lambda environment variable first. Tell him the exact variable name. Code that reads it ships after that.
6. Before finishing, run `python3 .github/scripts/secret_guard.py --tree` and report the result.
7. NEVER build anything that asks for, collects, uploads, stores, emails or processes client identity data — tax IDs, SSNs, passports, driver's licenses, other government IDs, or dates of birth. Chad does not want or need it; identity verification is handled by Rainmaker's own forms. If a task seems to need it, stop and ask Chad.
