from django.apps import AppConfig
import os
import sys
import threading


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        # Backfill missing MP3s automatically on the web process. Gated so
        # tests, migrations, shells and one-off commands are not affected.
        if os.environ.get("AUTO_GENERATE_AUDIO", "1") == "0":
            return
        argv = " ".join(sys.argv).lower()
        skip = (
            "test" in argv
            or "migrate" in argv
            or "makemigrations" in argv
            or "collectstatic" in argv
            or "shell" in argv
            or "createsuperuser" in argv
            or "check " in argv
            or "generateaudio" in argv
            or "generatedurations" in argv
            or "generatesubtitles" in argv
            or "translatetracks" in argv
            or "compiletranslations" in argv
        )
        if skip:
            return

        def _run():
            import subprocess
            import sys
            import time

            time.sleep(10)
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            manage_py = os.path.join(base_dir, "manage.py")
            if not os.path.exists(manage_py):
                base_dir = "/app/fazakkir_dmwm"
                manage_py = os.path.join(base_dir, "manage.py")
            env = os.environ.copy()
            env.setdefault("DJANGO_SETTINGS_MODULE", "fazakkir_dmwm.settings")
            env.setdefault("DEBUG", "0")
            # Run the audio sweep as a separate process so it is not killed
            # when a gunicorn worker handles its request and moves on.
            try:
                subprocess.Popen(
                    [sys.executable, manage_py, "generateaudio"],
                    cwd=base_dir,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:  # noqa: BLE001
                pass
            # Backfill durations the same way.
            try:
                subprocess.Popen(
                    [sys.executable, manage_py, "generatedurations"],
                    cwd=base_dir,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_run, daemon=True).start()
