# Runbook: the SF.gov daily copy

The public instance answers its SF.gov tools from a daily copy. This runbook covers
making that copy. The README section "The daily copy on the public instance" says what the
copy holds and how answers from it read.

## What runs

```bash
uv run opencivicquery sfgov-copy --bucket <snapshot-bucket>
```

The command runs once a day on a maintainer's computer. It reads, for each agency in
`DEFAULT_AGENCIES` in `src/opencivicquery/sources/sfgov_copy.py`, the agency's news pages,
the text of each, and the agency's meeting pages with their agenda text and attached files.
It writes one compressed file, `sfgov-copy/current.json.gz`, to the bucket the server keeps
its snapshots in. The bucket's lifecycle rule deletes only `temporary/`, so the copy stays
until the next run replaces it. The server reads it again every ten minutes.

It sends one request a second under the server's own identity. The first run reads every
news page's text. Later runs read text only for news pages that are new or whose title,
date, type, or summary changed, and read every meeting page again in lists of 50.

## Before the first run

1. Give the computer a credential that can write `sfgov-copy/current.json.gz` in the bucket.
   The safest is a service account of its own whose only role is on objects under
   `sfgov-copy/` (an IAM condition on the bucket, `resource.name.startsWith(...)`). The
   command uses Google's Application Default Credentials, or, with `--key-stdin`, a service
   account key it reads as JSON from standard input, so the key never has to sit in a file.
   On macOS the key can live in the login Keychain, base64-encoded:

   ```bash
   # Prints nothing; the key passes from gcloud to the Keychain through a pipe.
   gcloud iam service-accounts keys create /dev/stdout --iam-account=<service-account> \
     | base64 | tr -d '\n' \
     | { read -r key; printf 'add-generic-password -U -s <item> -a <account> -w %s\n' "$key" \
         | security -i; }
   ```

   and each run reads it back:

   ```bash
   security find-generic-password -s <item> -w | base64 --decode \
     | uv run opencivicquery sfgov-copy --bucket <snapshot-bucket> --key-stdin
   ```
2. Count what the first run will fetch. This sends four light requests per agency and writes
   nothing:

   ```bash
   uv run opencivicquery sfgov-copy --path /tmp/sfgov-plan --plan
   ```

3. Try a small sample into a local folder, which never replaces the server's copy:

   ```bash
   uv run opencivicquery sfgov-copy --path /tmp/sfgov-sample --sample 3
   ```

4. Run it for real with `--bucket`. A run prints one line per agency and the number of
   requests it sent.

## Every day

`operations/sfgov-copy.launchd.plist` is a macOS launchd job that runs the command once a
day with the key from the Keychain, as above. Point it at a clone kept only for this job,
so it never runs unfinished work, and run `uv sync --frozen --no-dev` in that clone once.
Replace the five `__PLACEHOLDER__` values, with the Keychain item's name in
`__KEYCHAIN_ITEM__`, save it as
`~/Library/LaunchAgents/org.opencivicquery.sfgov-copy.plist`, and load it with:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.opencivicquery.sfgov-copy.plist
```

launchd runs a missed job when the computer wakes. The Keychain must be unlocked, which it
is while the user is logged in. To run it once now and to stop it:

```bash
launchctl kickstart gui/$(id -u)/org.opencivicquery.sfgov-copy
launchctl bootout gui/$(id -u)/org.opencivicquery.sfgov-copy
```

To revoke the credential, delete the service account's key (or the account) in Google
Cloud and the Keychain item with `security delete-generic-password -s <item>`.

## When something goes wrong

- The command exits with status 1 when an agency could not be read. That agency keeps its
  pages from the previous copy, with their older date, and answers say that date.
- An HTTP 429 answer stops the run at once. The agencies not yet read keep their previous
  pages. The next day's run tries again.
- When no agency could be read and there is no earlier copy, nothing is written.
- A copy the server cannot read is logged on the server, and the server keeps the last copy
  it read. A copy the command cannot read is replaced by a full read on its next run.
- One news page whose text cannot be read is skipped and tried again the next day. Five in a
  row stop that agency's read, which then keeps its previous pages.
- The copy carries a format number, and the server reads only the format its own code
  writes; it keeps the last copy it read until it gets one it can read. After a change to
  the format, update the job's clone with `git pull` once the server that reads the new
  format is deployed. A run that finds an earlier format reads everything again.

Answers from the copy say the date it was taken, so a copy that stopped updating shows in
every answer.
