"""F4 \u2014 Universal Edit & Version History.

Cross-cutting service every module calls into so any editable record can be
changed with a reason, the old value is preserved, and there's one reusable
per-record History view. Public API: ``src.f4.service``.
"""