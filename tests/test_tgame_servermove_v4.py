import hashlib
import struct

from tools.patches import tgame_servermove_v4 as sm4


def test_validated_bridge_template_matches_reconstruction():
    body = sm4._bridge_for(0x025BE000)
    assert len(body) == 639
    assert hashlib.sha256(body).hexdigest() == (
        "da3cabb501ac8d4eb937aba3759864e029c63af97d2a5964b26e72685c9429c4"
    )


def test_external_calls_rebase_to_exact_ph_targets():
    code_va = 0x02600000
    body = sm4._bridge_for(code_va)

    for insn_off, target in sm4._EXTERNAL_CALLS.items():
        assert body[insn_off] == 0xE8
        disp = struct.unpack_from("<i", body, insn_off + 1)[0]
        assert code_va + insn_off + 5 + disp == target


def test_controller_family_keeps_virtual_dispatch_contract():
    assert sm4.KNOWN_VTABLES["PVEPlayerController"][1] == 0x008F24B0
    assert sm4.KNOWN_VTABLES["TGMechaPlayerController"][1] == 0x008F24B0
    assert sm4.KNOWN_VTABLES["TGBioPlayerController"][1] == 0x008F24B0
    assert sm4.KNOWN_VTABLES["PZPlayerControllerBase"][1] == 0x0162AA20
    assert sm4.KNOWN_VTABLES["PZPlayerControllerBase"][2] == 0x0162ABB0
