"""
python manage.py voice_report                 -> who is enrolled, how alike the voiceprints are, recent checks
python manage.py voice_report --reset 98XXXXXXXX  -> delete that account's voiceprint (they set it up again)
python manage.py voice_report --unlock 98XXXXXXXX -> clear a 2-minute voice lockout right away

Read it like this:
  * Two DIFFERENT people's voiceprints should be under 0.35 to each other.
    0.55 or more means the same person (or a recording of mostly noise) is on both accounts:
    reset both and record again in a quiet place.
  * In "recent checks", the owner should score well above their line and well above
    "nearest other voice". If a real owner keeps landing just under the line, record again
    in the room and on the microphone used for the demo.
"""
import itertools

from django.core.management.base import BaseCommand

from bank import voice
from bank.models import AuditLog, VoiceProfile


class Command(BaseCommand):
    help = "Show enrolled voiceprints, how alike they are, and recent voice checks."

    def add_arguments(self, parser):
        parser.add_argument("--reset", metavar="PHONE", help="Delete this account's voiceprint")
        parser.add_argument("--unlock", metavar="PHONE", help="Clear a voice lockout (after 5 failed checks)")

    def handle(self, *args, **opts):
        if opts.get("reset"):
            phone = "".join(c for c in opts["reset"] if c.isdigit())[-10:]
            n, _ = VoiceProfile.objects.filter(account__phone=phone).delete()
            self.stdout.write(self.style.SUCCESS(f"Deleted {n} voiceprint(s) for {phone}. "
                                                 "Log in as that account to record the voice again."))
            return

        if opts.get("unlock"):
            phone = "".join(c for c in opts["unlock"] if c.isdigit())[-10:]
            n = VoiceProfile.objects.filter(account__phone=phone).update(failed_attempts=0, locked_until=None)
            self.stdout.write(self.style.SUCCESS(f"Unlocked voice checks for {phone} ({n} voiceprint)."))
            return

        profiles = list(VoiceProfile.objects.select_related("account").order_by("account__full_name"))
        if not profiles:
            self.stdout.write("No voiceprints saved yet.")
            return
        self.stdout.write(self.style.MIGRATE_HEADING("Enrolled voices"))
        for p in profiles:
            line = max(p.threshold or voice.DEFAULT_THRESHOLD, voice.THRESHOLD_FLOOR)
            self.stdout.write(f"  {p.account.full_name:24s} {p.account.phone}  line {line:.2f}  "
                              f"saved {p.updated_at:%d %b %H:%M}")

        if len(profiles) > 1:
            self.stdout.write(self.style.MIGRATE_HEADING("How alike the voiceprints are (different people: under 0.35)"))
            for a, b in itertools.combinations(profiles, 2):
                try:
                    sim = voice.cosine(a.embedding, b.embedding)
                except (ValueError, TypeError):
                    self.stdout.write(f"  {a.account.full_name} / {b.account.full_name}: different format, skipped")
                    continue
                verdict = (self.style.ERROR("SAME VOICE? reset both and record again") if sim >= voice.SAME_VOICE_ELSEWHERE
                           else self.style.WARNING("too close, record again in a quiet room") if sim >= 0.35
                           else self.style.SUCCESS("ok, clearly different"))
                self.stdout.write(f"  {a.account.full_name:20s} vs {b.account.full_name:20s} {sim:5.2f}  {verdict}")

        self.stdout.write(self.style.MIGRATE_HEADING("Recent voice checks"))
        rows = AuditLog.objects.filter(event__in=["voice_verified", "voice_failed"]).select_related("account")[:15]
        for r in rows:
            d = r.detail or {}
            self.stdout.write(f"  {r.created_at:%d %b %H:%M}  {getattr(r.account, 'full_name', '?'):20s} "
                              f"{'PASS' if r.event == 'voice_verified' else 'fail (' + str(d.get('reason')) + ')':28s} "
                              f"score {d.get('similarity', '-')}  line {d.get('threshold', '-')}  "
                              f"nearest other voice {d.get('best_other', '-')}")
