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
            import time

            time.sleep(10)
            try:
                from videos.audio_tasks import start_audio_sweep

                start_audio_sweep()
            except Exception:  # noqa: BLE001
                pass
            try:
                import django
                from django.core.management import call_command

                call_command("generatedurations", stdout=open(os.devnull, "w"))
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_run, daemon=True).start()
