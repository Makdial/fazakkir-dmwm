# Deploying Fazakkir

This guide covers **where to host the site**, with a focus on the platforms that
behave like Replit: **connect a GitHub repository and the app deploys itself on
every push.**

---

## 1. Short answer

**Use [Render](https://render.com/) with a free [Neon](https://neon.com/)
Postgres database.** That combination is free forever, deploys from your GitHub
repo with no manual server work, and — critically — does not lose your data.

There is one non-obvious trap in that combination, explained in the next
section. Read it before you deploy, because getting it wrong deletes your
accounts, playlists and comments.

---

## 2. The trap: free hosts have an ephemeral filesystem

Render's free tier documents this plainly:

> Free web services have an ephemeral filesystem. This means that any changes to
> your web service's filesystem (local SQLite databases, etc.) are lost every
> time the service restarts.

Fazakkir stores its accounts, playlists, comments and video metadata in a local
SQLite file. On Render free that file is destroyed **every time the service
sleeps (15 min idle) or redeploys**. You would lose all your data within hours —
while the videos themselves survived, because those are in Cloudflare R2.

**The fix is one environment variable:** point `DATABASE_URL` at a hosted
Postgres. The app already supports this (it was added for exactly this reason),
and falls back to SQLite when the variable is absent, so nothing changes for
local development.

Neon's free tier is permanent: **1 GB storage, 100 compute-hours/month, no credit
card required.** Your database is a few megabytes.

---

## 3. Deploying to Render (step by step)

### Step 1 — Create your free Postgres

1. Sign up at <https://neon.com> (free, no card).
2. Click **Create project**, name it `fazakkir`, pick a region close to you.
3. Choose the **Free** plan when asked.
4. Neon shows a **connection string** that looks like
   `postgresql://user:password@ep-xxx.region.neon.tech/fazakkir?sslmode=require`.
   Keep it — you need it in step 5. (Connection strings can also be found later
   under **Connection string** in the dashboard.)

### Step 2 — Push the code

Already done: the repository is at
<https://github.com/Makdial/fazakkir-dmwm> and the current work is pushed.

### Step 3 — Create the Render service

1. Sign up at <https://render.com> using your **GitHub account**.
2. Click **New → Web Service**, then **connect a repository**.
3. Authorise Render to read `Makdial/fazakkir-dmwm`.
4. Fill in the form. The repository ships a `render.yaml` blueprint, so most
   fields fill in automatically:

   | Field | Value |
   | --- | --- |
   | Name | `fazakkir` |
   | Branch | `master` |
   | Runtime | Python |
   | Build command | see `render.yaml` |
   | Start command | `gunicorn fazakkir_dmwm.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120` |
   | Instance type | Free |

5. Click **Create Web Service**.

### Step 4 — Add the secret environment variables

Render marks these **Sync: false**, so you must paste them in yourself under
**Environment**:

| Variable | Where to get it |
| --- | --- |
| `DATABASE_URL` | The Neon connection string from step 1 |
| `SECRET_KEY` | Generate: `python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"` |
| `ALLOWED_HOSTS` | `fazakkir.onrender.com` (your Render hostname) |
| `CSRF_TRUSTED_ORIGINS` | `https://fazakkir.onrender.com` |
| `R2_ACCOUNT_ID` | your local `fazakkir_dmwm/.env` |
| `R2_ACCESS_KEY_ID` | your local `fazakkir_dmwm/.env` |
| `R2_SECRET_ACCESS_KEY` | your local `fazakkir_dmwm/.env` |

Optionally, if you have them:

| Variable | Notes |
| --- | --- |
| `OPENAI_API_KEY` | enables automatic subtitles |
| `EMAIL_HOST_PASSWORD` | Gmail **App password**, enables storage alerts |

The R2 values are not in the GitHub repository — they are git-ignored on purpose.
Copy them from your own `.env`.

### Step 5 — Deploy

Render deploys automatically. Watch the **Logs** tab. When it finishes you get
a URL like `https://fazakkir.onrender.com`.

### Step 6 — Create the database tables and your admin

The Render shell is under **Shell**, or run it locally against the hosted
database:

```bash
python manage.py migrate
python manage.py compiletranslations
python manage.py createsuperuser
```

### Step 7 — Verify

Open your URL and check:

- the home page loads and the CSS is styled (static files were collected);
- a video plays (R2 credentials are right);
- `https://fazakkir.onrender.com/manage/` loads after signing in.

---

## 4. What to expect from Render's free tier

| | Behaviour | Why it matters here |
| --- | --- | --- |
| Cold start | ~1 minute after 15 min idle | First visitor waits; afterwards it is normal |
| Database | **Not ephemeral** (Neon) | Your data is safe |
| Bandwidth | 5 GB/month | Fine while videos stream from R2; the proxied MP3 uses this |
| SSL | Automatic | Needed for the PWA and background playback |
| Custom domain | Paid only | You get `*.onrender.com` for free |
| Background jobs | **Killed on sleep** | See below |

### Background jobs and sleeping hosts

The MP3 extraction and the automatic subtitles run as background threads inside
the web process. When Render sleeps or redeploys, those threads are killed
mid-`ffmpeg`.

The app handles this: a job that has been "processing" for longer than
`JOB_STALE_MINUTES` (30 by default) is considered abandoned, marked retryable,
and picked up again on the next page view. You do not have to intervene, but a
very long conversion of a large file may need to be started twice.

---

## 5. The alternatives, honestly

| Option | GitHub deploy | Always on | Persistent data | Verdict |
| --- | :---: | :---: | :---: | --- |
| **Render + Neon** | Yes | No (sleeps) | Yes, with Neon | **Best match for what you asked** |
| **Northflank** | Yes | **Yes** (free Sandbox tier) | Yes | Better uptime, slightly clunkier UI |
| **PythonAnywhere** | No | Yes | Yes | Easiest of all, but manual, no GitHub link |
| Railway | Yes | Yes | Yes | **No free tier** (trial credits only) |
| Koyeb | Yes | — | Volumes now **paid** ($29/mo) | Pivoted to AI hosting; no longer free |
| Replit | Yes | Yes | Yes | Free tier publishes **one** app and is credit-metered; not intended as a production home |
| Fly.io / Heroku | Yes | Yes | Yes | No free tier |

### If you want zero setup: PythonAnywhere

It does **not** connect to GitHub — you clone and configure once by hand — but it
needs no database service, no build command and no secrets dashboard, and it does
not sleep. The full walkthrough is in [the original section below](#6-pythonanywhere-walkthrough-free).

### If you want GitHub + always-on: Northflank

Free Sandbox tier: 2 services, 1 database, 2 cron jobs, **always-on compute (no
sleeping)**, GitHub/GitLab/Bitbucket integration, and buildpacks so no Dockerfile
is needed. The trade-off is a busier UI than Render's.

---

## 6. PythonAnywhere walkthrough (free)

1. Sign up at <https://www.pythonanywhere.com/user/signup/> → **Beginner** (free)
   plan, then verify your email.
2. Open a **Bash console**:

   ```bash
   git clone https://github.com/Makdial/fazakkir-dmwm.git
   cd fazakkir-dmwm/fazakkir_dmwm
   python3.13 -m venv ~/fazakkir-venv
   source ~/fazakkir-venv/bin/activate
   pip install -e .
   ```

3. Create `.env`:

   ```ini
   DEBUG=0
   SECRET_KEY=<generated>
   ALLOWED_HOSTS=<yourusername>.pythonanywhere.com
   R2_ACCOUNT_ID=...
   R2_ACCESS_KEY_ID=...
   R2_SECRET_ACCESS_KEY=...
   R2_BUCKET=fazakkir
   ```

4. Prepare the database and static files:

   ```bash
   python manage.py migrate
   python manage.py collectstatic --noinput
   python manage.py compiletranslations
   python manage.py createsuperuser
   ```

5. On the **Web** tab: add a **Manual configuration** Python 3.13 app, set the
   virtualenv to `/home/<yourusername>/fazakkir-venv`, and map static files:

   | URL | Directory |
   | --- | --- |
   | `/static/` | `/home/<yourusername>/fazakkir-dmwm/fazakkir_dmwm/staticfiles` |

6. Replace the **WSGI configuration file** with:

   ```python
   import os
   import sys

   sys.path.insert(0, "/home/<yourusername>/fazakkir-venv/bin")
   sys.path.insert(1, "/home/<yourusername>/fazakkir-venv/lib/python3.13/site-packages")
   os.environ["DJANGO_SETTINGS_MODULE"] = "fazakkir_dmwm.settings"
   sys.path.insert(0, "/home/<yourusername>/fazakkir-dmwm/fazakkir_dmwm")

   from fazakkir_dmwm.wsgi import application
   ```

7. Press **Reload** and open `https://<yourusername>.pythonanywhere.com/`.

> PythonAnywhere free can only reach an allowlist of external sites over
> HTTP(S). Cloudflare R2 (`r2.cloudflarestorage.com`) and GitHub are both on
> that list, so storage and deploys work.

---

## 7. Updating the site later

With Render and GitHub connected this is just:

```bash
git push
```

Render rebuilds and redeploys automatically. To also run migrations, add a
**pre-deploy command** in the Render dashboard:

```
python fazakkir_dmwm/manage.py migrate --noinput
```

Manually (PythonAnywhere or SSH):

```bash
git pull
source ~/fazakkir-venv/bin/activate
pip install -e .
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py compiletranslations
```

---

## 8. Operations

### Backups

Videos and MP3s are in R2 and are safe. On Render the database is in Neon, so use
**Neon's automatic backups** (they are included on the free plan) or dump it:

```bash
python manage.py dumpdata --natural-foreign --natural-primary > backup.json
```

On PythonAnywhere (SQLite), copy `db.sqlite3`.

### HTTPS

Render and PythonAnywhere both provide HTTPS automatically. The PWA install
prompt, offline listening and the background playback controls only work in a
secure context, so this is required, not optional.

### Before you go live

- Set a private `SECRET_KEY` (never reuse the development one).
- Set your own administrator credentials.
- Add `EMAIL_HOST_PASSWORD` (a Gmail **App password**) so storage alerts are
  actually sent; until then they print to the log.

---

## Quick reference

| Task | How |
| --- | --- |
| Deploy a change | `git push` |
| Apply migrations | Render pre-deploy command, or `manage.py migrate` |
| Back up the database | `manage.py dumpdata > backup.json` (Neon: built-in backups) |
| Create an admin | `python manage.py createsuperuser` |
| Convert missing MP3s | `python manage.py generateaudio` |
| Generate subtitles | `python manage.py generatesubtitles` |
| Check storage | `python manage.py checkstorage` |