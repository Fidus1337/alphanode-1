"""Pasting a LIST of pairs into the universe box.

The box takes a whole list — that has always been true of Enter and of typed commas, but a
PASTE lost its tail: a typed comma used to commit everything before it and keep the rest in
the box, and applied to a paste that rule turned 'A,B,C' into two chips plus a dangling C.
Three pairs in, two pairs out reads as a field that only takes one. The typed-comma trigger
is gone entirely now (see test_universe_autofetch); a paste still commits itself.
"""
import pytest

pytestmark = pytest.mark.gui

LISTS = [
    ('ETHUSDT,SOLUSDT,XRPUSDT', ['ETHUSDT', 'SOLUSDT', 'XRPUSDT']),      # bare commas
    ('ETHUSDT, SOLUSDT, XRPUSDT', ['ETHUSDT', 'SOLUSDT', 'XRPUSDT']),    # commas + spaces
    ('ETHUSDT, SOLUSDT, XRPUSDT,', ['ETHUSDT', 'SOLUSDT', 'XRPUSDT']),   # trailing comma
    ('adausdt\ndotusdt\ntonusdt', ['ADAUSDT', 'DOTUSDT', 'TONUSDT']),    # a spreadsheet column
    ('LINKUSDT AVAXUSDT', ['LINKUSDT', 'AVAXUSDT']),                     # plain spaces
    ('OPUSDT', ['OPUSDT']),                                              # still fine with one
]


@pytest.fixture()
def pane(gui_app):
    """The real widget, actually on screen — an unmapped entry cannot take focus and
    silently drops every generated key event, which makes a paste test pass on nothing."""
    app, rec, state = gui_app
    app.root.geometry('1400x900')
    app.root.deiconify()
    app.cfg['settings_open'] = True
    app._apply_settings_vis()
    for _ in range(20):
        app.root.update()
    assert app.e_uni.winfo_ismapped()
    yield app
    app.root.withdraw()


def _paste(app, text):
    """As close to a real Ctrl+V as Tk allows: clipboard, focus, the key, then the KeyRelease
    the class binding is followed by in life."""
    app.e_uni.delete(0, 'end')
    app.root.clipboard_clear()
    app.root.clipboard_append(text)
    app.root.update()
    app.e_uni.focus_force()
    app.root.update()
    app.e_uni._entry.event_generate('<Control-v>')
    app.root.update()
    app.e_uni._entry.event_generate('<KeyRelease-v>')
    for _ in range(4):
        app.root.update()                            # let the after_idle commit run


@pytest.mark.parametrize('clip,want', LISTS)
def test_a_pasted_list_becomes_chips_all_at_once(pane, clip, want):
    app = pane
    app.v_unilist.set('BTCUSDT')
    _paste(app, clip)
    assert app.v_unilist.get().split(',') == ['BTCUSDT'] + want
    assert app.e_uni.get() == ''                     # nothing left dangling in the box


def test_paste_does_not_duplicate_what_is_already_there(pane):
    app = pane
    app.v_unilist.set('BTCUSDT,ETHUSDT')
    _paste(app, 'ETHUSDT,SOLUSDT')
    assert app.v_unilist.get() == 'BTCUSDT,ETHUSDT,SOLUSDT'


def test_typing_a_list_and_pressing_enter_once(pane):
    """The reported case, keystroke for keystroke: nothing may happen until Enter, and then
    everything must. Real key events, because the whole complaint was about what the box
    does WHILE you type."""
    app = pane
    app.v_unilist.set('BTCUSDT')
    app.e_uni.delete(0, 'end')
    app.e_uni.focus_force()
    app.root.update()
    named = {',': 'comma', ' ': 'space'}
    for ch in 'XMRUSDT, XLMUSDT':
        key = named.get(ch, ch)
        app.e_uni._entry.event_generate(f'<KeyPress-{key}>')
        app.e_uni._entry.event_generate(f'<KeyRelease-{key}>')
        app.root.update()
    assert app.e_uni.get() == 'XMRUSDT, XLMUSDT'      # the box keeps exactly what was typed
    assert app.v_unilist.get() == 'BTCUSDT'           # and nothing has been committed yet
    app.e_uni._entry.event_generate('<KeyPress-Return>')
    app.root.update()
    assert app.v_unilist.get() == 'BTCUSDT,XMRUSDT,XLMUSDT'
    assert app.e_uni.get() == ''


def test_the_placeholder_says_a_list_is_welcome(pane):
    app = pane
    assert 'list' in app.e_uni.cget('placeholder_text')
