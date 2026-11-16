# Google Cloud + YouTube setup (do this once, ~40 minutes)

This is the only part of the project that cannot be automated: Google requires a
human in the Cloud Console. Work through it top to bottom. At the end you will
have one file (`client_secret.json`) that the server reads, and nothing secret
will ever be typed into the codebase.

Everything below uses **one** Google Cloud project for both Google Drive and
YouTube. That is deliberate — it means the user authorises once and gets both
capabilities, instead of running two separate consent flows.

---

## 0. Before you start

Use a **throwaway Google account**, not your personal one. The brief says:

> Keep a test YouTube channel and test storage account; we will not use your personal ones.

Create one at <https://accounts.google.com/signup> if you don't have a spare.
Write the address down; every step below must be done while signed into it.

---

## 1. Create the Cloud project

1. Go to <https://console.cloud.google.com/>.
2. Top-left project dropdown → **New project**.
3. Name: `cloudtube-mcp`. Organisation: *No organisation*. → **Create**.
4. Wait for the notification, then make sure the project picker shows
   `cloudtube-mcp` before continuing. Every later step applies to the
   *selected* project, and selecting the wrong one is the single most common
   way to lose an hour here.

---

## 2. Enable the two APIs

1. Navigation menu → **APIs & Services** → **Library**.
2. Search `Google Drive API` → open it → **Enable**.
3. Back to Library, search `YouTube Data API v3` → open it → **Enable**.

Confirm under **APIs & Services → Enabled APIs & services** that both are listed.

---

## 3. Configure the consent screen

In the current console this lives under **APIs & Services → OAuth consent
screen**, which opens the *Google Auth Platform* pages.

1. **Get started / Branding**
   - App name: `CloudTube MCP`
   - User support email: your test account
   - Developer contact email: your test account
   - Save.
2. **Audience**
   - User type: **External**
   - Publishing status: leave it on **Testing**. Do not click
     "Publish app" — publishing a restricted-scope app starts a verification
     review you do not want and do not need.
   - Under **Test users** → **Add users** → add your test account's email
     address. Only accounts listed here can complete the OAuth flow.
3. **Data Access** → **Add or remove scopes** → paste these four into the
   "manually add scopes" box, one per line, then **Add to table** → **Update**:

   ```
   https://www.googleapis.com/auth/drive.readonly
   https://www.googleapis.com/auth/youtube.upload
   https://www.googleapis.com/auth/youtube.readonly
   https://www.googleapis.com/auth/userinfo.email
   ```

   These are the minimum that do the job. `drive.readonly` rather than full
   `drive` because the server never writes to Drive; `youtube.upload` to
   publish; `youtube.readonly` to read back processing status after an upload;
   `userinfo.email` only so the server can tell you *which* account is
   currently connected.

---

## 4. Create the OAuth client

1. **Clients** → **Create client**.
2. Application type: **Desktop app**. (Not "Web application" — the server uses
   a loopback redirect on `127.0.0.1`, which is what the Desktop type is for,
   and it avoids having to pre-register a redirect URI.)
3. Name: `cloudtube-mcp-local` → **Create**.
4. In the dialog, click **Download JSON**.
5. Save the downloaded file into the project at exactly:

   ```
   E:\Internship Project\.secrets\client_secret.json
   ```

   Create the `.secrets` folder if it isn't there. It is already covered by
   `.gitignore`, and a pre-commit hook in this repo will refuse any commit that
   contains it.

**Do not paste the contents of that file into chat, a commit message, a README,
or a screen recording.** When you record the demo, keep the console window and
the file explorer away from that folder.

---

## 5. Create the test YouTube channel

1. Signed in as the test account, go to <https://www.youtube.com/>.
2. Avatar (top right) → **Create a channel** → accept the default name →
   **Create channel**.
3. Go to <https://www.youtube.com/verify> and verify with a phone number.
   Without verification the account cannot upload videos longer than 15
   minutes and some upload features stay locked. The demo clip is short, but
   verify anyway so this is not what breaks on recording day.

---

## 6. Put a test video and some files in Drive

Signed in as the test account, at <https://drive.google.com/>:

1. Create a folder `MCP Test`.
2. Inside it create a subfolder `Demo Videos` and upload 2–3 short `.mp4`
   files. Name one of them something like `Q4 Product Demo.mp4` so the
   "find the latest product demo video" prompt has something to find.
3. Also drop in a few non-video files — a Google Doc, a `.txt`, a `.csv`, a
   PDF — so the `read_file` tool has material to work with, and so the
   "answer questions about the files" part of the brief is demonstrable.
4. Upload one file that the server *cannot* read as text (a `.zip`, or a large
   binary). This is deliberate: it is one of the error cases the recording has
   to show.

---

## 7. Two limits you need to know about now, not on demo day

**Uploads will be locked to private.** Google restricts any API project that
has not passed the YouTube API compliance audit: videos uploaded through it are
forced to `private` and cannot be made public or unlisted, whatever
`privacyStatus` the request asks for. The audit takes weeks, so it is out of
scope for a five-day assignment. The server therefore sends the requested
privacy correctly, reads back what YouTube actually applied, and tells you
plainly when the two differ rather than pretending it worked. This is
documented in the README as a known limitation.

**Quota is 10,000 units per day.** A video insert costs 1,600 units, so the
project gets roughly **six real uploads per day**, counting failures. Quota
resets at midnight Pacific. Because of this, every failure scenario in the test
suite runs against an injectable fake YouTube transport; real uploads are spent
only on the demo recording. Check usage at **APIs & Services → YouTube Data
API v3 → Quotas**.

**Refresh tokens expire after 7 days** while the app's publishing status is
"Testing". The server detects the resulting `invalid_grant` and returns a
re-authorise instruction instead of a stack trace — that is one of the handled
error cases.

---

## 8. Checklist

- [ ] Test Google account created, signed in everywhere above
- [ ] Project `cloudtube-mcp` created and selected
- [ ] Google Drive API enabled
- [ ] YouTube Data API v3 enabled
- [ ] Consent screen: External, Testing, test user added
- [ ] Four scopes added under Data Access
- [ ] Desktop OAuth client created
- [ ] `client_secret.json` saved to `.secrets/` and **not** committed
- [ ] Test YouTube channel created and phone-verified
- [ ] Drive folder with videos, documents, and one unreadable binary
