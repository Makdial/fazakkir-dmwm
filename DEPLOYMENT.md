# Deploying Fazakkir

This is a hands-on guide. It covers where to host the app, and step-by-step
instructions for getting it live.

---

## 1. Which free host should you use?

**Short answer: [PythonAnywhere](https://www.pythonanywhere.com/)'s free
(Beginner) plan is the best fit for this specific project, and
[Render](https://render.com/)'s free tier is a usable fallback.**

Nothing is "forever", so it is worth being precise about what each option really
gives you and where it will hurt this particular app.

### Why the fit matters for *this* project

Fazakkir has four unusual requirements that rule out most free hosts:

1. **It talks to Cloudflare R2 over the network** on nearly every request
   (presigned URLs, MP3 streaming, thumbnails).
2. **It writes to a SQLite database file** that must survive restarts.
3. **It runs background jobs** — `ffmpeg` MP3 extraction and the automatic
   subtitle/translation pipeline — triggered by ordinary page views.
4. **It streams MP3s through Django** (`/<id>/audio-stream/`), so *your* host's
   bandwidth, not just R2's, is being used.

### The candidates compared

| | **PythonAnywhere (Beginner, $0)** | **Render (Free)** | **Fly.io / Railway / Heroku** |
|---|---|---|---|
| Always-on | Yes | **No** — sleeps after 15 min idle, ~1 min cold start | Yes |
| Persistent disk (SQLite survives) | Yes, 512 MB | **No** — ephemeral, wiped on redeploy | Yes |
| Reaches Cloudflare R2 | **Yes** (`r2.cloudflarestorage.com` is allowlisted) | Yes | Yes |
| CPU to run `ffmpeg` | 100 CPU-s/day (web requests excluded) | 750 h/month shared | Metered |
| Custom domain | No (needs paid) | No | Yes |
| Bandwidth | Low | 5 GB/month | Metered |
| SSL | Yes (free, own domain only) | Yes | Yes |

### The three real problems with free hosting, and how to handle them

**1. "Forever" is not a thing.**
PythonAnywhere's free accounts have historically required re-confirming, and
free tiers get cut. Render cut free bandwidth from 100 GB to 5 GB in 2026.
Treat any free host as *temporary*: the code is on GitHub and the media is on
R2, so moving hosts is a 15-minute operation, not a rebuild. That is exactly
why the storage is already in R2 and not on the app server.

**2. Sleepy servers break the background jobs.**
Render's free tier spins the app down after 15 minutes of inactivity. Your MP3
extraction and subtitle generation run in a background thread of the *web
process* — if the process is killed mid-`ffmpeg`, the job dies and the status is
stuck on "processing". PythonAnywhere's free tier does not sleep, so this is
the main reason it is the better fit.

**3. Bandwidth goes through the app, not just R2.**
This is the subtle one. Videos stream **directly from R2**, which has free
egress. But the MP3 is proxied through Django, so every listener consumes
*PythonAnywhere's* bandwidth, and its free tier is explicitly "Low". Expect it
to be the first limit you hit with a popular video.

> **Fix, when that day comes:** serve the MP3 directly from R2 instead of
> proxying it. It costs a one-line change in `videos/views.py:audio_stream`
> (redirect to `video.audio_playback_url` instead of streaming) and would make
> your host's bandwidth usage essentially zero. The trade-off is that seeking
> and the service-worker offline cache are slightly less reliable, so it is a
> deliberate switch rather than the default.

### Recommendation

1. **Start on PythonAnywhere free** — it is the only option where the app is
   always on, SQLite persists, and R2 is reachable.
2. **Upgrade to the $10/month Developer plan** when you want a custom domain,
   more CPU, and no allowlist anxiety. This is the cheapest reliable step-up
   and is what this app actually deserves once it has real users.
3. **Migrate to Render/VPS** only if you outgrow it — and at that point switch
   SQLite to Postgres and move the background jobs to a real task queue.

---

## 2. Step-by-step: deploy to PythonAnywhere (free)

### Step 1 — Create the account

1. Go to <https://www.pythonanywhere.com/user/signup/> and sign up.
2. Choose the **Beginner** (free) account.
3. Verify your email address — the account is not usable until you do.

### Step 2 — Open a Bash console

On the PythonAnywhere dashboard click **Bash console**. This is a real Linux
shell; the rest of the commands run here.

### Step 3 — Get the code

Your repository is public, so a plain clone works:

```bash
git clone https://github.com/Makdial/fazakkir-dmwm.git
cd fazakkir-dmwm
```

The Django project lives in the `fazakkir_dmwm/` subdirectory:

```bash
cd fazakkir_dmwm
```

### Step 4 — Create a virtual environment

PythonAnywhere's free plan ships Python 3.13, which matches this project
(`requires-python = ">=3.13"`).

```bash
python3.13 -m venv /home/<yourusername>/fazakkir-venv
source /home/<yourusername>/fazakkir-venv/bin/activate
pip install --upgrade pip
pip install -e .
```

### Step 5 — Create the `.env` file

**This is the important step, and the one to do carefully.**

```bash
nano .env
```

Fill in at least:

```ini
DEBUG=0
SECRET_KEY=<paste the key you generated>
ALLOWED_HOSTS=<yourusername>.pythonanywhere.com
CSRF_TRUSTED_ORIGINS=

R2_ACCOUNT_ID=2422fc0c3eb620c18c237c952b9eccd6
R2_ACCESS_KEY_ID=<your R2 key id>
R2_SECRET_ACCESS_KEY=<your R2 secret>
R2_BUCKET=fazakkir
R2_PUBLIC_BASE_URL=
```

Generate a fresh secret key for this deployment (never reuse the one from your
machine, and never commit it):

```bash
python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
```

> Copy the R2 values from your local `fazakkir_dmwm/.env`. They are
> git-ignored, which is exactly why they are not in the GitHub repository.

### Step 6 — Create the database, collect static files, build translations

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py compiletranslations
```

### Step 7 — Create your administrator

```bash
python manage.py createsuperuser
```

Use the same credentials you use locally (`admin` / `admin12345` for now) so
your existing uploads, playlists and comments come across. **Change the
password immediately afterwards.**

### Step 8 — Point the web app at the virtual environment

On the **Web** tab:

1. **Add a new web app** → **Manual configuration** → Python 3.13.
2. Open the web app's **Virtualenv** section, set the path to
   `/home/<yourusername>/fazakkir-venv`.
3. Open the **Static files** section and add one mapping:

   | URL | Directory |
   | --- | --- |
   | `/static/` | `/home/<yourusername>/fazakkir-dmwm/fazakkir_dmwm/staticfiles` |

### Step 9 — Set the WSGI file

Click **WSGI configuration file**. Replace the whole contents with:

```python
import os
import sys

# Point the virtual environment at the project.
path = "/home/<yourusername>/fazakkir-venv/bin"
site_packages = "/home/<yourusername>/fazakkir-venv/lib/python3.13/site-packages"

sys.path.insert(0, path)
sys.path.insert(1, site_packages)

os.environ["DJANGO_SETTINGS_MODULE"] = "fazakkir_dmwm.settings"
sys.path.insert(0, "/home/<yourusername>/fazakkir-dmwm/fazakkir_dmwm")

from fazakkir_dmwm.wsgi import application
```

### Step 10 — Reload and verify

Hit the big green **Reload** button, then open
`https://<yourusername>.pythonanywhere.com/`.

Check `/manage/` loads and your video plays. If you get a 400, `ALLOWED_HOSTS`
is the usual cause.

### Step 11 — Schedule the storage check (optional but recommended)

On the **Tasks** tab, add a daily task at 08:00 UTC:

```
/home/<yourusername>/fazakkir-venv/bin/python /home/<yourusername>/fazakkir-dmwm/fazakkir_dmwm/manage.py checkstorage
```

---

## 3. Important: your PWA and background playback need HTTPS

You may have noticed this from the README, but it matters a great deal for
hosting, so repeating it here:

- The **install prompt**, **offline listening**, and the **lock-screen /
  background playback controls** only work in a *secure context* — i.e. real
  HTTPS. On the PythonAnywhere free subdomain you do get HTTPS, so this is
  fine.
- **Videos stream straight from R2**, so they work regardless of your host's
  bandwidth.

---

## 4. Going further: a custom domain

A custom domain requires the paid Developer plan ($10/month). Once you have it:

1. Buy a domain (Cloudflare Registrar is cheapest).
2. Add a **CNAME** record pointing at `<yourusername>.pythonanywhere.com`.
3. In PythonAnywhere, open the web app's **DOMAINS** tab and add your domain.
4. Update `.env`:

   ```ini
   ALLOWED_HOSTS=your-domain.com,www.your-domain.com
   CSRF_TRUSTED_ORIGINS=https://your-domain.com
   USE_X_FORWARDED_PROTO=1
   ```

5. Reload the web app.

---

## 5. Operating notes

### Backups

The videos and MP3s are in R2 and are safe. **The only irreplaceable state is
the SQLite database.** Back it up regularly:

```bash
python manage.py dumpdata --natural-foreign --natural-primary > backup.json
```

### Updating the site later

```bash
cd fazakkir-dmwm
git pull
source /home/<yourusername>/fazakkir-venv/bin/activate
cd fazakkir_dmwm
pip install -e .
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py compiletranslations
```

Then press **Reload** on the Web tab.

### Email alerts

Until `EMAIL_HOST` is set in `.env`, storage alerts are printed to the console
instead of being emailed. For Gmail you need an **App password** (requires
2-Step Verification), then:

```ini
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_HOST_USER=makhoutech@gmail.com
EMAIL_HOST_PASSWORD=<app password>
EMAIL_USE_TLS=1
```

Test it from `/manage/` with the **Send test email** button.

---

## Quick reference

| Task | Command (from the host) |
| --- | --- |
| Restart the app | Web tab → **Reload** |
| Update the code | `git pull && pip install -e . && python manage.py migrate` |
| Create an admin | `python manage.py createsuperuser` |
| Back up the database | `python manage.py dumpdata > backup.json` |
| Convert missing MP3s | `python manage.py generateaudio` |
| Generate subtitles | `python manage.py generatesubtitles` |
| Check storage | `python manage.py checkstorage` |