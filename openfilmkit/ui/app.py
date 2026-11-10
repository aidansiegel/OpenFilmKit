"""`openfilmkit ui`: rolls and their frames, a recipe in plain words, a preview, the queue, and the
clean command one key away.

The preview is drawn in the window by textual-image, as well as the terminal allows: real pixels in
Ghostty, Kitty or WezTerm (and Sixel terminals), colored blocks in Terminal.app or over a plain
SSH session. Space opens the full size in Quick Look (macOS) or the default viewer.
Work is done by the openfilmkit command itself, in the background, with its output in the log.
"""
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path

import textual_image.renderable  # noqa: F401 - asks the terminal what it can draw, before Textual starts
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (Button, Checkbox, DataTable, Footer, Header, Label, ListItem, ListView, RadioButton,
                             RadioSet, RichLog, Static)
from textual_image.widget import Image

from .. import __version__
from . import model

QUEUE_EVERY = 30   # seconds between looks at the queue
VIEWS = ('after', 'before', 'side by side', 'fixes')   # b flips after/before; B, ctrl+shift+b side by side
SETTLE = 0.15      # seconds a preview waits for the keys to stop, so a flurry of presses loads one
KEEP = 12          # previews kept, so going back to one is instant


def configured():
    """({hosts}, {queues}) from hosts.toml, for the clean dialog and the queue panel."""
    try:
        from ..queues import _config
        cfg = _config()
    except Exception:   # an unreadable hosts.toml shouldn't keep the UI from starting
        return {}, {}
    return cfg.get('hosts', {}), cfg.get('queues', {})


class CleanDialog(ModalScreen):
    """Options and where to do the work; returns an openfilmkit clean argument list, or None."""
    BINDINGS = [Binding('escape', 'cancel', 'Cancel')]

    def __init__(self, count, hosts, queues):
        super().__init__()
        self.count, self.hosts, self.queues = count, hosts, queues

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Label(f"Clean {self.count} photo{'s' if self.count != 1 else ''}", id='dialog-title')
            yield Checkbox('Scratches and debris too (filled in by a model)', id='scratches')
            yield Checkbox('Correct levels and color cast', id='color')
            yield Checkbox('Darken red eyes', id='red-eye')
            yield Checkbox('Keep film-holder edges (no crop)', id='no-crop')
            yield Checkbox('Spare long straight lines (with scratches)', id='spare-long-lines')
            yield Label('Where')
            with RadioSet(id='where'):
                yield RadioButton('Here', value=True, id='here')
                for name in self.hosts:
                    yield RadioButton(f'On {name}', id=f'on-{name}')
                for name in self.queues:
                    yield RadioButton(f'Into the {name} queue', id=f'queue-{name}')
            with Horizontal(id='dialog-buttons'):
                yield Button('Clean', variant='primary', id='go')
                yield Button('Cancel', id='cancel')

    @on(Button.Pressed, '#go')
    def go(self):
        args = [f'--{box.id}' for box in self.query(Checkbox) if box.value]
        chosen = self.query_one('#where', RadioSet).pressed_button
        if chosen and chosen.id.startswith('on-'):
            args += ['--on', chosen.id[3:]]
        elif chosen and chosen.id.startswith('queue-'):
            args += ['--queue', chosen.id[6:]]
        self.dismiss(args)

    @on(Button.Pressed, '#cancel')
    def action_cancel(self):
        self.dismiss(None)


class HelpScreen(ModalScreen):
    BINDINGS = [Binding('escape,q,question_mark', 'app.pop_screen', 'Close')]
    KEYS = [('↑ ↓', 'move through the frames (← → between the list of rolls and the frames)'),
            ('m', 'mark or unmark a frame; a marks every frame that needs cleaning'),
            ('c', 'clean the marked frames (or the one selected): here, on a host or into a queue'),
            ('space', 'open the full size in Quick Look or the default viewer (with its original)'),
            ('b', 'flip the preview between after (the cleaned copy) and before (the original), in place'),
            ('B', 'before and after side by side (again: back to after)'),
            ('^⇧B', 'side by side with every fix marked in red (ctrl+b does the same, for any terminal)'),
            ('C', 'collect what the queue has finished'),
            ('r', 'look again at the files and the queue'),
            ('q', 'quit')]

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Label('OpenFilmKit keys', id='dialog-title')
            for key, what in self.KEYS:
                yield Label(f'[b]{key:>6}[/b]  {what}')
            yield Label('\nPreviews are sharpest in Ghostty, Kitty or WezTerm.')


