"""Test fixture helper: give characters every static grant a world offers.

Most runtime tests are about commits, exposure, memory or scheduling, not about
authorization. Their fixtures predate WORLD-1 access enforcement, so they call
this helper to keep testing what they were written for. Authorization itself is
covered by tests/test_access_grants.py, which never uses this helper.
"""


def grant_everything(world, character_ids=("mizuki", "ena")):
    for character_id in character_ids:
        for location in world.locations:
            if location.access.get("public") is True:
                continue
            role = location.access.get("role", "fixture")
            world.grant_location(character_id, location.location_id, role)
        for channel_id in world.channels.ids():
            world.grant_channel(character_id, channel_id)
    return world
