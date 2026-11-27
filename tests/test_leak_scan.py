"""Stage 4 map-free detectors: synthetic strings only."""
from deid.leak_scan import detect, survivors
from deid.rewrite.mapping import DeidMap

DM = DeidMap([dict(surface=k, pseudonym=v, scope="global", widths_ok=True)
              for k, v in {"Mira Talvenk": "Ines Fintaro", "Quorvex": "Bandulo", "qtalvenk": "bfintaro"}.items()])


def test_detectors_find_identity_shapes():
    found = dict((d, s) for d, s in detect("C:\\Users\\zorbel\\Desktop\\a.prt"))
    assert found["user_path"] == "zorbel"
    assert ("email", "z.orbel@quorvex.org") in detect("mail z.orbel@quorvex.org now")
    assert detect("info@quorvex.org") == []                          # role address
    assert ("initials", "D. ZORBEL") in detect("D. ZORBEL")
    assert ("field_label", "Zim Orbel") in detect("Drawn by: Zim Orbel")
    assert ("copyright", "Zorbel Robotics") in detect("Copyright (c) 2019 Zorbel Robotics")
    assert ("repo_owner", "zorbelbot") in detect("https://github.com/zorbelbot/arm")
    assert ("host_user", "ZORB-PC\\zim") in detect("author ZORB-PC\\zim (ZORB-PC)")
    assert detect("C:\\Users\\Public\\x") == []                       # generic profile


def test_survivors_ignore_mapped_names_and_flag_unmapped():
    assert survivors("Drawn by: Mira Talvenk", DM) == []             # mapped -> pseudonym, not a survivor
    assert survivors("C:\\Users\\qtalvenk\\x.sldprt", DM) == []
    assert survivors("Drawn by: Zim Orbel", DM) == [("field_label", "Zim Orbel")]
