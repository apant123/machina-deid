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
    r = Replacer({"User": "jkzt", "Administrator": "Qwertyuiopasd", "Vincent": "Omswnny"})
    assert set(r.dropped_generic) == {"User", "Administrator"}
    out, _ = r.apply(b"C:\\Users\\User\\Pictures and Vincent")
    assert out == b"C:\\Users\\User\\Pictures and Omswnny"
    assert is_generic_account(" admin ")


def test_users_folder_not_hit_by_name_prefix():
    r = Replacer({"Use": "Xyz"})
    out, _ = r.apply(b"C:\\Users\\Use\\Desktop")
    assert out == b"C:\\Users\\Xyz\\Desktop"


def test_letter_valued_length_prefix_is_not_a_word_char():
    # ANSI CString: 1-byte length 0x4A ('J') immediately before the name
    data = b"\x00\x00\xffJdzucker\x00\x01"
    out, st = Replacer({"dzucker": "gtbwruo"}).apply(data)
    assert out == b"\x00\x00\xffJgtbwruo\x00\x01" and st.replaced == 1


def test_utf16_mfc_cstring():
    data = b"\xff\xfe\xff\x07" + u16("Vincent") + b"\x03\x80" + b"\xff\xfe\xff\x4a" + u16("C:\\Users\\Vincent\\Google")
    out, st = Replacer({"Vincent": "Omswnny"}).apply(data)
    assert u16("Vincent") not in out and st.replaced == 2
    assert out.count(u16("Omswnny")) == 2


def test_utf16_inside_identifier_skipped():
    data = u16("moVincentUnits") + b"\x00\x00"
    out, st = Replacer({"Vincent": "Omswnny"}).apply(data)
    assert out == data and st.skipped_inside_token == 1


def test_xml_names_protected_values_replaced():
    data = (b'<propertySection name="UserDefinedProperties"><property name="Author">'
            b'<vt:lpstr>Vincent</vt:lpstr></property><Vincent attr="1"/><x Vincent="v"/>')
    out, st = Replacer({"Vincent": "Omswnny", "UserDefinedProperties": "AbcdDefinedProperties"}).apply(data)
    assert b"<vt:lpstr>Omswnny</vt:lpstr>" in out
    assert b"<Vincent attr" in out and b' Vincent="v"' in out  # tag + attribute names untouched
    assert b'name="AbcdDefinedProperties"' in out              # attribute VALUE is data -> replaced
    assert st.skipped_structural == 2


def test_longest_first_full_token_wins():
    data = b"\x0fhackmancoltaire\x00 C:\\Users\\hackm\\AppData"
    out, _ = Replacer({"hackm": "aypkb", "hackmancoltaire": "Qzrvlwtpeynabus"}).apply(data)
    assert out == b"\x0fqzrvlwtpeynabus\x00 C:\\Users\\aypkb\\AppData"  # case copied from the match


def test_prefix_only_name_is_left_and_reported_as_residual():
    data = b"Author: hackmancoltaire\x00"
    r = Replacer({"hackm": "aypkb"})
    out, st = r.apply(data)
    assert out == data and st.skipped_inside_token == 1
    assert r.residual(out) == ["hackm"]  # caller must flag / extend entities


def test_cjk_substring_match_and_length_guard():
    r = Replacer({"江詠筑": "林雅婷"})
    out, st = r.apply("作业江詠筑HW6".encode("utf-8") + u16("江詠筑同学"))
    assert "江詠筑".encode() not in out and u16("江詠筑") not in out and st.replaced == 2
    r2 = Replacer({"Séba": "Seba"})  # cp1252 len 4 vs 4 ok; utf-8 5 vs 4 -> skipped in utf-8 only
    assert ("Séba", "utf-8") in r2.apply(b"x")[1].skipped_length_mismatch


def test_utf16_cstring_at_odd_offset_is_replaced():
    # regression: odd-offset UTF-16 matches were silently skipped (leak)
    data = b"\x01\xff\xfe\xff\x08" + u16("pcorreia") + b"\x03"
    assert len(b"\x01\xff\xfe\xff\x08") % 2 == 1
    out, st = Replacer({"pcorreia": "Kqhvnxmo"}).apply(data)
    assert u16("pcorreia") not in out and st.replaced == 1


def test_case_variants_applied():
    data = b'<vt:lpstr>TraceParts</vt:lpstr><vt:lpstr>TRACEPARTS</vt:lpstr> traceparts'
    out, _ = Replacer({"TraceParts": "Effwnthvmo"}).apply(data)
    assert b"TRACEPARTS" not in out and b"traceparts" not in out and b"EFFWNTHVMO" in out


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
    r = Replacer({"guilh": "Abcde", "rje": "qbx"})
    data = b"\x9a\x12guilh\x88\x01" + b"Desktop\\1103847_Guilherme_Grancho\\part"
    out, _ = r.apply(data)
    assert r.residual(out) == ["guilh"]           # the longer real name in text is flagged
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
