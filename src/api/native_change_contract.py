"""Cursor domains stay separate from authoritative history revisions."""


def sequence(value):
    if (not isinstance(value, str) or not value or not value.isascii()
            or not value.isdigit() or str(int(value)) != value or int(value) > 9223372036854775807):
        raise ValueError("Invalid integration change sequence")
    return int(value)


CHANGE_FIELDS = """afterSequence eventSequence headSequence snapshotSequence resyncRequired
    events { sequence externalOrderId revision }"""


def validate_change_page(page):
    if not isinstance(page, dict) or not isinstance(page.get("resyncRequired"), bool):
        raise ValueError("Invalid integration change page")
    after = sequence(page["afterSequence"]) if page.get("afterSequence") is not None else None
    end, head = [sequence(page.get(key)) for key in ("eventSequence", "headSequence")]
    events = page.get("events")
    if not isinstance(events, list) or len(events) > 100:
        raise ValueError("Invalid integration change events")
    if page["resyncRequired"]:
        if events:
            raise ValueError("Resync page contains events")
        if page.get("snapshotSequence", page["headSequence"]) != page["headSequence"]:
            raise ValueError("Invalid integration resync boundary")
        return
    if after is None or not after <= end <= head or (end < head and end == after):
        raise ValueError("Integration replay did not advance")
    previous = after
    for event in events:
        position = sequence(event.get("sequence"))
        sequence(event.get("revision"))
        if not previous < position <= end or not isinstance(event.get("externalOrderId"), str) or not event["externalOrderId"]:
            raise ValueError("Invalid integration change event")
        previous = position
