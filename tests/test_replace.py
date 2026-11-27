"""Regression tests for structure-safe replacement (eDrawings File 10 failure)."""
from deid.replace import Replacer, is_generic_account


def u16(s: str) -> bytes:
    return s.encode("utf-16-le")


def test_mfc_class_names_never_touched():
    data = b"\xff\xff\x01\x00\x13\x00moLengthUserUnits_c\x02\x00\x10\x00uiUserModelEnv_c"
    r = Replacer({"User": "jkzt", "Length": "Abcdef"})
    out, st = r.apply(data)
    assert out == data
    assert st.replaced == 0


def test_generic_accounts_dropped():
    r = Replacer({"User": "jkzt", "Administrator": "Qwertyuiopasd", "Quintor": "Omswnny"})
    assert set(r.dropped_generic) == {"User", "Administrator"}
    out, _ = r.apply(b"C:\\Users\\User\\Pictures and Quintor")
    assert out == b"C:\\Users\\User\\Pictures and Omswnny"
    assert is_generic_account(" admin ")


def test_users_folder_not_hit_by_name_prefix():
    r = Replacer({"Use": "Xyz"})
    out, _ = r.apply(b"C:\\Users\\Use\\Desktop")
    assert out == b"C:\\Users\\Xyz\\Desktop"


def test_letter_valued_length_prefix_is_not_a_word_char():
    # ANSI CString: 1-byte length 0x4A ('J') immediately before the name
    data = b"\x00\x00\xffJkmarrow\x00\x01"
    out, st = Replacer({"kmarrow": "gtbwruo"}).apply(data)
    assert out == b"\x00\x00\xffJgtbwruo\x00\x01" and st.replaced == 1


def test_utf16_mfc_cstring():
    data = b"\xff\xfe\xff\x07" + u16("Quintor") + b"\x03\x80" + b"\xff\xfe\xff\x4a" + u16("C:\\Users\\Quintor\\Google")
    out, st = Replacer({"Quintor": "Omswnny"}).apply(data)
    assert u16("Quintor") not in out and st.replaced == 2
    assert out.count(u16("Omswnny")) == 2


def test_utf16_inside_identifier_skipped():
    data = u16("moQuintorUnits") + b"\x00\x00"
    out, st = Replacer({"Quintor": "Omswnny"}).apply(data)
    assert out == data and st.skipped_inside_token == 1


def test_xml_names_protected_values_replaced():
    data = (b'<propertySection name="UserDefinedProperties"><property name="Author">'
            b'<vt:lpstr>Quintor</vt:lpstr></property><Quintor attr="1"/><x Quintor="v"/>')
    out, st = Replacer({"Quintor": "Omswnny", "UserDefinedProperties": "AbcdDefinedProperties"}).apply(data)
    assert b"<vt:lpstr>Omswnny</vt:lpstr>" in out
    assert b"<Quintor attr" in out and b' Quintor="v"' in out  # tag + attribute names untouched
    assert b'name="AbcdDefinedProperties"' in out              # attribute VALUE is data -> replaced
    assert st.skipped_structural == 2


def test_longest_first_full_token_wins():
    data = b"\x0fquorvandelibast\x00 C:\\Users\\quorv\\AppData"
    out, _ = Replacer({"quorv": "aypkb", "quorvandelibast": "Qzrvlwtpeynabus"}).apply(data)
    assert out == b"\x0fqzrvlwtpeynabus\x00 C:\\Users\\aypkb\\AppData"  # case copied from the match


def test_prefix_only_name_is_left_and_reported_as_residual():
    data = b"Author: quorvandelibast\x00"
    r = Replacer({"quorv": "aypkb"})
    out, st = r.apply(data)
    assert out == data and st.skipped_inside_token == 1
    assert r.residual(out) == ["quorv"]  # caller must flag / extend entities


def test_cjk_substring_match_and_length_guard():
    r = Replacer({"江詠筑": "林雅婷"})
    out, st = r.apply("作业江詠筑HW6".encode("utf-8") + u16("江詠筑同学"))
    assert "江詠筑".encode() not in out and u16("江詠筑") not in out and st.replaced == 2
    r2 = Replacer({"Séba": "Seba"})  # cp1252 len 4 vs 4 ok; utf-8 5 vs 4 -> skipped in utf-8 only
    assert ("Séba", "utf-8") in r2.apply(b"x")[1].skipped_length_mismatch


def test_utf16_cstring_at_odd_offset_is_replaced():
    # regression: odd-offset UTF-16 matches were silently skipped (leak)
    data = b"\x01\xff\xfe\xff\x08" + u16("jfalkenr") + b"\x03"
    assert len(b"\x01\xff\xfe\xff\x08") % 2 == 1
    out, st = Replacer({"jfalkenr": "Kqhvnxmo"}).apply(data)
    assert u16("jfalkenr") not in out and st.replaced == 1


