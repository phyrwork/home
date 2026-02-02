import re


def _slugify(value):
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


class FilterModule(object):
    def filters(self):
        return {
            "slugify": _slugify,
        }
