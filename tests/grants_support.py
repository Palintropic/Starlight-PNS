"""Test fixture helper: give characters every static grant a world offers.

Most runtime tests are about commits, exposure, memory or scheduling, not about
authorization. Their fixtures predate WORLD-1 access enforcement, so they call
this helper right after building the WorldState, before placing anyone: placing
and joining enforce grants, so the order matters. Authorization itself is
covered by tests/test_access_grants.py, which never uses this helper.
"""


from pns.models.location import accepted_roles


def grant_everything(world, character_ids=("mizuki", "ena", "kanade", "mafuyu")):
    for character_id in character_ids:
        for location in world.locations:
            if location.access.get("public") is True:
                continue
            accepted = accepted_roles(location.access)
            role = "fixture" if accepted is None else accepted[0]
            world._grant_location(character_id, location.location_id, role)
        for channel_id in world.channels.ids():
            world._grant_channel(character_id, channel_id)
    return world
