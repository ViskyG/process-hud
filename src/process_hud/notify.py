"""Best-effort native notifications; never bypass GNOME quiet/mute settings."""
from __future__ import annotations
import shutil
from .model import summary
from .util import tool


def _get_bool(schema, key):
    try:
        code, out, _ = tool(['gsettings', 'get', schema, key], timeout=2)
        if code == 0 and out.strip() in ('true', 'false'):
            return out.strip() == 'true'
    except (OSError, ValueError, TimeoutError):
        pass
    return None


def completed(store, job):
    # Independent of desktop banners/sound; enabled only by an explicit launch flag.
    from .codex_bridge import enqueue
    enqueue(store, job)
    if job['removed_at'] or not store.claim_notification(job['id']):
        return
    enabled, sound = store.setting('notify'), store.setting('sound')
    if not enabled and not sound:
        return
    server_sound = False
    try:
        if shutil.which('gdbus'):
            code, out, _ = tool(['gdbus', 'call', '--session', '--dest', 'org.freedesktop.Notifications',
                '--object-path', '/org/freedesktop/Notifications', '--method',
                'org.freedesktop.Notifications.GetCapabilities'], timeout=2)
            server_sound = code == 0 and "'sound'" in out
        if enabled:
            delivered = False
            if shutil.which('gdbus'):
                # This host's notify-send does not accept boolean hints.
                # The D-Bus call preserves the actual boolean type required by GNOME.
                hints = ("{'suppress-sound': <false>, 'sound-name': <'complete'>}"
                         if sound and server_sound else "{'suppress-sound': <true>}")
                code, _, _ = tool(['gdbus', 'call', '--session', '--dest', 'org.freedesktop.Notifications',
                    '--object-path', '/org/freedesktop/Notifications', '--method',
                    'org.freedesktop.Notifications.Notify', 'Process HUD', '0', '',
                    job['title'], summary(job), '[]', hints, '5000'], timeout=3)
                delivered = code == 0
            if not delivered and shutil.which('notify-send'):
                args = ['notify-send', '--app-name=Process HUD', '--urgency=normal']
                if sound and server_sound:
                    args += ['--hint=string:sound-name:complete']
                tool(args + ['--', job['title'], summary(job)], timeout=3)
        # GNOME can ignore notification sound hints. Use its event sound player only
        # when BOTH native preferences are available and explicitly allow it.
        if sound and not (enabled and server_sound) and shutil.which('canberra-gtk-play'):
            if (_get_bool('org.gnome.desktop.notifications', 'show-banners') is True and
                    _get_bool('org.gnome.desktop.sound', 'event-sounds') is True):
                tool(['canberra-gtk-play', '-i', 'complete', '-d', 'Process HUD'], timeout=3)
    except (OSError, ValueError, TimeoutError, RuntimeError):
        # Delivery failure must not change the task's actual outcome or delay work.
        return
