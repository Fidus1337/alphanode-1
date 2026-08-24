"""Select-with-the-mouse, copy-as-text: the live log pauses its rebuild under an active
selection and copies via Ctrl+C; a Treeview cell grows a read-only overlay whose text is
real, selectable text. gui_app comes from conftest (skips without a DISPLAY)."""


def _log_text(app):
    return app.logbox.get('1.0', 'end')


def _fwd_row(app, entry_id):
    """A REAL forward-track entry: _fwd_refresh (the 900ms after-job included) re-reads the
    same file, so the row survives event-loop spins — a bare tree.insert would be wiped."""
    ft = app._fwd_lib()
    tr = ft.load_track()
    tr['entries'].append(ft.new_entry('c560b8', 'alpha', ['tanh(high)'], ['BTC/USDT'],
                                      0.1, 0.001, '2026-01-01', tf='1h', entry_id=entry_id))
    ft.save_track(tr)
    app._fwd_refresh()


def test_log_ctrl_c_copies_selection(gui_app):
    app, rec, state = gui_app
    app._maybe_render_events([{'ts': '12:00:00', 't': 'hello copyable world', 'k': 'i'}])
    assert 'hello copyable world' in _log_text(app)
    app.logbox.tag_add('sel', '1.10', '1.30')            # "hello copyable world" minus the stamp
    picked = app.logbox.get('sel.first', 'sel.last')
    assert picked.strip()
    app.logbox._copy_sel()                               # the Ctrl+C binding
    assert app.root.clipboard_get() == picked
    assert not app._test_tk_errors


def test_log_holds_still_while_user_is_selecting(gui_app, monkeypatch):
    app, rec, state = gui_app
    a = [{'ts': '12:00:00', 't': 'round one', 'k': 'i'}]
    b = a + [{'ts': '12:00:05', 't': 'round two', 'k': 'i'}]
    app._maybe_render_events(a)
    assert 'round one' in _log_text(app)

    monkeypatch.setattr(app, '_log_sel_busy', lambda: True)   # user mid-drag over the log
    app._maybe_render_events(b)
    assert 'round two' not in _log_text(app)             # feed frozen under the mouse
    assert app._events_last == a                         # deferred, not dropped

    monkeypatch.setattr(app, '_log_sel_busy', lambda: False)  # mouse moved away
    app._maybe_render_events(b)
    assert 'round two' in _log_text(app)                 # the update lands on the next poll
    assert not app._test_tk_errors


def test_log_sel_busy_is_false_without_a_selection(gui_app):
    app, rec, state = gui_app
    app.logbox.tag_remove('sel', '1.0', 'end')
    assert app._log_sel_busy() is False


def test_tree_cell_overlay_holds_full_selectable_text(gui_app):
    app, rec, state = gui_app
    long_id = 'tanh_ts_mean_42_cs_rank_ema_159_volume_and_then_some_more_length'
    _fwd_row(app, long_id)
    app.root.deiconify()                                 # bbox needs a mapped window
    for _ in range(10):
        app.root.update()
    app.fwd_tree._cell_show(long_id, '#1')               # STRATEGY column
    ov = app.fwd_tree._cell_ov
    assert ov is not None, 'overlay never appeared over a visible cell'
    assert ov.get().strip() == long_id                   # FULL text, not the visual clip
    assert ov.selection_present()                        # pre-selected: Ctrl+C works at once
    assert '<Key-Escape>' in ov.bind()                   # Esc is wired; a synthesized
    app.fwd_tree._cell_kill()                            # keypress needs WM focus, so the
    app.root.update()                                    # dismissal itself is driven directly
    assert app.fwd_tree._cell_ov is None
    app.root.withdraw()
    assert not app._test_tk_errors


def test_tree_rebuild_dismisses_the_overlay(gui_app):
    app, rec, state = gui_app
    _fwd_row(app, 'c560b8')
    app.root.deiconify()
    for _ in range(10):
        app.root.update()
    app.fwd_tree._cell_show('c560b8', '#1')
    assert app.fwd_tree._cell_ov is not None, 'overlay never appeared over a visible cell'
    assert app.fwd_tree._cell_ov.get() == 'c560b8'
    app._fwd_refresh()                                   # the REAL rebuild path kills it first
    assert app.fwd_tree._cell_ov is None
    app.root.withdraw()
    assert not app._test_tk_errors


def test_every_table_is_wired_for_cell_selection(gui_app):
    app, rec, state = gui_app
    assert hasattr(app.tree, '_cell_show') and hasattr(app.tree, '_cell_kill')
    assert hasattr(app.fwd_tree, '_cell_show') and hasattr(app.fwd_tree, '_cell_kill')
    assert hasattr(app.logbox, '_copy_sel')              # the log's Ctrl+C
