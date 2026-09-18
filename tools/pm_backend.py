#!/usr/bin/env python3
"""The seam: what this pipeline actually needs from a project-management
system, as a documented interface, so a second backend COULD be written
without touching any tool.

Geoff: "Consider we should plan for this project to be ported to a
different project management database possibly." Every tool that talks to
ShotGrid currently imports shotgun_api3 directly, builds its own credentials
and constructs its own client -- seventeen copies of the same six lines.
That is the seam this closes: one factory (pm_backend_shotgrid.get_backend())
that hands back an object with the operations every tool actually calls, so
a tool never imports shotgun_api3 or constructs a client itself again.

BE HONEST ABOUT HOW THIN THIS IS (see docs/METHOD.md for the
full account). PMBackend's methods are a direct, method-for-method mirror of
shotgun_api3.Shotgun's own API -- find/find_one/create/update/upload/
upload_thumbnail/batch/schema_field_*  -- because that is the exact and only
method surface every tool in this codebase calls (grepped, not guessed).
This closes the CONNECTION seam: nothing outside pm_backend_shotgrid.py
constructs a client or reads SHOTGRID_* credentials. It does NOT close the
VOCABULARY seam: every call site still passes ShotGrid entity type strings
("Shot", "Version", "Sequence", "Episode", "Note", "Attachment"), ShotGrid
filter syntax ([["field", "is", value]]), and ShotGrid field names
(sg_status_list, sg_uploaded_movie, sg_path_to_movie, sg_gen_status, ...)
straight through this interface. A second backend implementing PMBackend
would still receive ShotGrid-shaped arguments at every call site. Porting
away from ShotGrid needs a translation layer or a rewrite of those call
sites -- this module does not pretend otherwise.

A concrete backend implements every method below with the same call
signature semantics as shotgun_api3.Shotgun's own methods (this codebase
passes shotgun_api3-style positional/keyword args straight through), so the
one implementation so far (pm_backend_shotgrid.ShotGridBackend) is a pure
passthrough -- see that module's docstring for why that is a deliberate,
documented choice and not laziness.
"""


class PMBackend(object):
    """Abstract interface. A concrete backend implements every method."""

    def find(self, entity_type, filters, fields, **kw):
        """-> list of entity dicts matching filters, with fields populated."""
        raise NotImplementedError

    def find_one(self, entity_type, filters, fields=None, **kw):
        """-> a single entity dict, or None."""
        raise NotImplementedError

    def create(self, entity_type, data, **kw):
        """-> the created entity dict (at least type + id)."""
        raise NotImplementedError

    def update(self, entity_type, entity_id, data, **kw):
        """-> the updated fields. Used for status changes too (e.g. data=
        {"sg_status_list": "rev"}) -- there is no separate set_status verb;
        see BACKEND-SEAM.md for why that is itself part of the vocabulary
        leak, not a limitation of this interface shape."""
        raise NotImplementedError

    def upload(self, entity_type, entity_id, path, field_name=None, display_name=None, **kw):
        """Attach media/a file to an entity -- this is how a Version's
        sg_uploaded_movie gets its media, and how a note/document gets
        attached (episode_setup.py attaches the bible/script/SRT this way)."""
        raise NotImplementedError

    def upload_thumbnail(self, entity_type, entity_id, path, **kw):
        raise NotImplementedError

    def batch(self, requests):
        """Multiple create/update/delete requests in one round trip."""
        raise NotImplementedError

    def schema_field_read(self, entity_type, **kw):
        """-> the field schema for entity_type. Used to discover whether a
        custom field already exists before creating one (episode_setup.py,
        script_to_beats.py's Story-Beat-entity probe)."""
        raise NotImplementedError

    def schema_field_create(self, entity_type, data_type, display_name, **kw):
        """-> the REAL field code ShotGrid assigned (it renames on
        creation -- see episode_setup.py's ensure_fields() for why the
        caller must never assume the name it asked for is the name it got)."""
        raise NotImplementedError

    def schema_field_update(self, entity_type, field_name, properties, **kw):
        raise NotImplementedError
