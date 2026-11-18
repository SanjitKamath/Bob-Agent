from bob_editor import server
from bob_sdk.manifest import Tier


async def test_the_server_offers_exactly_the_declared_tools():
    offered = {tool.name for tool in await server.server.list_tools()}
    assert offered == set(server.MANIFEST.tools)


def test_every_tool_is_a_granted_one_and_nothing_else_is_allowed():
    assert set(server.MANIFEST.tools.values()) == {Tier.READ_GRANTED, Tier.WRITE_GRANTED}
    assert server.MANIFEST.permissions.network == "none"
    assert not server.MANIFEST.default_enabled
