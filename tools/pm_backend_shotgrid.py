#!/usr/bin/env python3
"""The one ShotGrid implementation of the pm_backend.PMBackend seam.

Ports 17 copies of the same connect-from-env-vars block (animatic.py,
annotations.py, captions_sg.py, character_sheets.py, conform.py, delivery.py,
episode_assemble.py, episode_ingest.py, episode_setup.py, finishing.py,
genvideo_service.py, genvideo_worker.py, note_triage.py, panel_compose.py,
prompt_revision.py, sg_publish.py, video_from_panel.py) into ONE place
(invariant 11). Credentials are read from SHOTGRID_SITE_URL /
SHOTGRID_SCRIPT_NAME / SHOTGRID_SCRIPT_KEY -- environment variables only,
never printed, never written to a file, matching every one of those
originals exactly.

ShotGridBackend is a DELIBERATE, DOCUMENTED pure passthrough: every method
forwards its args/kwargs straight to the underlying shotgun_api3.Shotgun
client, unchanged. Two reasons, not laziness:

  1. Behaviour must not change (this is a mechanical migration -- see the
     migration report). Every call site in this codebase already calls
     these methods with shotgun_api3's own argument conventions (positional
     entity_type/filters/fields, keyword order=..., field_name=..., etc).
     Re-typing a narrower signature risks silently dropping a kwarg some
     call site depends on; passthrough cannot do that.
  2. It is the honest shape of what this seam actually is right now: a
     CONNECTION seam (one place builds the client, reads credentials, no
     tool imports shotgun_api3 directly any more), not a VOCABULARY seam.
     See pm_backend.py's docstring and docs/METHOD.md

get_backend() is the one factory every migrated tool calls in place of its
own former sg_connect(). It is not memoized/cached across calls -- each tool
process calls it once at the point it needs SG, exactly as before; nothing
here introduces a second way to obtain a client.
"""
import os
import sys

from pm_backend import PMBackend

_ENV = ("SHOTGRID_SITE_URL", "SHOTGRID_SCRIPT_NAME", "SHOTGRID_SCRIPT_KEY")


class ShotGridBackend(PMBackend):
    def __init__(self, sg):
        self._sg = sg

    @property
    def raw(self):
        """Escape hatch to the underlying shotgun_api3.Shotgun client, for
        the rare call this interface does not (yet) name explicitly. Every
        method a tool in this codebase actually calls IS named explicitly
        below (grepped across every tool -- see pm_backend.py); this exists
        so a future, narrower need does not require widening the whole
        interface for one call site. Reach for it sparingly: every use is a
        small vocabulary leak, same as the named methods, just unlabelled."""
        return self._sg

    def find(self, *a, **kw):
        return self._sg.find(*a, **kw)

    def find_one(self, *a, **kw):
        return self._sg.find_one(*a, **kw)

    def create(self, *a, **kw):
        return self._sg.create(*a, **kw)

    def update(self, *a, **kw):
        return self._sg.update(*a, **kw)

    def upload(self, *a, **kw):
        return self._sg.upload(*a, **kw)

    def upload_thumbnail(self, *a, **kw):
        return self._sg.upload_thumbnail(*a, **kw)

    def batch(self, *a, **kw):
        return self._sg.batch(*a, **kw)

    def schema_field_read(self, *a, **kw):
        return self._sg.schema_field_read(*a, **kw)

    def schema_field_create(self, *a, **kw):
        return self._sg.schema_field_create(*a, **kw)

    def schema_field_update(self, *a, **kw):
        return self._sg.schema_field_update(*a, **kw)


def get_backend():
    """-> ShotGridBackend, connected from SHOTGRID_* env vars. FATAL
    (loud sys.exit, never a swallowed exception) if any are unset --
    matching every sg_connect() this replaces."""
    import shotgun_api3
    missing = [n for n in _ENV if not os.environ.get(n)]
    if missing:
        sys.exit("FATAL: %s not set." % ", ".join(missing))
    sg = shotgun_api3.Shotgun(os.environ["SHOTGRID_SITE_URL"],
                              script_name=os.environ["SHOTGRID_SCRIPT_NAME"],
                              api_key=os.environ["SHOTGRID_SCRIPT_KEY"])
    return ShotGridBackend(sg)