class OpenFilmKitApp(App):
    TITLE = 'OpenFilmKit'
    CSS = '''
    #places { width: 24; border-right: solid $primary-darken-2; }
    #title { padding: 0 1; color: $text-muted; height: 1; }
    #frames { height: auto; max-height: 45%; }
    #detail { height: 1fr; min-height: 12; }
    #recipe { width: 1fr; padding: 0 1; overflow-y: auto; }
    #preview-side { width: 1fr; height: 100%; }
    #preview-caption { height: 1; color: $text-muted; text-align: center; }
    #preview-box { height: 1fr; align: center middle; }
    #preview { width: auto; height: auto; }   /* as big as fits, keeping the photo's shape */
    #queue { height: auto; max-height: 8; border-top: solid $primary-darken-2; padding: 0 1; }
    #log { height: 6; border-top: solid $primary-darken-2; display: none; }
    CleanDialog, HelpScreen { align: center middle; }
    #dialog { width: 72; height: auto; padding: 1 2; border: thick $primary; background: $surface; }
    #dialog-title { text-style: bold; margin-bottom: 1; }
    #dialog-buttons { height: auto; margin-top: 1; }
    '''
    BINDINGS = [Binding('space', 'look', 'Quick Look'), Binding('b', 'show(\'before\')', 'Before/after'),
                Binding('B', 'show(\'side by side\')', 'Side by side'),
                Binding('ctrl+shift+b,ctrl+b', 'show(\'fixes\')', 'Fixes', key_display='^⇧B'),
                Binding('m', 'mark', 'Mark'), Binding('a', 'mark_needing', 'Mark to clean', show=False),
                Binding('c', 'clean', 'Clean…'), Binding('C', 'collect', 'Collect', show=False),
                Binding('r', 'refresh', 'Refresh'), Binding('question_mark', 'help', 'Help'),
                Binding('q', 'quit', 'Quit')]

    def __init__(self, folder='.'):
        super().__init__()
        self.folder = Path(folder).expanduser().resolve()
        self.places = model.places(self.folder)
        self.place = self.places[0][1] if self.places else self.folder
        self.items, self.marked, self.jobs = [], set(), {}
        self.view = 'after'
        self.wanted = 0                  # the newest preview asked for; older loads are dropped
        self.previews = OrderedDict()    # (paths, view, mtimes) -> (image, caption)
        self.hosts, self.queues = configured()
        self.sub_title = __version__

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield ListView(*[ListItem(Label(label)) for label, _ in self.places], id='places')
            with Vertical():
                yield Static(id='title')
                yield DataTable(id='frames', cursor_type='row', zebra_stripes=True)
                with Horizontal(id='detail'):
                    yield Static(id='recipe')
                    with Vertical(id='preview-side'):
                        yield Static(id='preview-caption')
                        with Container(id='preview-box'):
                            yield Image(id='preview')
        yield Static(id='queue')
        yield RichLog(id='log', wrap=True, markup=True)
        yield Footer()

    def on_mount(self):
        table = self.query_one('#frames', DataTable)
        table.add_columns(' ', 'frame', 'master', 'conversion', 'cleaned', 'state')
        self.query_one('#queue').display = bool(self.queues)
        self.show_place()
        table.focus()
        if self.queues:
            self.look_at_queue()
            self.set_interval(QUEUE_EVERY, self.look_at_queue)

    # -- what's shown
    def show_place(self):
        self.items = model.items(self.place)
        self.marked &= {i.source for i in self.items if i.source}
        self.query_one('#title', Static).update(
            f'{self.place.name} · {model.roll_title(self.place)} · {len(self.items)} photos')
        self.fill_table()

    def state_of(self, item):
        job = self.jobs.get(str(item.source)) if item.source else None
        if job:
            where = f" on {job['worker']}" if job.get('worker') else ''
            return {'done': 'done → collect (C)'}.get(job['state'], job['state'] + where)
        return item.state

    def fill_table(self):
        table = self.query_one('#frames', DataTable)
        row = table.cursor_row
        table.clear()
        for i, item in enumerate(self.items):
            table.add_row('✓' if item.source in self.marked else ' ', item.name,
                          '✓' if item.master else '—', item.source.name if item.source else '—',
                          f'v{len(item.cleaned)}' if item.cleaned else '—', self.state_of(item), key=str(i))
        if self.items:
            table.move_cursor(row=min(max(row, 0), len(self.items) - 1))
        self.show_item()

    def current(self):
        table = self.query_one('#frames', DataTable)
        if not self.items or table.cursor_row is None or table.cursor_row >= len(self.items):
            return None
        return self.items[table.cursor_row]

    def show_item(self):
        item = self.current()
        self.query_one('#recipe', Static).update('\n'.join(model.describe(item)) if item else 'No photos here.')
        self.wanted += 1
        self.query_one('#preview-caption', Static).update(f'{self.view} …')
        self.load_preview(item, self.view, self.wanted)

    def from_thread(self, fn, *args):
        """Hand work back to the UI from a background thread, unless the app has closed meanwhile
        (quitting while a preview loads, say): then there's nothing left to update."""
        if not self.is_running:
            return
        try:
            self.call_from_thread(fn, *args)
        except RuntimeError:
            pass

    def make_preview(self, item, view):
        """(image, caption) for an item in one of the VIEWS."""
        if not item or not (item.latest or item.source or item.master):
            return None, ''
        if not item.latest:
            shown = item.source or item.master
            if shown is item.master:
                return None, 'not converted yet: no preview of a scan master'
            return model.preview_image(shown), f'{shown.name} (not cleaned yet)'
        if view == 'after':
            return model.preview_image(item.latest), f'after: {item.latest.name}   (b: before)'
        if view == 'before':
            return model.before_image(item.source, item.latest), f'before: {item.source.name}   (b: after)'
        if view == 'side by side':
            return model.side_by_side(item.source, item.latest)[0], f'before: {item.source.name}  |  after: {item.latest.name}'
        try:
            picture, n = model.side_by_side(item.source, item.latest, fixes=True)
        except model.NotComparable as e:
            return model.side_by_side(item.source, item.latest)[0], f'fixes: can\'t mark them; {e}'
        return picture, f'before  |  after, {n:,} pixels changed noticeably (by more than 8 levels) in red'

    @work(thread=True, exclusive=True, group='preview')
    def load_preview(self, item, view, ticket):
        time.sleep(SETTLE)
        if ticket != self.wanted:
            return                               # another key came since: that one's preview counts
        paths = tuple(str(p) for p in (item.source, item.latest) if p) if item else ()
        key = (paths, view, tuple(Path(p).stat().st_mtime_ns for p in paths if Path(p).exists()))
        try:
            found = self.previews.get(key) or self.make_preview(item, view)
        except Exception as e:   # an unreadable file: say so, keep going
            found = (None, f'preview: {e}')
        self.previews[key] = found
        self.previews.move_to_end(key)
        while len(self.previews) > KEEP:
            self.previews.popitem(last=False)
        self.from_thread(self.show_preview, ticket, *found)

    def show_preview(self, ticket, picture, caption):
        if ticket != self.wanted:   # checked here, on the UI's thread: a newer key may have come meanwhile
            return
        preview = self.query_one('#preview', Image)
        preview.image = picture
        preview.refresh(layout=True)             # size it for this picture's shape now, not the last one's
        self.query_one('#preview-caption', Static).update(caption)

    @on(DataTable.RowHighlighted, '#frames')
    def row_moved(self):
        self.show_item()

    @on(ListView.Highlighted, '#places')
    def place_moved(self, event):
        index = self.query_one('#places', ListView).index
        if index is not None and self.places[index][1] != self.place:
            self.place = self.places[index][1]
            self.show_place()

    def log_line(self, text):
        log = self.query_one('#log', RichLog)
        log.display = True   # shown once there's something to show
        log.write(text)

    # -- keys
    def action_look(self):
        item = self.current()
        if not item:
            return
        try:
            how = model.open_in_viewer(item.source, item.latest) if item.latest else \
                model.open_in_viewer(item.source or item.master)
            self.notify(f'Opened in {how}')
        except OSError as e:
            self.notify(str(e), severity='warning')

    def action_show(self, view):
        """b flips between after and before; B and ctrl+shift+b turn their side-by-side view on, or
        back off to after."""
        if view == 'before':
            self.view = 'after' if self.view == 'before' else 'before'
        else:
            self.view = 'after' if self.view == view else view
        self.show_item()

    def action_mark(self):
        item = self.current()
        if item and item.source:
            self.marked ^= {item.source}
            self.fill_table()

    def action_mark_needing(self):
        self.marked = {i.source for i in self.items if i.source and not i.cleaned}
        self.fill_table()

    def action_refresh(self):
        self.show_place()
        if self.queues:
            self.look_at_queue()

    def action_help(self):
        self.push_screen(HelpScreen())

    def action_clean(self):
        chosen = [i.source for i in self.items if i.source in self.marked]
        if not chosen and self.current() and self.current().source:
            chosen = [self.current().source]
        if not chosen:
            self.notify('Nothing to clean: convert the frame first', severity='warning')
            return

        def start(args):
            if args is not None:
                self.run_openfilmkit(['clean', *map(str, chosen), *args])
                self.marked.clear()
                self.fill_table()
        self.push_screen(CleanDialog(len(chosen), self.hosts, self.queues), start)

    def action_collect(self):
        if not self.queues:
            self.notify('No queue in hosts.toml', severity='warning')
            return
        for name in self.queues:
            self.run_openfilmkit(['queue', 'collect', name])

    # -- background work
    @work(thread=True, group='commands')
    def run_openfilmkit(self, argv):
        """Run an openfilmkit command (as a separate process, so its memory goes when it ends),
        showing what it says."""
        self.call_from_thread(self.log_line, f'[b]openfilmkit {" ".join(argv)}[/b]')
        proc = subprocess.Popen([sys.executable, '-m', 'openfilmkit', *argv], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            self.call_from_thread(self.log_line, line.rstrip())
        proc.wait()
        self.call_from_thread(self.log_line, '[green]done[/green]' if proc.returncode == 0
                              else f'[red]ended with status {proc.returncode}[/red]')
        self.call_from_thread(self.action_refresh)

    @work(thread=True, exclusive=True, group='queue')
    def look_at_queue(self):
        from .. import queues
        jobs, lines = {}, []
        for name in self.queues:
            try:
                info = queues.status(queues.queue_settings(name))
            except Exception as e:
                lines.append(f'Queue {name}: [red]{e}[/red]')
                continue
            for row in info['jobs']:
                jobs[row.get('photo')] = row
            counts = {}
            for row in info['jobs']:
                counts[row['state']] = counts.get(row['state'], 0) + 1
            summary = ', '.join(f'{n} {s}' for s, n in counts.items()) or 'empty'
            lines.append(f'Queue [b]{name}[/b]: {summary}')
            for w in info['workers']:
                busy = f"running {w['running'][:12]}" if w.get('running') else 'idle'
                lines.append(f"  worker {w['name']}: {busy}, up to {w.get('memory_gb')} GB, seen {w['seen_ago_s']} s ago")
        self.from_thread(self._queue_seen, jobs, '\n'.join(lines))

    def _queue_seen(self, jobs, text):
        self.jobs = jobs
        self.query_one('#queue', Static).update(text)
        self.fill_table()


def run(folder='.'):
    OpenFilmKitApp(folder).run()