def test_case_variants_applied():
    data = b'<vt:lpstr>PartsVault</vt:lpstr><vt:lpstr>PARTSVAULT</vt:lpstr> partsvault'
    out, _ = Replacer({"PartsVault": "Qmzrtyvbnx"}).apply(data)
    assert b"PARTSVAULT" not in out and b"partsvault" not in out and b"QMZRTYVBNX" in out


def test_short_key_not_replaced_in_binary_noise():
    noise = b"\x9a\x12rje\x88\x01\x00\xferje\x7f"  # 'rje' bytes inside geometry
    out, st = Replacer({"rje": "qbx"}).apply(noise)
    assert out == noise and st.replaced == 0


def test_short_key_replaced_as_whole_string():
    ansi = b"\x00\x03rje\x00"                          # ANSI CString with length 3
    uni = b"\xff\xfe\xff\x03" + u16("rje") + b"\x05"   # Unicode CString
    path = b"C:\\Users\\rje\\Desktop"                   # delimited in text
    for data in (ansi, uni, path):
        out, st = Replacer({"rje": "qbx"}).apply(data)
        assert st.replaced == 1, data


def test_residual_ignores_binary_and_flags_text_variants():
    r = Replacer({"teodo": "Abcde", "rje": "qbx"})
    data = b"\x9a\x12teodo\x88\x01" + b"Desktop\\9900417_Teodorico_Vallemo\\part"
    out, _ = r.apply(data)
    assert r.residual(out) == ["teodo"]           # the longer real name in text is flagged
    assert r.residual(b"\x9a\x12rje\x88\x01\x02") == []   # binary noise ignored


def test_arbitrary_mixed_case_matched_and_case_transferred():
    out, st = Replacer({"ALeX": "Qmrv"}).apply(b"E:\\WorkSW_AleX\\ and alex and ALEX")
    assert out == b"E:\\WorkSW_QmrV\\ and qmrv and QMRV" and st.replaced == 3


def test_short_name_in_ole_property_set_replaced():
    # VT_LPSTR: type 0x1e, u32 length incl. NUL, text, NUL  (SummaryInformation Last Saved By)
    lpstr = b"\x1e\x00\x00\x00\x05\x00\x00\x00Tony\x00\x00\x00\x00"
    lpwstr = b"\x1f\x00\x00\x00\x05\x00\x00\x00" + u16("Tony") + b"\x00\x00"
    for data in (lpstr, lpwstr):
        out, st = Replacer({"Tony": "Eqsc"}).apply(data)
        assert st.replaced == 1 and b"Tony" not in out and u16("Tony") not in out, data


def test_short_keys_need_a_token_case_shape():
    from deid.replace import Replacer
    r = Replacer({"ZORB": "QAXE", "iZub": "rFeb"}, "global")
    assert r.apply(b" ZORB ")[0] == b" QAXE " and r.apply(b" zorb ")[0] == b" qaxe " and r.apply(b" Zorb ")[0] == b" Qaxe "
    assert r.apply(b"{ZoRb[bB")[0] == b"{ZoRb[bB"           # mixed-case run in binary noise: not a name
    assert r.apply(b" iZub's ")[0] == b" rFeb's "           # the surface's own mixed casing is fine


def test_one_byte_length_prefix_must_match_the_text_length():
    from deid.replace import Replacer
    r = Replacer({"Zorb": "Qaxe"}, "global")
    assert r.apply(b"\x00hzorb.sldprt\x00")[0] == b"\x00hzorb.sldprt\x00"       # 'h' is a letter, not a length
    assert r.apply(b"\x00\x0eZorb 2x4 Block\x00")[0] == b"\x00\x0eQaxe 2x4 Block\x00"   # real 1-byte length (14)


def test_cad_format_vocabulary_is_never_rewritten():
    from deid.replace import Replacer
    r = Replacer({"ACAD_ZORB": "QXCD_ZORB", "SW-Zorbex": "HH-Qaxeso", "Quorvex": "Bandulo"}, "global")
    assert r.apply(b"  3\nACAD_ZORB\n350\n")[0] == b"  3\nACAD_ZORB\n350\n"         # DXF dictionary name
    assert r.apply(b'<prop name="SW-Zorbex">Quorvex</prop>')[0] == b'<prop name="SW-Zorbex">Bandulo</prop>'
    u = "SW-Zorbex\x00".encode("utf-16-le")
    assert r.apply(u)[0] == u                                                      # OLE property name (UTF-16)
    assert r.apply(b" Quorvex ACAD ")[0] == b" Bandulo ACAD "


def test_cp1252_bytes_are_not_delimiters_in_binary():
    from deid.replace import Replacer
    r = Replacer({"ZORB": "QAXE"}, "global")
    blob = b"\x41\x42\x93ZORB\x94\x43\x44\x45\x46"        # cp1252 quote bytes around a short key in a binary run
    assert r.apply(blob)[0] == blob
    assert r.apply("“ZORB”".encode())[0] == "“QAXE”".encode()     # UTF-8 typographic quotes still delimit
